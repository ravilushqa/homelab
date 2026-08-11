#!/usr/bin/env python3
"""Build deterministic vova-medcenter overlays from immutable Git revisions."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


BASE_REVISION = "5f694c64f2e5127aabdee1adc1ca5c678325ef34"
BASE_IMAGES = {
    "backend": "vova-medcenter-backend:5f694c6-offline-overlay",
    "frontend": "vova-medcenter-frontend:5f694c6-offline-overlay",
}
MANAGED_GLOBS = (
    "backend-overlay-*.tar.gz.b64",
    "backend-overlay-*.delete.sh",
    "frontend-overlay-*.tar.gz.b64",
    "frontend-overlay-*.delete.sh",
)
LABEL_OVERLAY_SHA = "space.ravil.vova-medcenter.overlay-sha256"


@dataclass(frozen=True)
class GitFile:
    source: str
    destination: str
    blob: str
    mode: int


@dataclass(frozen=True)
class ServicePayload:
    revision: str
    files: tuple[GitFile, ...]
    deletions: tuple[str, ...]
    payload_sha256: str


def run_git(repo: Path, *args: str, text: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    return result.stdout


def resolve_revision(repo: Path, revision: str) -> str:
    return str(run_git(repo, "rev-parse", f"{revision}^{{commit}}", text=True)).strip()


def git_blob(repo: Path, revision: str, path: str) -> bytes:
    return bytes(run_git(repo, "show", f"{revision}:{path}"))


def ls_tree(repo: Path, revision: str, prefixes: Iterable[str]) -> list[tuple[str, str, str]]:
    raw = bytes(run_git(repo, "ls-tree", "-r", "-z", revision, "--", *prefixes))
    rows: list[tuple[str, str, str]] = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, path_bytes = entry.split(b"\t", 1)
        mode, object_type, blob = meta.decode("ascii").split(" ")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ValueError(f"Unsupported Git entry {mode} {object_type}: {path_bytes!r}")
        rows.append((path_bytes.decode("utf-8"), blob, mode))
    return rows


def validate_destination(destination: str, allowed_roots: tuple[str, ...]) -> None:
    path = PurePosixPath(destination)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Unsafe overlay destination: {destination!r}")
    if not any(destination == root or destination.startswith(f"{root}/") for root in allowed_roots):
        raise ValueError(f"Overlay destination is outside allowed roots: {destination!r}")


def backend_destinations(source: str) -> tuple[str, ...]:
    if source.startswith("backend/app/"):
        return (f"app/{source.removeprefix('backend/')}",)
    if source.startswith("backend/migrations/"):
        return (f"app/{source.removeprefix('backend/')}",)
    if source == "backend/alembic.ini":
        return ("app/alembic.ini",)
    if source.startswith("assets/templates/"):
        return (source,)
    if source.startswith("frontend/public/"):
        relative = source.removeprefix("frontend/public/")
        return (f"app/frontend/public/{relative}", f"frontend/public/{relative}")
    return ()


def frontend_destinations(source: str) -> tuple[str, ...]:
    if source == "frontend/index.html":
        return ("usr/share/nginx/html/index.html",)
    if source.startswith("frontend/public/"):
        relative = source.removeprefix("frontend/public/")
        return (f"usr/share/nginx/html/{relative}",)
    return ()


def tree_map(
    repo: Path,
    revision: str,
    prefixes: tuple[str, ...],
    mapper,
    allowed_roots: tuple[str, ...],
) -> dict[str, GitFile]:
    mapped: dict[str, GitFile] = {}
    for source, blob, git_mode in ls_tree(repo, revision, prefixes):
        for destination in mapper(source):
            validate_destination(destination, allowed_roots)
            mode = 0o755 if git_mode == "100755" else 0o644
            item = GitFile(source, destination, blob, mode)
            if destination in mapped and mapped[destination] != item:
                raise ValueError(f"Multiple sources map to {destination}")
            mapped[destination] = item
    return mapped


def payload_hash(repo: Path, revision: str, files: Iterable[GitFile], deletions: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value.destination):
        content = git_blob(repo, revision, item.source)
        digest.update(b"file\0")
        digest.update(item.destination.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.mode).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    for destination in sorted(deletions):
        digest.update(b"delete\0")
        digest.update(destination.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_service_payload(repo: Path, service: str, revision: str) -> ServicePayload:
    if service == "backend":
        prefixes = ("backend/app", "backend/migrations", "backend/alembic.ini", "assets/templates", "frontend/public")
        mapper = backend_destinations
        allowed = ("app", "assets/templates", "frontend/public")
    elif service == "frontend":
        prefixes = ("frontend/index.html", "frontend/public")
        mapper = frontend_destinations
        allowed = ("usr/share/nginx/html",)
    else:
        raise ValueError(f"Unknown service: {service}")

    base = tree_map(repo, BASE_REVISION, prefixes, mapper, allowed)
    target = tree_map(repo, revision, prefixes, mapper, allowed)
    files = tuple(
        target[destination]
        for destination in sorted(target)
        if destination not in base
        or target[destination].blob != base[destination].blob
        or target[destination].mode != base[destination].mode
    )
    deletions = tuple(sorted(set(base) - set(target)))
    return ServicePayload(revision, files, deletions, payload_hash(repo, revision, files, deletions))


def package_json_build_contract(repo: Path, revision: str) -> dict[str, object]:
    package = json.loads(git_blob(repo, revision, "frontend/package.json"))
    return {
        "type": package.get("type"),
        "build": package.get("scripts", {}).get("build"),
        "dependencies": package.get("dependencies", {}),
        "devDependencies": package.get("devDependencies", {}),
    }


def validate_build_contract(repo: Path, backend_revision: str, frontend_revision: str) -> None:
    exact_guards = (
        ("backend", backend_revision, "backend/requirements.txt"),
        ("frontend", frontend_revision, "frontend/package-lock.json"),
        ("frontend", frontend_revision, "frontend/vite.config.ts"),
    )
    for service, revision, path in exact_guards:
        if git_blob(repo, BASE_REVISION, path) != git_blob(repo, revision, path):
            raise ValueError(
                f"{service} build input {path} changed since canonical base {BASE_REVISION}; "
                "create a new base image or move the release to GHCR"
            )
    if package_json_build_contract(repo, BASE_REVISION) != package_json_build_contract(repo, frontend_revision):
        raise ValueError(
            "frontend/package.json build contract changed since the canonical base; "
            "create a new base image or move the release to GHCR"
        )


def deletion_script(deletions: Iterable[str]) -> bytes:
    lines = ["#!/bin/sh", "set -eu"]
    for destination in sorted(deletions):
        escaped = ("/" + destination).replace("'", "'\"'\"'")
        lines.append(f"rm -f -- '{escaped}'")
    return ("\n".join(lines) + "\n").encode("utf-8")


def make_archive(repo: Path, payload: ServicePayload, extra_files: dict[str, bytes] | None = None) -> bytes:
    members: list[tuple[str, bytes, int]] = []
    for item in payload.files:
        members.append((item.destination, git_blob(repo, payload.revision, item.source), item.mode))
    for destination, content in sorted((extra_files or {}).items()):
        validate_destination(destination, ("usr/share/nginx/html",))
        members.append((destination, content, 0o644))

    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", compresslevel=9, mtime=0) as gzip_file:
        with tarfile.open(fileobj=gzip_file, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for destination, content, mode in sorted(members):
                info = tarfile.TarInfo(destination)
                info.size = len(content)
                info.mode = mode
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = "root"
                info.gname = "root"
                archive.addfile(info, io.BytesIO(content))
    return base64.encodebytes(compressed.getvalue())


def render_compose(template: str, values: dict[str, str]) -> bytes:
    rendered = template
    for key, value in values.items():
        marker = f"@@{key}@@"
        if rendered.count(marker) != 1:
            raise ValueError(f"Expected exactly one {marker} in compose.yaml.in")
        rendered = rendered.replace(marker, value)
    leftovers = sorted(part for part in rendered.split() if "@@" in part)
    if leftovers:
        raise ValueError(f"Unresolved compose template markers: {leftovers}")
    return rendered.encode("utf-8")


def build_outputs(repo: Path, stack_dir: Path, backend_revision: str, frontend_revision: str) -> dict[str, bytes]:
    backend_revision = resolve_revision(repo, backend_revision)
    frontend_revision = resolve_revision(repo, frontend_revision)
    if resolve_revision(repo, BASE_REVISION) != BASE_REVISION:
        raise ValueError("Canonical base revision did not resolve to the expected commit")
    validate_build_contract(repo, backend_revision, frontend_revision)

    backend = build_service_payload(repo, "backend", backend_revision)
    frontend = build_service_payload(repo, "frontend", frontend_revision)
    backend_tag = f"{backend_revision[:12]}-cum-{backend.payload_sha256[:12]}"
    frontend_tag = f"{frontend_revision[:12]}-cum-{frontend.payload_sha256[:12]}"
    backend_image = f"vova-medcenter-backend:{backend_tag}"
    frontend_image = f"vova-medcenter-frontend:{frontend_tag}"

    public_release = {
        "schema_version": 1,
        "backend": {
            "image": backend_image,
            "overlay_sha256": backend.payload_sha256,
            "revision": backend_revision,
        },
        "frontend": {
            "image": frontend_image,
            "overlay_sha256": frontend.payload_sha256,
            "revision": frontend_revision,
        },
    }
    public_release_bytes = (json.dumps(public_release, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    backend_archive = make_archive(repo, backend)
    frontend_archive = make_archive(
        repo,
        frontend,
        {"usr/share/nginx/html/demo/release.json": public_release_bytes},
    )
    backend_delete = deletion_script(backend.deletions)
    frontend_delete = deletion_script(frontend.deletions)

    backend_stem = f"backend-overlay-{backend_tag}"
    frontend_stem = f"frontend-overlay-{frontend_tag}"
    release = {
        **public_release,
        "base": {
            "backend_image": BASE_IMAGES["backend"],
            "frontend_image": BASE_IMAGES["frontend"],
            "revision": BASE_REVISION,
        },
        "backend": {
            **public_release["backend"],
            "archive": f"{backend_stem}.tar.gz.b64",
            "archive_sha256": hashlib.sha256(backend_archive).hexdigest(),
            "delete_script": f"{backend_stem}.delete.sh",
            "delete_script_sha256": hashlib.sha256(backend_delete).hexdigest(),
        },
        "frontend": {
            **public_release["frontend"],
            "archive": f"{frontend_stem}.tar.gz.b64",
            "archive_sha256": hashlib.sha256(frontend_archive).hexdigest(),
            "delete_script": f"{frontend_stem}.delete.sh",
            "delete_script_sha256": hashlib.sha256(frontend_delete).hexdigest(),
        },
    }
    release_bytes = (json.dumps(release, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    template = (stack_dir / "compose.yaml.in").read_text(encoding="utf-8")
    compose = render_compose(
        template,
        {
            "BACKEND_IMAGE": backend_image,
            "BACKEND_REVISION": backend_revision,
            "BACKEND_OVERLAY_SHA256": backend.payload_sha256,
            "BACKEND_DELETE_SCRIPT": f"{backend_stem}.delete.sh",
            "BACKEND_ARCHIVE": f"{backend_stem}.tar.gz.b64",
            "FRONTEND_IMAGE": frontend_image,
            "FRONTEND_REVISION": frontend_revision,
            "FRONTEND_OVERLAY_SHA256": frontend.payload_sha256,
            "FRONTEND_DELETE_SCRIPT": f"{frontend_stem}.delete.sh",
            "FRONTEND_ARCHIVE": f"{frontend_stem}.tar.gz.b64",
        },
    )
    return {
        f"{backend_stem}.tar.gz.b64": backend_archive,
        f"{backend_stem}.delete.sh": backend_delete,
        f"{frontend_stem}.tar.gz.b64": frontend_archive,
        f"{frontend_stem}.delete.sh": frontend_delete,
        "compose.yaml": compose,
        "release.json": release_bytes,
    }


def managed_paths(stack_dir: Path) -> set[Path]:
    paths: set[Path] = set()
    for pattern in MANAGED_GLOBS:
        paths.update(stack_dir.glob(pattern))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-repo", required=True, type=Path)
    parser.add_argument("--backend-revision", required=True)
    parser.add_argument("--frontend-revision", required=True)
    parser.add_argument("--stack-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--check", action="store_true", help="Verify generated files without changing them")
    args = parser.parse_args()

    repo = args.app_repo.resolve()
    stack_dir = args.stack_dir.resolve()
    outputs = build_outputs(repo, stack_dir, args.backend_revision, args.frontend_revision)
    expected_managed = {stack_dir / name for name in outputs if "-overlay-" in name}
    obsolete = managed_paths(stack_dir) - expected_managed

    if args.check:
        mismatches = [
            name
            for name, content in outputs.items()
            if not (stack_dir / name).is_file() or (stack_dir / name).read_bytes() != content
        ]
        if mismatches or obsolete:
            for name in mismatches:
                print(f"OUTDATED: {name}", file=sys.stderr)
            for path in sorted(obsolete):
                print(f"OBSOLETE: {path.name}", file=sys.stderr)
            return 1
        print("Generated overlays, compose.yaml, and release.json are current.")
        return 0

    stack_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(obsolete):
        path.unlink()
    for name, content in outputs.items():
        path = stack_dir / name
        path.write_bytes(content)
        if name.endswith(".delete.sh"):
            os.chmod(path, 0o755)
    print(json.loads(outputs["release.json"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

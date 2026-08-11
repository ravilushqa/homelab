from __future__ import annotations

import base64
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATOR_PATH = REPO_ROOT / "komodo" / "stacks" / "vova-medcenter" / "generate_overlays.py"
VERIFIER_PATH = REPO_ROOT / ".github" / "scripts" / "verify_vova_medcenter_live.py"
TEMPLATE_PATH = REPO_ROOT / "komodo" / "stacks" / "vova-medcenter" / "compose.yaml.in"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generator = load_module("vova_overlay_generator", GENERATOR_PATH)
verifier_module = load_module("vova_live_verifier", VERIFIER_PATH)


class GeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.stack = self.root / "stack"
        self.app.mkdir()
        self.stack.mkdir()
        (self.stack / "compose.yaml.in").write_bytes(TEMPLATE_PATH.read_bytes())
        self.git("init")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.write("backend/requirements.txt", b"fastapi==1\n")
        self.write("backend/app/main.py", b"VALUE = 'base'\n")
        self.write("backend/alembic.ini", b"[alembic]\n")
        self.write("assets/templates/Templates/\u041b\u041c\u041a.xls", b"base-template")
        self.write("frontend/package-lock.json", b"{}\n")
        self.write("frontend/vite.config.ts", b"export default {}\n")
        self.write(
            "frontend/package.json",
            json.dumps(
                {
                    "type": "module",
                    "scripts": {"build": "vite build"},
                    "dependencies": {},
                    "devDependencies": {"vite": "1"},
                }
            ).encode(),
        )
        self.write("frontend/index.html", b"<meta http-equiv='refresh'>\n")
        self.write("frontend/public/demo/app.js", b"const version = 'base';\n")
        self.commit("base")
        self.base_revision = self.git("rev-parse", "HEAD").strip()

        self.write("backend/app/main.py", b"VALUE = 'target'\n")
        (self.app / "assets/templates/Templates/\u041b\u041c\u041a.xls").unlink()
        self.write("frontend/public/demo/app.js", b"const version = 'target';\n")
        self.write("frontend/public/demo/\u043d\u043e\u0432\u044b\u0439.txt", "\u0434\u0430\u043d\u043d\u044b\u0435\n".encode("utf-8"))
        package = json.loads((self.app / "frontend/package.json").read_text())
        package["scripts"]["test"] = "node --test"
        self.write("frontend/package.json", json.dumps(package).encode())
        self.commit("target")
        self.target_revision = self.git("rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.app), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def write(self, relative: str, content: bytes) -> None:
        path = self.app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def commit(self, message: str) -> None:
        self.git("add", ".")
        self.git("commit", "-m", message)

    def test_deterministic_cumulative_overlay_and_deletions(self) -> None:
        with mock.patch.object(generator, "BASE_REVISION", self.base_revision):
            first = generator.build_outputs(self.app, self.stack, self.target_revision, self.target_revision)
            second = generator.build_outputs(self.app, self.stack, self.target_revision, self.target_revision)
        self.assertEqual(first, second)

        release = json.loads(first["release.json"])
        backend_delete = first[release["backend"]["delete_script"]].decode("utf-8")
        self.assertIn("/assets/templates/Templates/\u041b\u041c\u041a.xls", backend_delete)

        frontend_archive = base64.b64decode(first[release["frontend"]["archive"]])
        with tarfile.open(fileobj=io.BytesIO(frontend_archive), mode="r:gz") as archive:
            names = archive.getnames()
            public_release = json.loads(archive.extractfile("usr/share/nginx/html/demo/release.json").read())
        self.assertIn("usr/share/nginx/html/demo/\u043d\u043e\u0432\u044b\u0439.txt", names)
        self.assertEqual(public_release["frontend"]["revision"], self.target_revision)
        self.assertIn(release["frontend"]["overlay_sha256"][:12], release["frontend"]["image"])

    def test_build_dependency_change_is_rejected(self) -> None:
        self.write("backend/requirements.txt", b"fastapi==2\n")
        self.commit("dependency change")
        incompatible = self.git("rev-parse", "HEAD").strip()
        with mock.patch.object(generator, "BASE_REVISION", self.base_revision):
            with self.assertRaisesRegex(ValueError, "create a new base image or move the release to GHCR"):
                generator.validate_build_contract(self.app, incompatible, self.target_revision)

    def test_unsafe_destination_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsafe overlay destination"):
            generator.validate_destination("../escape", ("app",))


class FakeRequests:
    def __init__(self, release: dict) -> None:
        self.release = release
        self.base_ids = {"backend": "sha256:base-backend", "frontend": "sha256:base-frontend"}
        self.running = {"backend": True, "frontend": True}
        self.images = {service: release[service]["image"] for service in ("backend", "frontend")}
        self.revisions = {service: release[service]["revision"] for service in ("backend", "frontend")}
        self.overlays = {service: release[service]["overlay_sha256"] for service in ("backend", "frontend")}

    def __call__(self, url: str, payload: dict | None) -> dict:
        if url.endswith("/read"):
            request_type = payload["type"]
            params = payload["params"]
            if request_type == "GetStack":
                return {"config": {"server_id": "server-prod"}}
            if request_type == "InspectDockerImage":
                service = "backend" if "backend" in params["image"] else "frontend"
                return {"id": self.base_ids[service]}
            if request_type == "InspectStackContainer":
                service = params["service"]
                return {
                    "image": f"sha256:live-{service}",
                    "config": {
                        "image": self.images[service],
                        "labels": {
                            verifier_module.REVISION_LABEL: self.revisions[service],
                            verifier_module.OVERLAY_LABEL: self.overlays[service],
                        },
                    },
                    "state": {"running": self.running[service]},
                }
        if "/api/v1/health" in url:
            return {"status": "ok"}
        if "/demo/release.json" in url:
            return {
                "schema_version": self.release["schema_version"],
                "backend": {key: self.release["backend"][key] for key in ("image", "overlay_sha256", "revision")},
                "frontend": {key: self.release["frontend"][key] for key in ("image", "overlay_sha256", "revision")},
            }
        raise AssertionError((url, payload))


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.release = json.loads((REPO_ROOT / "komodo/stacks/vova-medcenter/release.json").read_text())
        self.requests = FakeRequests(self.release)
        self.verifier = verifier_module.LiveVerifier(
            self.release,
            "https://komodo.example",
            "https://vova.example",
            self.requests,
        )

    def test_successful_preflight_and_live_release(self) -> None:
        self.verifier.preflight()
        self.verifier.verify_once()

    def test_missing_base_fails_preflight(self) -> None:
        self.requests.base_ids["backend"] = ""
        with self.assertRaisesRegex(verifier_module.VerificationError, "has no image ID"):
            self.verifier.preflight()

    def test_old_tag_fails(self) -> None:
        self.requests.images["frontend"] = "vova-medcenter-frontend:old"
        with self.assertRaisesRegex(verifier_module.VerificationError, "frontend mismatch"):
            self.verifier.verify_once()

    def test_wrong_label_fails(self) -> None:
        self.requests.overlays["backend"] = "wrong"
        with self.assertRaisesRegex(verifier_module.VerificationError, "backend mismatch"):
            self.verifier.verify_once()

    def test_stopped_container_fails(self) -> None:
        self.requests.running["backend"] = False
        with self.assertRaisesRegex(verifier_module.VerificationError, "backend mismatch"):
            self.verifier.verify_once()

    def test_postdeploy_timeout_fails(self) -> None:
        self.requests.images["frontend"] = "vova-medcenter-frontend:old"
        with self.assertRaisesRegex(verifier_module.VerificationError, "Timed out"):
            self.verifier.postdeploy(attempts=2, delay_seconds=0)


if __name__ == "__main__":
    unittest.main()

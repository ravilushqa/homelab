#!/usr/bin/env python3
"""Verify canonical bases and the live vova-medcenter release through Komodo."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


OVERLAY_LABEL = "space.ravil.vova-medcenter.overlay-sha256"
REVISION_LABEL = "org.opencontainers.image.revision"


class VerificationError(RuntimeError):
    pass


class JsonHttpClient:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self.headers = {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "X-Api-Secret": api_secret,
        }

    def request(self, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = self.headers if payload is not None else {"Cache-Control": "no-cache"}
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.HTTPError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(1)
        raise VerificationError(f"Request failed for {url}: {last_error}")


class LiveVerifier:
    def __init__(
        self,
        release: dict[str, Any],
        komodo_url: str,
        public_url: str,
        request_json: Callable[[str, dict[str, Any] | None], dict[str, Any]],
        stack_name: str = "vova-medcenter",
    ) -> None:
        self.release = release
        self.komodo_url = komodo_url.rstrip("/")
        self.public_url = public_url.rstrip("/")
        self.request_json = request_json
        self.stack_name = stack_name

    def komodo_read(self, request_type: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.request_json(
            f"{self.komodo_url}/read",
            {"type": request_type, "params": params},
        )

    def preflight(self) -> None:
        stack = self.komodo_read("GetStack", {"stack": self.stack_name})
        server = stack.get("config", {}).get("server_id")
        if not isinstance(server, str) or not server:
            raise VerificationError(
                f"Komodo did not return server_id for {self.stack_name}; Read/Inspect permission is required"
            )
        for service in ("backend", "frontend"):
            image = self.release["base"][f"{service}_image"]
            inspected = self.komodo_read("InspectDockerImage", {"server": server, "image": image})
            image_id = inspected.get("id") or inspected.get("Id")
            if not isinstance(image_id, str) or not image_id:
                raise VerificationError(f"Canonical {service} base has no image ID: {image}")
            print(f"Verified canonical {service} base: {image} ({image_id})")

    def inspect_service(self, service: str) -> None:
        expected = self.release[service]
        inspected = self.komodo_read(
            "InspectStackContainer",
            {"stack": self.stack_name, "service": service},
        )
        config = inspected.get("config") or {}
        state = inspected.get("state") or {}
        labels = config.get("labels") or {}
        actual = {
            "image": config.get("image"),
            "image_id": inspected.get("image"),
            "running": state.get("running", False),
            "revision": labels.get(REVISION_LABEL),
            "overlay_sha256": labels.get(OVERLAY_LABEL),
        }
        matches = (
            actual["image"] == expected["image"]
            and isinstance(actual["image_id"], str)
            and bool(actual["image_id"])
            and actual["running"] is True
            and actual["revision"] == expected["revision"]
            and actual["overlay_sha256"] == expected["overlay_sha256"]
        )
        if not matches:
            raise VerificationError(
                f"{service} mismatch: expected image={expected['image']} revision={expected['revision']} "
                f"overlay={expected['overlay_sha256']}; actual={json.dumps(actual, sort_keys=True)}"
            )

    def expected_public_release(self) -> dict[str, Any]:
        return {
            "schema_version": self.release["schema_version"],
            "backend": {
                key: self.release["backend"][key]
                for key in ("image", "overlay_sha256", "revision")
            },
            "frontend": {
                key: self.release["frontend"][key]
                for key in ("image", "overlay_sha256", "revision")
            },
        }

    def verify_public(self) -> None:
        nonce = f"{os.environ.get('GITHUB_RUN_ID', 'manual')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '0')}-{time.time_ns()}"
        query = urllib.parse.urlencode({"verify": nonce})
        health = self.request_json(f"{self.public_url}/api/v1/health?{query}", None)
        if health.get("status") != "ok":
            raise VerificationError(f"Public health response is not healthy: {health}")
        actual_release = self.request_json(f"{self.public_url}/demo/release.json?{query}", None)
        expected_release = self.expected_public_release()
        if actual_release != expected_release:
            raise VerificationError(
                f"Public release mismatch: expected={json.dumps(expected_release, sort_keys=True)} "
                f"actual={json.dumps(actual_release, sort_keys=True)}"
            )

    def verify_once(self) -> None:
        self.inspect_service("backend")
        self.inspect_service("frontend")
        self.verify_public()

    def postdeploy(self, attempts: int = 30, delay_seconds: float = 10) -> None:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self.verify_once()
                print("Live vova-medcenter release matches release.json.")
                return
            except Exception as error:  # Retry transient API, container, and public-routing states.
                last_error = error
                print(f"Live verification attempt {attempt}/{attempts} failed: {error}", file=sys.stderr)
                if attempt < attempts:
                    time.sleep(delay_seconds)
        raise VerificationError(f"Timed out waiting for the declared vova-medcenter release: {last_error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "postdeploy"))
    parser.add_argument(
        "release",
        nargs="?",
        type=Path,
        default=Path("komodo/stacks/vova-medcenter/release.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("KOMODO_API_KEY")
    api_secret = os.environ.get("KOMODO_API_SECRET")
    komodo_url = os.environ.get("KOMODO_URL")
    if not api_key or not api_secret or not komodo_url:
        raise VerificationError("KOMODO_API_KEY, KOMODO_API_SECRET, and KOMODO_URL are required")
    release = json.loads(args.release.read_text(encoding="utf-8"))
    client = JsonHttpClient(api_key, api_secret)
    verifier = LiveVerifier(
        release,
        komodo_url,
        os.environ.get("VOVA_PUBLIC_URL", "https://vova-medcenter.ravil.space"),
        client.request,
        os.environ.get("VOVA_STACK_NAME", "vova-medcenter"),
    )
    if args.phase == "preflight":
        verifier.preflight()
    else:
        verifier.postdeploy(
            attempts=int(os.environ.get("VERIFY_ATTEMPTS", "30")),
            delay_seconds=float(os.environ.get("VERIFY_DELAY_SECONDS", "10")),
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        raise SystemExit(1)

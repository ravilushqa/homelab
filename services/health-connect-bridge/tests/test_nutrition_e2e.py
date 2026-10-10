"""
End-to-end subprocess smoke test for nutrition write-back.

Installs into an isolated HOME directory, starts the receiver subprocess
on a random high port, exercises the full flow via installed CLI binary
and real HTTP requests. No production writes, no production network.

SYNTHETIC — all data fabricated; isolated HOME; no production service.

Flow tested:
  1. Install (--nutrition) into isolated HOME
  2. Verify CLI binary installed and --help works
  3. Start receiver subprocess (random port, isolated DBs)
  4. Create entry via CLI → empty queue
  5. Wrong-version confirm attempt → rejected
  6. Confirm → record in queue (HTTP GET)
  7. Edit → queue empty (draft), version incremented
  8. Reconfirm → queue has higher version
  9. Stale ack → 409
  10. Correct ack → 200 acked
  11. Repeated ack → 200 idempotent
  12. Queue empty after ack
  13. Cancel another entry before fetch → no delivery warning
  14. Auth separation: ingest token can't read nutrition
  15. Stop receiver, re-start, queue still empty (persistence)
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_INSTALL_SH = _REPO_ROOT / "services/health-connect-bridge/install.sh"
_SOURCE_DIR = _REPO_ROOT / "services/health-connect-bridge"


def _free_port() -> int:
    """Find a free local port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_installer(fake_home: str, extra_args: list = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": fake_home}
    args = ["bash", str(_INSTALL_SH), "--source-dir", str(_SOURCE_DIR)]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(args, capture_output=True, text=True, env=env,
                          cwd=str(_REPO_ROOT), timeout=60)


def _http_request(host: str, port: int, method: str, path: str,
                  headers: dict = None, body: bytes = None) -> tuple:
    conn = HTTPConnection(host, port, timeout=10)
    h = headers or {}
    if body is not None:
        h["Content-Length"] = str(len(body))
    else:
        body = b""
        h.setdefault("Content-Length", "0")
    conn.request(method, path, body=body, headers=h)
    resp = conn.getresponse()
    status = resp.status
    data = resp.read()
    all_headers = dict(resp.getheaders())
    conn.close()
    try:
        body_json = json.loads(data)
    except Exception:
        body_json = {}
    return status, body_json, all_headers


class TestNutritionE2E(unittest.TestCase):
    """Full subprocess smoke test. Uses installed CLI binary + live receiver."""

    @classmethod
    def setUpClass(cls):
        cls.fake_home = tempfile.mkdtemp(prefix="test_e2e_home_")
        cls.log_lines = []

        # Install with nutrition
        result = _run_installer(cls.fake_home, ["--nutrition"])
        if result.returncode != 0:
            raise RuntimeError(
                f"Installer failed:\n{result.stdout}\n{result.stderr}"
            )
        cls.log_lines.append(f"Installer stdout: {result.stdout[:500]}")

        cls.install_base = Path(cls.fake_home) / ".hermes/profiles/health/workspace/health-connect-bridge"
        cls.bin_dir = cls.install_base / "bin"
        cls.health_db = cls.install_base / "data" / "health.sqlite3"
        cls.nutrition_db = cls.install_base / "data" / "nutrition.sqlite3"
        cls.ingest_token = (cls.install_base / "secrets" / "ingest-token").read_text().strip()
        cls.nutrition_token = (cls.install_base / "secrets" / "nutrition-token").read_text().strip()

        cls.port = _free_port()
        cls.host = "127.0.0.1"

        # Start receiver subprocess
        code_parent = cls.install_base / "code"
        cls.receiver_proc = subprocess.Popen(
            [
                sys.executable, "-m", "health_connect_bridge.receiver",
                "--host", cls.host,
                "--port", str(cls.port),
                "--token-path", str(cls.install_base / "secrets" / "ingest-token"),
                "--db-path", str(cls.health_db),
                "--nutrition-token-path", str(cls.install_base / "secrets" / "nutrition-token"),
                "--nutrition-db-path", str(cls.nutrition_db),
            ],
            env={**os.environ, "PYTHONPATH": str(code_parent), "HOME": cls.fake_home},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        # Wait for receiver to start
        for _ in range(30):
            try:
                status, _, _ = _http_request(cls.host, cls.port, "GET", "/healthz")
                if status == 200:
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
        else:
            cls.receiver_proc.terminate()
            raise RuntimeError("Receiver did not start in time")

        cls.log_lines.append(f"Receiver started on port {cls.port}")

    @classmethod
    def tearDownClass(cls):
        if cls.receiver_proc and cls.receiver_proc.poll() is None:
            cls.receiver_proc.terminate()
            try:
                cls.receiver_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.receiver_proc.kill()

        # Save log to scratch
        log_path = Path("/home/claw/.hermes/profiles/homelab/cache/scratch/nutrition-e2e-smoke.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w") as f:
            f.write("\n".join(cls.log_lines))

        # Cleanup
        import shutil
        try:
            shutil.rmtree(cls.fake_home, ignore_errors=True)
        except Exception:
            pass

    def _cli(self, args: list, stdin_bytes: bytes = None) -> tuple:
        """Run installed nutrition-queue CLI binary."""
        cli_bin = self.bin_dir / "nutrition-queue"
        env = {**os.environ, "HOME": self.fake_home}
        result = subprocess.run(
            [str(cli_bin)] + args,
            input=stdin_bytes,
            capture_output=True, text=False,
            env=env, timeout=15,
        )
        stdout_str = result.stdout.decode("utf-8", errors="replace")
        stderr_str = result.stderr.decode("utf-8", errors="replace")
        self.log_lines.append(f"CLI {args[:3]}: rc={result.returncode}")
        return result.returncode, stdout_str, stderr_str

    def _cli_json(self, args: list, input_data: dict = None) -> tuple:
        stdin = json.dumps(input_data).encode() if input_data else None
        rc, out, err = self._cli(args, stdin_bytes=stdin)
        try:
            out_json = json.loads(out) if out.strip() else {}
        except Exception:
            out_json = {}
        return rc, out_json, err

    def _minimal_data(self, **overrides) -> dict:
        d = {
            "name": "E2E Synthetic Lunch",
            "start_time": "2026-01-15T12:00:00+00:00",
            "end_time": "2026-01-15T12:30:00+00:00",
            "energy_kcal": 500.0,
            "protein_g": 30.0,
            "carbohydrate_g": 60.0,
            "fat_g": 15.0,
        }
        d.update(overrides)
        return d

    # ── Test 0: CLI help ─────────────────────────────────────────────────────

    def test_00_cli_binary_exists_and_help(self):
        cli_bin = self.bin_dir / "nutrition-queue"
        self.assertTrue(cli_bin.exists(), f"CLI binary not found: {cli_bin}")
        self.assertTrue(os.access(str(cli_bin), os.X_OK), "CLI binary not executable")

        rc, out, err = self._cli(["--help"])
        # argparse --help exits with code 0
        self.assertEqual(rc, 0, f"--help failed: {err}")
        self.assertIn("create", out + err)
        self.assertIn("confirm", out + err)
        self.log_lines.append(f"CLI help output (first 300): {(out+err)[:300]}")

    # ── Test 1: Create and empty queue ───────────────────────────────────────

    def test_01_create_and_empty_queue(self):
        rc, out_json, err = self._cli_json(["create"], input_data=self._minimal_data())
        self.assertEqual(rc, 0, f"create failed: {err}")
        self.assertIn("client_record_id", out_json)
        self.assertEqual(out_json.get("state"), "draft")
        self.__class__.crid1 = out_json["client_record_id"]
        self.log_lines.append(f"Created crid1={self.crid1}")

        # Queue should be empty (not confirmed yet)
        status, body, hdrs = _http_request(
            self.host, self.port, "GET", "/nutrition/queue",
            headers={"X-Nutrition-Token": self.nutrition_token}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("records"), [])

    # ── Test 2: Wrong version confirm rejected ───────────────────────────────

    def test_02_wrong_version_confirm_rejected(self):
        from ..nutrition_queue import CONFIRM_AFFIRM_PHRASE
        rc, out_json, err = self._cli_json([
            "confirm", self.crid1,
            "--version", "99",
            "--affirm", CONFIRM_AFFIRM_PHRASE,
            "--confirmer", "alice",
        ])
        self.assertNotEqual(rc, 0, "Should fail with wrong version")

    # ── Test 3: Confirm and check queue ──────────────────────────────────────

    def test_03_confirm_and_queue(self):
        from ..nutrition_queue import CONFIRM_AFFIRM_PHRASE
        rc, out_json, err = self._cli_json([
            "confirm", self.crid1,
            "--version", "1",
            "--affirm", CONFIRM_AFFIRM_PHRASE,
            "--confirmer", "alice",
        ])
        self.assertEqual(rc, 0, f"confirm failed: {err}")
        self.assertEqual(out_json.get("state"), "confirmed")

        status, body, hdrs = _http_request(
            self.host, self.port, "GET", "/nutrition/queue",
            headers={"X-Nutrition-Token": self.nutrition_token}
        )
        self.assertEqual(status, 200)
        records = body.get("records", [])
        crids = [r["client_record_id"] for r in records]
        self.assertIn(self.crid1, crids)
        r = next(r for r in records if r["client_record_id"] == self.crid1)
        self.assertEqual(r["client_record_version"], 1)
        # Verify locked schema keys
        for key in ("client_record_id", "client_record_version", "name",
                    "start_time", "end_time", "energy_kcal", "protein_g",
                    "carbohydrate_g", "fat_g", "description"):
            self.assertIn(key, r)
        # Cache-Control header
        cc = hdrs.get("Cache-Control", "")
        self.assertIn("no-store", cc)

    # ── Test 4: Edit resets confirmation ─────────────────────────────────────

    def test_04_edit_resets_confirmation(self):
        rc, out_json, err = self._cli_json(
            ["edit", self.crid1, "--version", "1"],
            input_data=self._minimal_data(name="Corrected Lunch")
        )
        self.assertEqual(rc, 0, f"edit failed: {err}")
        self.assertEqual(out_json.get("state"), "draft")
        self.assertEqual(out_json.get("client_record_version"), 2)

        status, body, _ = _http_request(
            self.host, self.port, "GET", "/nutrition/queue",
            headers={"X-Nutrition-Token": self.nutrition_token}
        )
        self.assertEqual(body.get("records"), [])  # draft, not in queue

    # ── Test 5: Reconfirm at v2 ──────────────────────────────────────────────

    def test_05_reconfirm_at_v2(self):
        from ..nutrition_queue import CONFIRM_AFFIRM_PHRASE
        rc, out_json, err = self._cli_json([
            "confirm", self.crid1,
            "--version", "2",
            "--affirm", CONFIRM_AFFIRM_PHRASE,
            "--confirmer", "alice",
        ])
        self.assertEqual(rc, 0, f"reconfirm failed: {err}")
        self.assertEqual(out_json.get("state"), "confirmed")

        status, body, _ = _http_request(
            self.host, self.port, "GET", "/nutrition/queue",
            headers={"X-Nutrition-Token": self.nutrition_token}
        )
        records = body.get("records", [])
        r = next((r for r in records if r["client_record_id"] == self.crid1), None)
        self.assertIsNotNone(r)
        self.assertEqual(r["client_record_version"], 2)

    # ── Test 6: Stale ack (v1) → 409 ────────────────────────────────────────

    def test_06_stale_ack_409(self):
        ack_body = json.dumps({
            "client_record_id": self.crid1,
            "client_record_version": 1,  # stale
        }).encode()
        status, body, _ = _http_request(
            self.host, self.port, "POST", "/nutrition/ack",
            headers={
                "Content-Type": "application/json",
                "X-Nutrition-Token": self.nutrition_token,
            },
            body=ack_body,
        )
        self.assertEqual(status, 409)

    # ── Test 7: Correct ack (v2) → 200 ──────────────────────────────────────

    def test_07_correct_ack(self):
        ack_body = json.dumps({
            "client_record_id": self.crid1,
            "client_record_version": 2,
        }).encode()
        status, body, _ = _http_request(
            self.host, self.port, "POST", "/nutrition/ack",
            headers={
                "Content-Type": "application/json",
                "X-Nutrition-Token": self.nutrition_token,
            },
            body=ack_body,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("result"), "acked")

    # ── Test 8: Repeat ack → idempotent ─────────────────────────────────────

    def test_08_repeat_ack_idempotent(self):
        ack_body = json.dumps({
            "client_record_id": self.crid1,
            "client_record_version": 2,
        }).encode()
        status, body, _ = _http_request(
            self.host, self.port, "POST", "/nutrition/ack",
            headers={
                "Content-Type": "application/json",
                "X-Nutrition-Token": self.nutrition_token,
            },
            body=ack_body,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("result"), "idempotent")

    # ── Test 9: Queue empty after ack ────────────────────────────────────────

    def test_09_queue_empty_after_ack(self):
        status, body, _ = _http_request(
            self.host, self.port, "GET", "/nutrition/queue",
            headers={"X-Nutrition-Token": self.nutrition_token}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("records"), [])

    # ── Test 10: Status shows acked ──────────────────────────────────────────

    def test_10_status_shows_acked(self):
        rc, out_json, err = self._cli_json(["status", self.crid1])
        self.assertEqual(rc, 0, f"status failed: {err}")
        self.assertEqual(out_json.get("state"), "acked")
        self.assertEqual(out_json.get("delivery_possible"), 1)

    # ── Test 11: Cancel before fetch ─────────────────────────────────────────

    def test_11_cancel_before_fetch(self):
        # Create another entry and cancel it before it's ever fetched
        rc, out_json, err = self._cli_json(["create"], input_data=self._minimal_data())
        self.assertEqual(rc, 0)
        crid2 = out_json["client_record_id"]

        rc, out_json, err = self._cli_json(["cancel", crid2, "--version", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(out_json.get("state"), "cancelled")
        # No delivery warning since never fetched
        self.assertNotIn("cancellation_note", out_json)

    # ── Test 12: Auth separation (ingest token can't read nutrition) ─────────

    def test_12_auth_separation(self):
        # Ingest token cannot access nutrition queue
        status, _, _ = _http_request(
            self.host, self.port, "GET", "/nutrition/queue",
            headers={"X-Nutrition-Token": self.ingest_token}  # wrong token
        )
        self.assertIn(status, (400, 401))

        # Nutrition token cannot ingest
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        ingest_payload = json.dumps({"timestamp": ts}).encode()
        status, _, _ = _http_request(
            self.host, self.port, "POST", "/ingest/health-connect",
            headers={
                "Content-Type": "application/json",
                "X-Health-Token": self.nutrition_token,  # wrong token
            },
            body=ingest_payload,
        )
        self.assertEqual(status, 401)

    # ── Test 13: Restart persistence ────────────────────────────────────────

    def test_13_restart_persistence(self):
        """Create+confirm, stop receiver, restart, queue still has the record."""
        from ..nutrition_queue import CONFIRM_AFFIRM_PHRASE

        # Create and confirm a fresh entry
        rc, out_json, err = self._cli_json(["create"], input_data=self._minimal_data())
        self.assertEqual(rc, 0)
        crid3 = out_json["client_record_id"]
        rc, _, err = self._cli_json([
            "confirm", crid3, "--version", "1",
            "--affirm", CONFIRM_AFFIRM_PHRASE,
            "--confirmer", "alice",
        ])
        self.assertEqual(rc, 0)

        # Stop receiver
        self.receiver_proc.terminate()
        try:
            self.receiver_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.receiver_proc.kill()

        new_port = _free_port()
        code_parent = self.install_base / "code"
        self.__class__.receiver_proc = subprocess.Popen(
            [
                sys.executable, "-m", "health_connect_bridge.receiver",
                "--host", self.host,
                "--port", str(new_port),
                "--token-path", str(self.install_base / "secrets" / "ingest-token"),
                "--db-path", str(self.health_db),
                "--nutrition-token-path", str(self.install_base / "secrets" / "nutrition-token"),
                "--nutrition-db-path", str(self.nutrition_db),
            ],
            env={**os.environ, "PYTHONPATH": str(code_parent), "HOME": self.fake_home},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.__class__.port = new_port

        for _ in range(30):
            try:
                status, _, _ = _http_request(self.host, new_port, "GET", "/healthz")
                if status == 200:
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
        else:
            self.fail("Restarted receiver did not start in time")

        # Queue should still have crid3
        status, body, _ = _http_request(
            self.host, new_port, "GET", "/nutrition/queue",
            headers={"X-Nutrition-Token": self.nutrition_token}
        )
        self.assertEqual(status, 200)
        crids = [r["client_record_id"] for r in body.get("records", [])]
        self.assertIn(crid3, crids)
        self.log_lines.append(f"Restart test: crid3={crid3} found after restart on port {new_port}")


if __name__ == "__main__":
    unittest.main()

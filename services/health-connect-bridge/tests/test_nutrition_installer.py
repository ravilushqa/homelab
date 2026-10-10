"""
Nutrition installer tests.

SYNTHETIC — uses isolated HOME directory, no production writes, no service start.
Tests: nutrition token generation, DB paths, CLI wrapper, systemd unit update detection,
       opt-in behavior (existing ingest-only stays untouched without --nutrition).
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_INSTALL_SH = _REPO_ROOT / "services/health-connect-bridge/install.sh"


def _run_installer(extra_args: list = None, env_overrides: dict = None) -> subprocess.CompletedProcess:
    """Run install.sh with an isolated HOME directory."""
    fake_home = tempfile.mkdtemp(prefix="test_installer_home_")
    env = os.environ.copy()
    env["HOME"] = fake_home
    env["USER"] = "testuser"
    if env_overrides:
        env.update(env_overrides)
    args = ["bash", str(_INSTALL_SH), "--source-dir", str(_INSTALL_SH.parent)]
    if extra_args:
        args.extend(extra_args)
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO_ROOT),
        timeout=60,
    )
    result._fake_home = fake_home
    return result


class TestIngestOnlyInstaller(unittest.TestCase):
    """Existing ingest-only installer must work unchanged (backward compat)."""

    def test_installer_succeeds(self):
        result = _run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_creates_token(self):
        result = _run_installer()
        home = result._fake_home
        token_path = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/secrets/ingest-token"
        )
        self.assertTrue(token_path.exists(), f"Token not found at {token_path}")

    def test_installer_token_mode_0600(self):
        result = _run_installer()
        home = result._fake_home
        token_path = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/secrets/ingest-token"
        )
        if token_path.exists():
            mode = stat.S_IMODE(token_path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_installer_no_nutrition_without_flag(self):
        result = _run_installer()
        home = result._fake_home
        nutrition_token = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/secrets/nutrition-token"
        )
        self.assertFalse(
            nutrition_token.exists(),
            "Nutrition token must NOT be created without --nutrition flag"
        )

    def test_installer_idempotent(self):
        """Running installer twice must not fail."""
        result1 = _run_installer()
        self.assertEqual(result1.returncode, 0, result1.stderr)
        # Re-run in same home
        fake_home = result1._fake_home
        env = os.environ.copy()
        env["HOME"] = fake_home
        result2 = subprocess.run(
            ["bash", str(_INSTALL_SH), "--source-dir", str(_INSTALL_SH.parent)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(result2.returncode, 0, result2.stderr)

    def test_token_not_printed_to_stdout(self):
        """Token value must never appear in stdout."""
        result = _run_installer()
        home = result._fake_home
        token_path = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/secrets/ingest-token"
        )
        if token_path.exists():
            token_val = token_path.read_text().strip()
            self.assertNotIn(token_val, result.stdout)


class TestNutritionInstaller(unittest.TestCase):
    """--nutrition flag creates nutrition token and updates systemd unit."""

    def test_nutrition_flag_creates_nutrition_token(self):
        result = _run_installer(["--nutrition"])
        self.assertEqual(result.returncode, 0, result.stderr)
        home = result._fake_home
        nutrition_token = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/secrets/nutrition-token"
        )
        self.assertTrue(nutrition_token.exists(),
                        f"Nutrition token not created at {nutrition_token}")

    def test_nutrition_token_mode_0600(self):
        result = _run_installer(["--nutrition"])
        self.assertEqual(result.returncode, 0, result.stderr)
        home = result._fake_home
        nutrition_token = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/secrets/nutrition-token"
        )
        if nutrition_token.exists():
            mode = stat.S_IMODE(nutrition_token.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_nutrition_token_not_in_stdout(self):
        """Nutrition token value must never appear in stdout."""
        result = _run_installer(["--nutrition"])
        home = result._fake_home
        nutrition_token = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/secrets/nutrition-token"
        )
        if nutrition_token.exists():
            token_val = nutrition_token.read_text().strip()
            self.assertNotIn(token_val, result.stdout)

    def test_nutrition_tokens_differ_from_ingest_token(self):
        """Nutrition token and ingest token must have different values."""
        result = _run_installer(["--nutrition"])
        home = result._fake_home
        ingest_token_path = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/secrets/ingest-token"
        )
        nutrition_token_path = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/secrets/nutrition-token"
        )
        if ingest_token_path.exists() and nutrition_token_path.exists():
            ingest_val = ingest_token_path.read_text().strip()
            nutrition_val = nutrition_token_path.read_text().strip()
            self.assertNotEqual(ingest_val, nutrition_val,
                                "Ingest and nutrition tokens must differ")

    def test_nutrition_systemd_unit_has_nutrition_args(self):
        """Systemd unit with --nutrition must include --nutrition-token-path arg."""
        result = _run_installer(["--nutrition"])
        self.assertEqual(result.returncode, 0, result.stderr)
        home = result._fake_home
        unit_path = (
            Path(home) / ".config/systemd/user/health-connect-bridge.service"
        )
        if unit_path.exists():
            unit_text = unit_path.read_text()
            self.assertIn("--nutrition-token-path", unit_text)
            self.assertIn("--nutrition-db-path", unit_text)

    def test_nutrition_cli_wrapper_created(self):
        result = _run_installer(["--nutrition"])
        self.assertEqual(result.returncode, 0, result.stderr)
        home = result._fake_home
        cli_wrapper = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/bin/nutrition-queue"
        )
        self.assertTrue(cli_wrapper.exists(), f"Nutrition CLI wrapper not found: {cli_wrapper}")

    def test_nutrition_cli_wrapper_executable(self):
        result = _run_installer(["--nutrition"])
        home = result._fake_home
        cli_wrapper = (
            Path(home) /
            ".hermes/profiles/health/workspace/health-connect-bridge/bin/nutrition-queue"
        )
        if cli_wrapper.exists():
            mode = stat.S_IMODE(cli_wrapper.stat().st_mode)
            self.assertTrue(mode & 0o111, "Nutrition CLI wrapper must be executable")

    def test_nutrition_idempotent(self):
        """Running installer twice with --nutrition must not fail."""
        result1 = _run_installer(["--nutrition"])
        self.assertEqual(result1.returncode, 0, result1.stderr)
        fake_home = result1._fake_home
        env = os.environ.copy()
        env["HOME"] = fake_home
        result2 = subprocess.run(
            ["bash", str(_INSTALL_SH), "--source-dir", str(_INSTALL_SH.parent),
             "--nutrition"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(result2.returncode, 0, result2.stderr)

    def test_nutrition_update_detection(self):
        """
        Installer run without --nutrition creates non-nutrition unit.
        Re-run with --nutrition updates unit to include nutrition args.
        """
        # First run: ingest-only
        result1 = _run_installer()
        self.assertEqual(result1.returncode, 0, result1.stderr)
        fake_home = result1._fake_home

        unit_path = Path(fake_home) / ".config/systemd/user/health-connect-bridge.service"
        if unit_path.exists():
            unit_text1 = unit_path.read_text()
            self.assertNotIn("--nutrition-token-path", unit_text1)

        # Second run: with --nutrition (unit must be updated)
        env = os.environ.copy()
        env["HOME"] = fake_home
        result2 = subprocess.run(
            ["bash", str(_INSTALL_SH), "--source-dir", str(_INSTALL_SH.parent),
             "--nutrition"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(result2.returncode, 0, result2.stderr)

        if unit_path.exists():
            unit_text2 = unit_path.read_text()
            self.assertIn("--nutrition-token-path", unit_text2)


if __name__ == "__main__":
    unittest.main()

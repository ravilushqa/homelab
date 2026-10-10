"""
Privacy gate and compose validation tests.

Tests:
  - Source check: correct labels pass; comment-only / wrong-service / missing fail.
  - Exact regex validation with positive/negative URL samples.
  - Live SSH check with fixtures: ok / missing-nutrition / stale-live / log-drop-missing.
  - Malformed patterns fail (substring-only, wrong regex shape).
  - Gate CLI integration tests using isolated fixtures.
  - Deployment ordering in komodo-deploy.yaml.
  - Token/DB file security checks.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_GATE_SCRIPT = _REPO_ROOT / "services/health-connect-bridge/privacy_gate_check.py"
_FIXTURES_DIR = Path(__file__).parent / "fixtures/privacy_gate"

# Import gate functions for unit tests
sys.path.insert(0, str(_REPO_ROOT / "services/health-connect-bridge"))
from privacy_gate_check import (
    check_source,
    check_live,
    _parse_traefik_service_block,
    _normalize_compose_value,
    _validate_ignore_pattern,
    _REQUIRED_IGNORE_URLS,
    _NoDupKeyLoader,
)
import yaml as _yaml


class TestComposeNormalization(unittest.TestCase):
    def test_double_dollar_normalized(self):
        self.assertEqual(_normalize_compose_value("^/path(\\?.*)?$$"), "^/path(\\?.*)?$")

    def test_single_dollar_unchanged(self):
        self.assertEqual(_normalize_compose_value("no_dollar"), "no_dollar")

    def test_no_backslash_doubling(self):
        # PyYAML already unescapes \\? → \? before we receive it;
        # _normalize_compose_value must not double-unescape.
        self.assertEqual(_normalize_compose_value("\\?"), "\\?")


class TestYAMLDuplicateKeyRejection(unittest.TestCase):
    def test_duplicate_key_raises(self):
        yaml_text = "key: value1\nkey: value2\n"
        with self.assertRaises(_yaml.YAMLError):
            _yaml.load(yaml_text, Loader=_NoDupKeyLoader)

    def test_invalid_yaml_returns_parse_error(self):
        block = _parse_traefik_service_block("services:\n  traefik:\n    labels:\n      - [unclosed")
        self.assertIn("parse_error", block)

    def test_valid_yaml_no_error(self):
        yaml_text = "services:\n  traefik:\n    labels:\n      - key=value\n"
        block = _parse_traefik_service_block(yaml_text)
        self.assertNotIn("parse_error", block)


class TestYAMLServiceParsing(unittest.TestCase):
    """Verify label extraction is scoped to traefik service only."""

    def test_extracts_traefik_labels_only(self):
        # Raw string: \\\\ in the YAML source → \\ parsed by PyYAML → \? in value
        yaml = r"""
services:
  traefik:
    image: traefik:latest
    labels:
      - "traefik.enable=true"
      - "traefik.http.middlewares.umami.plugin.umami-feeder.ignoreURLs[0]=^/ingest/health-connect(\\?.*)?$$"
  whoami:
    labels:
      - "traefik.http.middlewares.umami.plugin.umami-feeder.ignoreURLs[0]=^/other-path$$"
"""
        block = _parse_traefik_service_block(yaml)
        vals = [l for l in block["labels"] if "ignoreURLs[0]" in l]
        self.assertEqual(len(vals), 1)
        self.assertIn("/ingest/health-connect", vals[0])
        # Must NOT include the whoami label
        for l in block["labels"]:
            self.assertNotIn("/other-path", l)

    def test_missing_traefik_service_returns_empty(self):
        yaml = """
services:
  whoami:
    labels:
      - "something=value"
"""
        block = _parse_traefik_service_block(yaml)
        self.assertEqual(block["labels"], [])

    def test_extracts_command_args(self):
        yaml = """
services:
  traefik:
    command:
      - --accesslog=true
      - --accesslog.fields.queryparameters.defaultmode=drop
      - --accesslog.fields.headers.defaultmode=drop
    labels:
      - "traefik.enable=true"
"""
        block = _parse_traefik_service_block(yaml)
        self.assertTrue(any("queryparameters.defaultmode=drop" in a for a in block["command"]))
        self.assertTrue(any("headers.defaultmode=drop" in a for a in block["command"]))


class TestPatternValidation(unittest.TestCase):
    def test_correct_pattern_passes(self):
        suffix, expected, pos, neg = _REQUIRED_IGNORE_URLS[1]
        ok, msg = _validate_ignore_pattern(expected, suffix, expected, pos, neg)
        self.assertTrue(ok, msg)

    def test_substring_only_pattern_fails(self):
        suffix, expected, pos, neg = _REQUIRED_IGNORE_URLS[1]
        # "/nutrition/queue" without anchors matches more broadly → different from expected
        ok, msg = _validate_ignore_pattern("/nutrition/queue", suffix, expected, pos, neg)
        self.assertFalse(ok, "Substring-only pattern must fail — not equal to expected")

    def test_malformed_regex_fails(self):
        suffix, expected, pos, neg = _REQUIRED_IGNORE_URLS[1]
        ok, msg = _validate_ignore_pattern("[invalid(regex", suffix, expected, pos, neg)
        self.assertFalse(ok)

    def test_positive_sample_must_match(self):
        # Pattern that doesn't match the query-string variant
        suffix, expected, pos, neg = _REQUIRED_IGNORE_URLS[1]
        bad = r"^/nutrition/queue$"  # no query string support
        ok, msg = _validate_ignore_pattern(bad, suffix, expected, pos, neg)
        self.assertFalse(ok, "Pattern missing query-string support must fail positive test")

    def test_negative_sample_must_not_match(self):
        suffix, expected, pos, neg = _REQUIRED_IGNORE_URLS[1]
        # A too-broad pattern that matches negative samples
        bad = r"^/nutrition/.*$"
        ok, msg = _validate_ignore_pattern(bad, suffix, expected, pos, neg)
        self.assertFalse(ok, "Too-broad pattern must fail negative test")


class TestSourceCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose_path = _REPO_ROOT / "komodo/stacks/traefik/compose.yaml"

    def test_actual_compose_passes(self):
        passed, msgs = check_source(str(self.compose_path))
        self.assertTrue(passed, "Actual traefik compose must pass:\n" + "\n".join(msgs))

    def test_missing_nutrition_queue_label_fails(self):
        text = self.compose_path.read_text()
        lines = text.splitlines()
        filtered = [l for l in lines if not ("ignoreURLs[1]" in l and "nutrition/queue" in l)]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("\n".join(filtered))
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed)
        finally:
            os.unlink(fpath)

    def test_comment_only_label_fails(self):
        """Path in comment only is not a label — must fail."""
        text = self.compose_path.read_text()
        lines = text.splitlines()
        new_lines = []
        for line in lines:
            if "ignoreURLs[1]" in line and "nutrition/queue" in line and not line.strip().startswith("#"):
                new_lines.append("      # (disabled) " + line.strip())
            elif "ignoreURLs[2]" in line and "nutrition/ack" in line and not line.strip().startswith("#"):
                new_lines.append("      # (disabled) " + line.strip())
            else:
                new_lines.append(line)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("\n".join(new_lines))
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed, "Comment-only labels must fail source check")
        finally:
            os.unlink(fpath)

    def test_missing_accesslog_drop_fails(self):
        text = self.compose_path.read_text()
        modified = text.replace(
            "--accesslog.fields.queryparameters.defaultmode=drop",
            "--accesslog.fields.queryparameters.defaultmode=keep"
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed)
        finally:
            os.unlink(fpath)

    def test_wrong_service_label_not_counted(self):
        """Labels on a different service must not satisfy the gate."""
        yaml = """
services:
  traefik:
    command:
      - --accesslog.fields.queryparameters.defaultmode=drop
      - --accesslog.fields.headers.defaultmode=drop
    labels:
      - "traefik.enable=true"
  whoami:
    labels:
      - "traefik.http.middlewares.umami.plugin.umami-feeder.ignoreURLs[0]=^/ingest/health-connect(\\?.*)?$$"
      - "traefik.http.middlewares.umami.plugin.umami-feeder.ignoreURLs[1]=^/nutrition/queue(\\?.*)?$$"
      - "traefik.http.middlewares.umami.plugin.umami-feeder.ignoreURLs[2]=^/nutrition/ack(\\?.*)?$$"
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed, "Labels on wrong service must not count")
        finally:
            os.unlink(fpath)

    def test_conflicting_defaultmode_keep_fails(self):
        """A conflicting --defaultmode=keep after =drop must fail source check."""
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        # Insert the conflicting arg inside the traefik command block (after the headers drop line)
        # so it is parsed as a traefik.command entry, not as an unrelated YAML section.
        modified = text.replace(
            "      - --accesslog.fields.headers.defaultmode=drop",
            "      - --accesslog.fields.headers.defaultmode=drop\n"
            "      - --accesslog.fields.queryparameters.defaultmode=keep",
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed, "Conflicting defaultmode=keep must fail source check")
        finally:
            os.unlink(fpath)

    def test_per_field_header_keep_in_command_fails(self):
        """Per-field headers.names.*=keep in traefik command must fail source check."""
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        modified = text.replace(
            "      - --accesslog.fields.headers.defaultmode=drop",
            "      - --accesslog.fields.headers.defaultmode=drop\n"
            "      - --accesslog.fields.headers.names.X-Nutrition-Token=keep",
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed,
                "Per-field header keep override must fail source check")
        finally:
            os.unlink(fpath)

    def test_per_field_qp_redact_in_command_fails(self):
        """Per-field queryparameters.names.*=redact in traefik command must fail."""
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        modified = text.replace(
            "      - --accesslog.fields.queryparameters.defaultmode=drop",
            "      - --accesslog.fields.queryparameters.defaultmode=drop\n"
            "      - --accesslog.fields.queryparameters.names.token=redact",
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed,
                "Per-field queryparameter redact override must fail source check")
        finally:
            os.unlink(fpath)

    def test_conflicting_defaultmode_redact_fails(self):
        """--accesslog.fields.*.defaultmode=redact must fail source check (same as keep)."""
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        modified = text.replace(
            "      - --accesslog.fields.queryparameters.defaultmode=drop",
            "      - --accesslog.fields.queryparameters.defaultmode=drop\n"
            "      - --accesslog.fields.queryparameters.defaultmode=redact",
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed,
                "Conflicting defaultmode=redact must fail source check")
        finally:
            os.unlink(fpath)

    def test_duplicate_defaultmode_drop_fails(self):
        """Duplicate --accesslog.fields.*.defaultmode=drop must fail source check."""
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        modified = text.replace(
            "      - --accesslog.fields.queryparameters.defaultmode=drop",
            "      - --accesslog.fields.queryparameters.defaultmode=drop\n"
            "      - --accesslog.fields.queryparameters.defaultmode=drop",
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed,
                "Duplicate defaultmode=drop must fail source check (ambiguous)")
        finally:
            os.unlink(fpath)

    def test_per_field_unknown_mode_fails(self):
        """Per-field setting with unknown mode (not =drop) must fail source check."""
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        modified = text.replace(
            "      - --accesslog.fields.queryparameters.defaultmode=drop",
            "      - --accesslog.fields.queryparameters.defaultmode=drop\n"
            "      - --accesslog.fields.queryparameters.names.token=unknown",
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed,
                "Per-field unknown mode must fail source check")
        finally:
            os.unlink(fpath)

    def test_per_field_drop_allowed(self):
        """Per-field =drop override must NOT fail source check (it reinforces defaultmode)."""
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        modified = text.replace(
            "      - --accesslog.fields.queryparameters.defaultmode=drop",
            "      - --accesslog.fields.queryparameters.defaultmode=drop\n"
            "      - --accesslog.fields.queryparameters.names.token=drop",
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertTrue(passed,
                "Per-field =drop override must be allowed:\n" + "\n".join(msgs))
        finally:
            os.unlink(fpath)

    def test_invalid_yaml_fails(self):
        """Malformed YAML (invalid syntax) must fail source check."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("services:\n  traefik:\n    labels:\n      - [unclosed\n")
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed, "Invalid YAML must fail")
        finally:
            os.unlink(fpath)

    def test_duplicate_yaml_key_fails(self):
        """Duplicate key in YAML must fail source check."""
        yaml = """
services:
  traefik:
    command:
      - --accesslog.fields.queryparameters.defaultmode=drop
      - --accesslog.fields.headers.defaultmode=drop
    labels:
      - "traefik.enable=true"
services:
  whoami:
    image: traefik/whoami
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed, "Duplicate YAML key must fail")
        finally:
            os.unlink(fpath)


class TestLiveSSHFixtures(unittest.TestCase):
    def test_ok_fixture_passes(self):
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_ok.txt"),
        )
        self.assertTrue(passed, "\n".join(msgs))

    def test_missing_nutrition_fixture_fails(self):
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_missing_nutrition.txt"),
        )
        self.assertFalse(passed, "Missing nutrition patterns must fail")

    def test_stale_live_fails(self):
        """Stale running container (no nutrition exclusions) must fail."""
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_stale_live.txt"),
        )
        self.assertFalse(passed)

    def test_missing_log_drops_fails(self):
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_missing_log_drops.txt"),
        )
        self.assertFalse(passed, "Missing log drop flags must fail")

    def test_malformed_pattern_fails(self):
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_malformed_pattern.txt"),
        )
        self.assertFalse(passed, "Malformed/wrong pattern must fail")

    def test_empty_fixture_fails_closed(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("")
            fpath = f.name
        try:
            passed, msgs = check_live(host="(fixture)", fixture_path=fpath)
            self.assertFalse(passed)
        finally:
            os.unlink(fpath)

    def test_no_fixture_no_host_fails_closed(self):
        """No fixture and unreachable host → fail closed."""
        passed, msgs = check_live(host="192.0.2.1")  # TEST-NET, unreachable
        self.assertFalse(passed)

    def test_stopped_container_fails(self):
        """Stopped container (running=false) must fail even with correct labels."""
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_stopped.txt"),
        )
        self.assertFalse(passed, "Stopped container must fail live gate")
        self.assertTrue(any("not running" in m or "running=false" in m or "CLOSED" in m
                            for m in msgs), f"Expected running failure message, got: {msgs}")

    def test_field_override_fails(self):
        """Per-field accesslog override (qp_override=true) must fail."""
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_with_overrides.txt"),
        )
        self.assertFalse(passed, "Override flag must fail live gate")

    def test_missing_override_keys_fails_closed(self):
        """SSH output missing qp_override/hd_override keys must fail closed."""
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_missing_override_keys.txt"),
        )
        self.assertFalse(passed,
            "Missing override evidence must fail closed (cannot confirm absence)")
        # Must explicitly indicate it is failing closed, not a false ok
        self.assertTrue(any("CLOSED" in m or "missing" in m.lower() for m in msgs),
            f"Expected fail-closed message, got: {msgs}")

    def test_conflict_mode_fails(self):
        """SSH output with qp_conflict=true must fail live gate."""
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_conflict_mode.txt"),
        )
        self.assertFalse(passed, "Conflicting defaultmode must fail live gate")
        self.assertTrue(any("conflict" in m.lower() for m in msgs),
            f"Expected conflict message, got: {msgs}")

    def test_dup_drop_fails(self):
        """SSH output with qp_dup_drop=true must fail live gate."""
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_dup_drop.txt"),
        )
        self.assertFalse(passed, "Duplicate drop flag must fail live gate")

    def test_duplicate_ssh_keys_fail_closed(self):
        """SSH output with duplicate keys must fail closed (injection/extraction bug)."""
        passed, msgs = check_live(
            host="(fixture)",
            fixture_path=str(_FIXTURES_DIR / "ssh_duplicate_keys.txt"),
        )
        self.assertFalse(passed, "Duplicate SSH keys must fail closed")
        self.assertTrue(any("duplicate" in m.lower() or "CLOSED" in m for m in msgs),
            f"Expected fail-closed duplicate message, got: {msgs}")


class TestGateCLI(unittest.TestCase):
    """Run gate script as subprocess."""

    def _run_gate(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_GATE_SCRIPT)] + args,
            capture_output=True, text=True, timeout=30,
        )

    def test_gate_passes_with_ok_fixture(self):
        result = self._run_gate([
            "--source", str(_REPO_ROOT / "komodo/stacks/traefik/compose.yaml"),
            "--ssh-fixture", str(_FIXTURES_DIR / "ssh_ok.txt"),
        ])
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)

    def test_gate_fails_with_stale_live(self):
        result = self._run_gate([
            "--source", str(_REPO_ROOT / "komodo/stacks/traefik/compose.yaml"),
            "--ssh-fixture", str(_FIXTURES_DIR / "ssh_stale_live.txt"),
        ])
        self.assertNotEqual(result.returncode, 0)

    def test_gate_fails_without_live_check(self):
        result = self._run_gate([
            "--source", str(_REPO_ROOT / "komodo/stacks/traefik/compose.yaml"),
        ])
        self.assertNotEqual(result.returncode, 0)

    def test_gate_source_comment_only_rejected(self):
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        lines = text.splitlines()
        new_lines = []
        for line in lines:
            if "ignoreURLs[1]" in line and "nutrition/queue" in line and not line.strip().startswith("#"):
                new_lines.append("      # disabled: " + line.strip())
            else:
                new_lines.append(line)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("\n".join(new_lines))
            fpath = f.name
        try:
            result = self._run_gate([
                "--source", fpath,
                "--ssh-fixture", str(_FIXTURES_DIR / "ssh_ok.txt"),
            ])
            self.assertNotEqual(result.returncode, 0)
        finally:
            os.unlink(fpath)

    def test_live_gate_must_fail_today(self):
        """
        Production live gate MUST fail today because nutrition exclusions
        are not yet deployed to the running Traefik instance.
        The stale_live fixture represents the known current production state:
        ingest exclusion exists but nutrition exclusions are empty.
        """
        result = self._run_gate([
            "--source", str(_REPO_ROOT / "komodo/stacks/traefik/compose.yaml"),
            "--ssh-fixture", str(_FIXTURES_DIR / "ssh_stale_live.txt"),
        ])
        self.assertNotEqual(result.returncode, 0,
                             "Live gate must fail today (nutrition not deployed)")


class TestDeployWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = (_REPO_ROOT / ".github/workflows/komodo-deploy.yaml").read_text()

    def test_traefik_gates_before_hcb(self):
        gate_pos = self.wf.find('deploy_and_wait "traefik"')
        loop_pos = self.wf.find('[ "$STACK" = "traefik" ] && continue')
        self.assertGreater(gate_pos, -1)
        self.assertLess(gate_pos, loop_pos)

    def test_hcb_always_deploys_traefik_first(self):
        self.assertIn('grep -qw "health-connect-bridge"', self.wf)
        self.assertIn('deploy_and_wait "traefik"', self.wf)

    def test_gate_script_invoked(self):
        self.assertIn("privacy_gate_check.py", self.wf)

    def test_live_host_in_gate(self):
        self.assertIn("--live-host", self.wf)

    def test_ssh_user_present(self):
        # Gate should specify SSH user
        self.assertIn("--ssh-user", self.wf)


class TestTokenSecurity(unittest.TestCase):
    def test_symlink_rejected(self):
        from ..receiver import _load_token
        fd, real = tempfile.mkstemp(prefix="test_tok_")
        os.write(fd, b"a" * 40)
        os.close(fd)
        os.chmod(real, 0o600)
        link = real + "_link"
        try:
            os.symlink(real, link)
            with self.assertRaises(RuntimeError):
                _load_token(Path(link))
        finally:
            os.unlink(link)
            os.unlink(real)

    def test_wrong_mode_rejected(self):
        from ..receiver import _load_token
        fd, path = tempfile.mkstemp(prefix="test_tok_mode_")
        os.write(fd, b"a" * 40)
        os.close(fd)
        os.chmod(path, 0o644)
        try:
            with self.assertRaises(RuntimeError):
                _load_token(Path(path))
        finally:
            os.unlink(path)

    def test_valid_token_loads(self):
        from ..receiver import _load_token
        fd, path = tempfile.mkstemp(prefix="test_tok_valid_")
        token_val = b"a" * 48
        os.write(fd, token_val)
        os.close(fd)
        os.chmod(path, 0o600)
        try:
            loaded = _load_token(Path(path))
            self.assertEqual(loaded, token_val)
        finally:
            os.unlink(path)


class TestSourceDefaultmodeUnknown(unittest.TestCase):
    """
    Regression: source check must reject --accesslog.fields.*.defaultmode=unknown
    (and any non-drop value) not just keep/redact.
    """

    def _compose_with_extra_cmd(self, extra_cmd: str) -> str:
        """Insert extra_cmd into the traefik command block of the actual compose."""
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        return text.replace(
            "      - --accesslog.fields.headers.defaultmode=drop",
            "      - --accesslog.fields.headers.defaultmode=drop\n"
            f"      {extra_cmd}",
        )

    def test_defaultmode_unknown_fails(self):
        """--accesslog.fields.headers.defaultmode=unknown must fail source check."""
        modified = self._compose_with_extra_cmd(
            "- --accesslog.fields.headers.defaultmode=unknown"
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed, "defaultmode=unknown must fail source check")
        finally:
            os.unlink(fpath)

    def test_defaultmode_unknown_qp_fails(self):
        """--accesslog.fields.queryparameters.defaultmode=unknown must fail source check."""
        text = (_REPO_ROOT / "komodo/stacks/traefik/compose.yaml").read_text()
        modified = text.replace(
            "      - --accesslog.fields.queryparameters.defaultmode=drop",
            "      - --accesslog.fields.queryparameters.defaultmode=drop\n"
            "      - --accesslog.fields.queryparameters.defaultmode=unknown",
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(modified)
            fpath = f.name
        try:
            passed, msgs = check_source(fpath)
            self.assertFalse(passed, "defaultmode=unknown on qp must fail source check")
        finally:
            os.unlink(fpath)

    def test_defaultmode_drop_only_passes(self):
        """Actual compose with only =drop must pass (sanity check for this class)."""
        passed, msgs = check_source(
            str(_REPO_ROOT / "komodo/stacks/traefik/compose.yaml")
        )
        self.assertTrue(passed, "Actual compose must still pass: " + "\n".join(msgs))


class TestFakeDockerSSH(unittest.TestCase):
    """
    Regression: run the actual SSH extraction shell script with a fake 'docker'
    binary to verify correct handling of malformed JSON, failed inspects,
    conflicting modes, and duplicate drop flags.

    These tests exercise the exception fallback paths (formerly print('false'),
    now print('error')) and the extraction logic for conflict detection.
    """

    def _run_script_with_fake_docker(
        self, fake_docker_script: str, env_extra: dict = None
    ) -> tuple:
        """
        Run _SSH_EXTRACT_SCRIPT with a fake docker command in PATH.
        Returns (returncode, lines).
        """
        from privacy_gate_check import _SSH_EXTRACT_SCRIPT
        import shutil

        tmpdir = tempfile.mkdtemp(prefix="test_fakedk_")
        try:
            # Write fake docker binary
            fake_docker = os.path.join(tmpdir, "docker")
            with open(fake_docker, "w") as f:
                f.write("#!/bin/bash\n" + fake_docker_script + "\n")
            os.chmod(fake_docker, 0o700)

            env = dict(os.environ)
            env["PATH"] = tmpdir + ":" + env.get("PATH", "")
            if env_extra:
                env.update(env_extra)

            result = subprocess.run(
                ["bash", "-s"],
                input=_SSH_EXTRACT_SCRIPT,
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
            lines = result.stdout.splitlines()
            return result.returncode, lines
        finally:
            import shutil as _shutil
            _shutil.rmtree(tmpdir, ignore_errors=True)

    def _parse_output(self, lines: list) -> dict:
        from privacy_gate_check import _parse_ssh_output
        result, _ = _parse_ssh_output(lines)
        return result

    def test_malformed_json_fails_closed(self):
        """docker inspect returning malformed JSON must cause extraction error (not false)."""
        fake = r"""
echo "not valid json" >&2
# Return malformed JSON for Cmd
if [[ "$*" == *"json .Config.Cmd"* ]]; then
    printf 'this is not json\n'
elif [[ "$*" == *".State.Running"* ]]; then
    printf 'true\n'
elif [[ "$*" == *".Config.Labels"* ]]; then
    printf '^/ingest/health-connect(\?.*)?$\n'
else
    printf 'true\n'
fi
"""
        rc, lines = self._run_script_with_fake_docker(fake)
        data = self._parse_output(lines)
        # Extraction errors must yield 'error' not 'false'
        # The qp_drop, hd_drop, and override/conflict/dup keys should be 'error'
        for key in ("qp_drop", "hd_drop", "qp_override", "hd_override",
                    "qp_conflict", "hd_conflict", "qp_dup_drop", "hd_dup_drop"):
            if key in data:
                self.assertNotEqual(
                    data[key], "false",
                    f"{key}='false' is unsafe evidence for malformed JSON; expected 'error'"
                )

    def test_failed_inspect_fails_closed(self):
        """docker inspect failing (exit 1) must cause extraction error."""
        fake = r"""
if [[ "$*" == *"--format"* ]]; then
    exit 1  # simulate docker inspect failure
fi
"""
        rc, lines = self._run_script_with_fake_docker(fake)
        # Script itself might fail; check that any extracted keys are 'error' not 'false'
        data = self._parse_output(lines)
        for key in ("qp_drop", "hd_drop"):
            if key in data:
                self.assertNotEqual(data[key], "false",
                    f"{key}='false' hides failed docker inspect; expected 'error'")

    def test_conflicting_unknown_mode_detected(self):
        """docker inspect returning args with defaultmode=unknown must be flagged."""
        fake = r"""
if [[ "$*" == *".State.Running"* ]]; then
    printf 'true\n'
elif [[ "$*" == *"json .Config.Cmd"* ]]; then
    printf '["--accesslog.fields.queryparameters.defaultmode=unknown"]\n'
elif [[ "$*" == *".Config.Labels"* ]]; then
    printf '^/ingest/health-connect(\?.*)?$\n'
else
    printf 'false\n'
fi
"""
        rc, lines = self._run_script_with_fake_docker(fake)
        data = self._parse_output(lines)
        # qp_conflict must be 'true' since unknown != drop
        if "qp_conflict" in data:
            self.assertEqual(data["qp_conflict"], "true",
                "defaultmode=unknown must be detected as conflicting")

    def test_conflicting_keep_mode_detected(self):
        """docker inspect returning args with defaultmode=keep must be flagged."""
        fake = r"""
if [[ "$*" == *".State.Running"* ]]; then
    printf 'true\n'
elif [[ "$*" == *"json .Config.Cmd"* ]]; then
    printf '["--accesslog.fields.headers.defaultmode=keep"]\n'
elif [[ "$*" == *".Config.Labels"* ]]; then
    printf '^/ingest/health-connect(\?.*)?$\n'
else
    printf 'false\n'
fi
"""
        rc, lines = self._run_script_with_fake_docker(fake)
        data = self._parse_output(lines)
        if "hd_conflict" in data:
            self.assertEqual(data["hd_conflict"], "true",
                "defaultmode=keep must be detected as conflicting")

    def test_conflicting_redact_mode_detected(self):
        """docker inspect returning args with defaultmode=redact must be flagged."""
        fake = r"""
if [[ "$*" == *".State.Running"* ]]; then
    printf 'true\n'
elif [[ "$*" == *"json .Config.Cmd"* ]]; then
    printf '["--accesslog.fields.queryparameters.defaultmode=redact"]\n'
elif [[ "$*" == *".Config.Labels"* ]]; then
    printf '^/ingest/health-connect(\?.*)?$\n'
else
    printf 'false\n'
fi
"""
        rc, lines = self._run_script_with_fake_docker(fake)
        data = self._parse_output(lines)
        if "qp_conflict" in data:
            self.assertEqual(data["qp_conflict"], "true",
                "defaultmode=redact must be detected as conflicting")

    def test_duplicate_drop_detected(self):
        """docker inspect returning duplicate defaultmode=drop must be flagged."""
        fake = r"""
if [[ "$*" == *".State.Running"* ]]; then
    printf 'true\n'
elif [[ "$*" == *"json .Config.Cmd"* ]]; then
    printf '["--accesslog.fields.queryparameters.defaultmode=drop","--accesslog.fields.queryparameters.defaultmode=drop"]\n'
elif [[ "$*" == *".Config.Labels"* ]]; then
    printf '^/ingest/health-connect(\?.*)?$\n'
else
    printf 'false\n'
fi
"""
        rc, lines = self._run_script_with_fake_docker(fake)
        data = self._parse_output(lines)
        if "qp_dup_drop" in data:
            self.assertEqual(data["qp_dup_drop"], "true",
                "Duplicate drop flag must be detected")

    def test_extraction_error_check_live_fails_closed(self):
        """
        check_live with SSH output containing 'error' values must fail closed.
        """
        from privacy_gate_check import check_live
        # Fixture with 'error' in override evidence keys
        fixture_lines = (
            "running=true\n"
            "ignoreURLs[0]=^/ingest/health-connect(\\?.*)?$\n"
            "ignoreURLs[1]=^/nutrition/queue(\\?.*)?$\n"
            "ignoreURLs[2]=^/nutrition/ack(\\?.*)?$\n"
            "qp_drop=true\n"
            "hd_drop=true\n"
            "qp_override=error\n"
            "hd_override=false\n"
            "qp_conflict=false\n"
            "hd_conflict=false\n"
            "qp_dup_drop=false\n"
            "hd_dup_drop=false\n"
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(fixture_lines)
            fpath = f.name
        try:
            passed, msgs = check_live(host="(fixture)", fixture_path=fpath)
            self.assertFalse(passed,
                "SSH output with extraction 'error' must fail closed")
            self.assertTrue(
                any("CLOSED" in m or "unexpected" in m.lower() or "error" in m.lower()
                    for m in msgs),
                f"Expected fail-closed message for extraction error, got: {msgs}"
            )
        finally:
            os.unlink(fpath)


if __name__ == "__main__":
    unittest.main()

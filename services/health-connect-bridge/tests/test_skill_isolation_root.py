"""
Focused isolation-root unit tests for agent-workflow/nutrition_writeback.py.

Covers:
- Health scratch root accepted by _is_under_allowed_scratch
- Outside / ancestor paths rejected
- _check_isolation_root rejects symlink path and insecure mode (0755)
- Exact Russian consent text passes _validate_consent unchanged
- Negation, wrong format, internal affirm phrase, field mismatch all rejected

No production network, data, or secrets accessed.
Temp dirs created under health scratch (~/.hermes/profiles/health/cache/scratch).
"""

import importlib.util
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Load the standalone skill module without going through the package system
# ---------------------------------------------------------------------------
_SKILL_PATH = (
    Path(__file__).parent.parent / "agent-workflow" / "nutrition_writeback.py"
)
_spec = importlib.util.spec_from_file_location("_nutrition_writeback_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_is_under_allowed_scratch = _mod._is_under_allowed_scratch
_check_isolation_root = _mod._check_isolation_root
_validate_consent = _mod._validate_consent
_CONFIRM_AFFIRM = _mod._CONFIRM_AFFIRM

_HEALTH_SCRATCH = Path.home() / ".hermes/profiles/health/cache/scratch"


# ---------------------------------------------------------------------------
# Allowed scratch root acceptance / rejection
# ---------------------------------------------------------------------------

class TestAllowedScratchRoots(unittest.TestCase):
    def test_health_scratch_root_itself_accepted(self):
        self.assertTrue(_is_under_allowed_scratch(_HEALTH_SCRATCH))

    def test_health_scratch_subpath_accepted(self):
        sub = _HEALTH_SCRATCH / "test-session" / "isolation"
        self.assertTrue(_is_under_allowed_scratch(sub))

    def test_slash_tmp_rejected(self):
        self.assertFalse(_is_under_allowed_scratch(Path("/tmp/outside-path")))

    def test_home_root_rejected(self):
        self.assertFalse(_is_under_allowed_scratch(Path.home()))

    def test_partial_prefix_not_matched(self):
        # A path that starts with a similar string but is not under the root
        self.assertFalse(
            _is_under_allowed_scratch(
                Path.home() / ".hermes/profiles/health-extra/cache/scratch"
            )
        )


# ---------------------------------------------------------------------------
# _check_isolation_root: symlink and insecure-mode rejection
# ---------------------------------------------------------------------------

class TestCheckIsolationRoot(unittest.TestCase):
    def setUp(self):
        self._base = Path(tempfile.mkdtemp(dir=_HEALTH_SCRATCH, prefix="test_isoroot_"))
        os.chmod(self._base, 0o700)

    def tearDown(self):
        shutil.rmtree(self._base, ignore_errors=True)

    def test_symlink_root_rejected(self):
        real = self._base / "real"
        real.mkdir(mode=0o700)
        link = self._base / "link_to_real"
        link.symlink_to(real)
        errs = _check_isolation_root(link)
        self.assertTrue(errs, "expected errors for symlink root, got none")
        self.assertTrue(
            any("symlink" in e for e in errs),
            f"expected 'symlink' in error, got: {errs}",
        )

    def test_insecure_mode_rejected(self):
        d = self._base / "insecure"
        d.mkdir(mode=0o755)
        # umask (typically 077) prevents mkdir(mode=0755) from setting group/other
        # bits.  Explicitly chmod to 0755 to simulate an insecure root regardless
        # of the process umask.  This only affects the test fixture directory;
        # production security is unchanged.
        os.chmod(d, 0o755)
        errs = _check_isolation_root(d)
        self.assertTrue(errs, "expected errors for 0755 root, got none")
        self.assertTrue(
            any("0700" in e or "permission" in e.lower() or "insecure" in e.lower() for e in errs),
            f"expected permission error, got: {errs}",
        )

    def test_valid_root_under_health_scratch_accepted(self):
        d = self._base / "valid"
        d.mkdir(mode=0o700)
        errs = _check_isolation_root(d)
        self.assertEqual(errs, [], f"unexpected errors for valid health-scratch root: {errs}")

    def test_nonexistent_root_rejected(self):
        d = self._base / "does_not_exist"
        errs = _check_isolation_root(d)
        self.assertTrue(errs, "expected errors for non-existent root")


# ---------------------------------------------------------------------------
# _validate_consent: Russian grammar + rejection cases
# ---------------------------------------------------------------------------

class TestConsentValidation(unittest.TestCase):
    _CONFIRMER = "Тест"
    _MEAL = "Завтрак"
    _ID = "rec-001"
    _VERSION = 1

    def _ru(self, confirmer=None, meal=None, id_=None, version=None):
        c = confirmer if confirmer is not None else self._CONFIRMER
        m = meal if meal is not None else self._MEAL
        i = id_ if id_ is not None else self._ID
        v = version if version is not None else self._VERSION
        return (
            f"Я, {c}, подтверждаю запись Health Connect для "
            f"{m}, ID {i}, версия {v}, с показанным временем и макросами."
        )

    # ── passing ──────────────────────────────────────────────────────────────

    def test_exact_russian_consent_accepted(self):
        errs = _validate_consent(
            self._ru(), self._ID, self._VERSION, self._MEAL, self._CONFIRMER
        )
        self.assertEqual(errs, [], f"valid Russian consent rejected: {errs}")

    # ── rejection cases ───────────────────────────────────────────────────────

    def test_russian_negation_не_rejected(self):
        # Insert "не" — the canonical Russian negation word
        bad = (
            "Я, Тест, не подтверждаю запись Health Connect для Завтрак, "
            "ID rec-001, версия 1, с показанным временем и макросами."
        )
        errs = _validate_consent(bad, self._ID, self._VERSION, self._MEAL, self._CONFIRMER)
        self.assertTrue(errs, "negation (не) must be rejected")

    def test_empty_consent_rejected(self):
        errs = _validate_consent("", self._ID, self._VERSION, self._MEAL, self._CONFIRMER)
        self.assertTrue(errs, "empty consent must be rejected")

    def test_free_form_text_rejected(self):
        errs = _validate_consent(
            "Yes I confirm this", self._ID, self._VERSION, self._MEAL, self._CONFIRMER
        )
        self.assertTrue(errs, "free-form text must be rejected")

    def test_internal_affirm_phrase_rejected(self):
        errs = _validate_consent(
            _CONFIRM_AFFIRM, self._ID, self._VERSION, self._MEAL, self._CONFIRMER
        )
        self.assertTrue(errs, "internal affirm phrase must be rejected")

    def test_wrong_confirmer_rejected(self):
        errs = _validate_consent(
            self._ru(confirmer="Другой"), self._ID, self._VERSION, self._MEAL, self._CONFIRMER
        )
        self.assertTrue(errs, "confirmer mismatch must be rejected")

    def test_wrong_id_rejected(self):
        errs = _validate_consent(
            self._ru(id_="wrong-id"), self._ID, self._VERSION, self._MEAL, self._CONFIRMER
        )
        self.assertTrue(errs, "ID mismatch must be rejected")

    def test_wrong_version_rejected(self):
        errs = _validate_consent(
            self._ru(version=99), self._ID, self._VERSION, self._MEAL, self._CONFIRMER
        )
        self.assertTrue(errs, "version mismatch must be rejected")

    def test_wrong_meal_rejected(self):
        errs = _validate_consent(
            self._ru(meal="Ужин"), self._ID, self._VERSION, self._MEAL, self._CONFIRMER
        )
        self.assertTrue(errs, "meal name mismatch must be rejected")


if __name__ == "__main__":
    unittest.main()

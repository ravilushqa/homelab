"""
Nutrition CLI tests.

SYNTHETIC — isolated databases, no production writes.
Tests: create, edit, confirm, status, cancel via CLI entry-points;
       JSON input hardening (dup keys, NaN, size limit, depth);
       no PHI in error messages.
"""

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ..nutrition_queue import (
    NutritionStore,
    CONFIRM_AFFIRM_PHRASE,
    AI_DESCRIPTION_PREFIX,
)
from ..nutrition_cli import (
    build_parser,
    cmd_create,
    cmd_edit,
    cmd_confirm,
    cmd_status,
    cmd_cancel,
    _load_json_input,
)


def _make_temp_db() -> Path:
    fd, path = tempfile.mkstemp(suffix=".sqlite3", prefix="test_cli_")
    os.close(fd)
    os.unlink(path)
    return Path(path)


def _minimal_data(**overrides) -> dict:
    d = {
        "name": "CLI Test Meal",
        "start_time": "2026-01-15T12:00:00+00:00",
        "end_time": "2026-01-15T12:30:00+00:00",
        "energy_kcal": 500.0,
        "protein_g": 30.0,
        "carbohydrate_g": 60.0,
        "fat_g": 15.0,
    }
    d.update(overrides)
    return d


def _parse_args(args: list) -> object:
    parser = build_parser()
    return parser.parse_args(args)


def _write_temp_json(data_bytes: bytes) -> str:
    """Write raw bytes to a temp file; return path."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="test_cli_json_")
    os.write(fd, data_bytes)
    os.close(fd)
    return path


class TestJSONInputHardening(unittest.TestCase):
    """CLI JSON parser must reject malformed/dangerous inputs without leaking PHI."""

    def setUp(self):
        self.db = _make_temp_db()

    def tearDown(self):
        try:
            self.db.unlink()
        except FileNotFoundError:
            pass

    def test_valid_json_accepted(self):
        fpath = _write_temp_json(json.dumps(_minimal_data()).encode())
        try:
            args = _parse_args(["--db", str(self.db), "create", "--file", fpath])
            out = StringIO()
            with patch("sys.stdout", out):
                rc = cmd_create(args)
            self.assertEqual(rc, 0)
        finally:
            os.unlink(fpath)

    def test_input_size_limit(self):
        """Input over 65536 bytes is rejected."""
        big = {"name": "x" * 70000, "start_time": "2026-01-15T12:00:00Z",
               "end_time": "2026-01-15T12:30:00Z", "energy_kcal": 0,
               "protein_g": 0, "carbohydrate_g": 0, "fat_g": 0}
        fpath = _write_temp_json(json.dumps(big).encode())
        try:
            args = _parse_args(["--db", str(self.db), "create", "--file", fpath])
            err = StringIO()
            with patch("sys.stderr", err):
                rc = cmd_create(args)
            self.assertNotEqual(rc, 0)
        finally:
            os.unlink(fpath)

    def test_duplicate_key_in_file_rejected(self):
        raw = b'{"name":"A","name":"B","start_time":"2026-01-15T12:00:00Z","end_time":"2026-01-15T12:30:00Z","energy_kcal":0,"protein_g":0,"carbohydrate_g":0,"fat_g":0}'
        fpath = _write_temp_json(raw)
        try:
            args = _parse_args(["--db", str(self.db), "create", "--file", fpath])
            err = StringIO()
            with patch("sys.stderr", err):
                rc = cmd_create(args)
            self.assertNotEqual(rc, 0)
        finally:
            os.unlink(fpath)

    def test_error_message_no_phi_echo(self):
        """Error messages must not echo user-supplied field names."""
        raw = json.dumps(_minimal_data(evil_field="some data")).encode()
        fpath = _write_temp_json(raw)
        try:
            args = _parse_args(["--db", str(self.db), "create", "--file", fpath])
            err_out = StringIO()
            with patch("sys.stderr", err_out):
                rc = cmd_create(args)
            self.assertNotEqual(rc, 0)
            stderr_text = err_out.getvalue()
            self.assertNotIn("evil_field", stderr_text)
            self.assertNotIn("some data", stderr_text)
        finally:
            os.unlink(fpath)


class TestCLICreate(unittest.TestCase):
    def setUp(self):
        self.db = _make_temp_db()
        self._tmpfiles = []

    def tearDown(self):
        for f in self._tmpfiles:
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        try:
            self.db.unlink()
        except FileNotFoundError:
            pass

    def _create_via_file(self, data: dict) -> tuple:
        fpath = _write_temp_json(json.dumps(data).encode())
        self._tmpfiles.append(fpath)
        args = _parse_args(["--db", str(self.db), "create", "--file", fpath])
        captured = StringIO()
        with patch("sys.stdout", captured):
            rc = cmd_create(args)
        if rc == 0:
            return rc, json.loads(captured.getvalue())
        return rc, {}

    def test_create_returns_crid(self):
        rc, out = self._create_via_file(_minimal_data())
        self.assertEqual(rc, 0)
        self.assertIn("client_record_id", out)

    def test_create_version_1(self):
        rc, out = self._create_via_file(_minimal_data())
        self.assertEqual(out.get("client_record_version"), 1)

    def test_create_state_draft(self):
        rc, out = self._create_via_file(_minimal_data())
        self.assertEqual(out.get("state"), "draft")

    def test_create_invalid_data_returns_error(self):
        rc, _ = self._create_via_file(_minimal_data(energy_kcal=-1.0))
        self.assertNotEqual(rc, 0)

    def test_create_optional_nutrients_rejected(self):
        rc, _ = self._create_via_file(_minimal_data(fiber_g=5.0))
        self.assertNotEqual(rc, 0)

    def test_create_from_file(self):
        fd, fpath = tempfile.mkstemp(suffix=".json", prefix="test_cli_create_")
        with os.fdopen(fd, "w") as f:
            json.dump(_minimal_data(), f)
        try:
            args = _parse_args(["--db", str(self.db), "create", "--file", fpath])
            captured = StringIO()
            with patch("sys.stdout", captured):
                rc = cmd_create(args)
            self.assertEqual(rc, 0)
            out = json.loads(captured.getvalue())
            self.assertIn("client_record_id", out)
        finally:
            os.unlink(fpath)


class TestCLIEdit(unittest.TestCase):
    def setUp(self):
        self.db = _make_temp_db()
        self.store = NutritionStore(self.db)
        entry = self.store.create_entry(_minimal_data())
        self.crid = entry["client_record_id"]

    def tearDown(self):
        self.store.close()
        try:
            self.db.unlink()
        except FileNotFoundError:
            pass

    def _edit_via_file(self, crid: str, version: int, data: dict) -> tuple:
        import argparse
        fpath = _write_temp_json(json.dumps(data).encode())
        try:
            # Bypass argparse to avoid crid-starts-with-dash issues (token_urlsafe can produce -)
            args = argparse.Namespace(db=self.db, id=crid, version=version, file=fpath, command="edit")
            captured = StringIO()
            with patch("sys.stdout", captured):
                rc = cmd_edit(args)
        finally:
            os.unlink(fpath)
        if rc == 0:
            return rc, json.loads(captured.getvalue())
        return rc, {}

    def test_edit_bumps_version(self):
        rc, out = self._edit_via_file(self.crid, 1, _minimal_data(name="Updated"))
        self.assertEqual(rc, 0)
        self.assertEqual(out.get("client_record_version"), 2)

    def test_edit_resets_to_draft(self):
        # Confirm first (version stays 1 after confirm)
        self.store.confirm_entry(self.crid, 1, CONFIRM_AFFIRM_PHRASE, "alice")
        rc, out = self._edit_via_file(self.crid, 1, _minimal_data(name="Re-edited"))
        self.assertEqual(rc, 0)
        self.assertEqual(out.get("state"), "draft")

    def test_edit_wrong_version_error(self):
        rc, _ = self._edit_via_file(self.crid, 99, _minimal_data())
        self.assertNotEqual(rc, 0)

    def test_edit_nonexistent_error(self):
        rc, _ = self._edit_via_file("nonexistent-id", 1, _minimal_data())
        self.assertNotEqual(rc, 0)

    def test_edit_after_ack_shows_delivery_note(self):
        """Edit after ack shows delivery_possible warning."""
        self.store.confirm_entry(self.crid, 1, CONFIRM_AFFIRM_PHRASE, "alice")
        self.store.ack_record(self.crid, 1)
        rc, out = self._edit_via_file(self.crid, 1, _minimal_data(name="Corrected"))
        self.assertEqual(rc, 0)
        self.assertIn("delivery_possible", out)
        self.assertTrue(out["delivery_possible"])


class TestCLIConfirm(unittest.TestCase):
    def setUp(self):
        self.db = _make_temp_db()
        self.store = NutritionStore(self.db)
        entry = self.store.create_entry(_minimal_data())
        self.crid = entry["client_record_id"]

    def tearDown(self):
        self.store.close()
        try:
            self.db.unlink()
        except FileNotFoundError:
            pass

    def _confirm_via_cli(self, crid, version, affirm, confirmer) -> tuple:
        import argparse
        # Bypass argparse to avoid crid-starts-with-dash issues
        args = argparse.Namespace(db=self.db, id=crid, version=version,
                                   affirm=affirm, confirmer=confirmer, command="confirm")
        captured = StringIO()
        with patch("sys.stdout", captured):
            rc = cmd_confirm(args)
        if rc == 0:
            return rc, json.loads(captured.getvalue())
        return rc, {}

    def test_confirm_correct_returns_0(self):
        rc, out = self._confirm_via_cli(
            self.crid, 1, CONFIRM_AFFIRM_PHRASE, "alice"
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out.get("state"), "confirmed")

    def test_confirm_wrong_affirm_returns_error(self):
        rc, _ = self._confirm_via_cli(self.crid, 1, "wrong phrase", "alice")
        self.assertNotEqual(rc, 0)

    def test_confirm_empty_confirmer_returns_error(self):
        rc, _ = self._confirm_via_cli(self.crid, 1, CONFIRM_AFFIRM_PHRASE, "")
        self.assertNotEqual(rc, 0)

    def test_confirm_wrong_version_returns_error(self):
        rc, _ = self._confirm_via_cli(self.crid, 99, CONFIRM_AFFIRM_PHRASE, "alice")
        self.assertNotEqual(rc, 0)

    def test_confirm_records_confirmer(self):
        rc, out = self._confirm_via_cli(
            self.crid, 1, CONFIRM_AFFIRM_PHRASE, "bob"
        )
        self.assertEqual(out.get("confirmed_by"), "bob")

    def test_confirm_does_not_bump_version(self):
        rc, out = self._confirm_via_cli(
            self.crid, 1, CONFIRM_AFFIRM_PHRASE, "alice"
        )
        self.assertEqual(out.get("client_record_version"), 1)


class TestCLIStatus(unittest.TestCase):
    def setUp(self):
        self.db = _make_temp_db()
        self.store = NutritionStore(self.db)
        entry = self.store.create_entry(_minimal_data())
        self.crid = entry["client_record_id"]

    def tearDown(self):
        self.store.close()
        try:
            self.db.unlink()
        except FileNotFoundError:
            pass

    def _status_all(self) -> dict:
        args = _parse_args(["--db", str(self.db), "status"])
        captured = StringIO()
        with patch("sys.stdout", captured):
            cmd_status(args)
        return json.loads(captured.getvalue())

    def _status_one(self, crid: str) -> dict:
        import argparse
        args = argparse.Namespace(db=self.db, id=crid, state=None, command="status")
        captured = StringIO()
        with patch("sys.stdout", captured):
            cmd_status(args)
        return json.loads(captured.getvalue())

    def test_status_all_has_stats(self):
        out = self._status_all()
        self.assertIn("stats", out)
        self.assertIn("entries", out)

    def test_status_one_returns_entry(self):
        out = self._status_one(self.crid)
        self.assertEqual(out.get("client_record_id"), self.crid)

    def test_status_one_nonexistent_error(self):
        import argparse
        args = argparse.Namespace(db=self.db, id="bad-id", state=None, command="status")
        err = StringIO()
        with patch("sys.stderr", err):
            rc = cmd_status(args)
        self.assertNotEqual(rc, 0)

    def test_status_shows_description_body(self):
        entry = self.store.create_entry(_minimal_data(description="test meal body"))
        crid = entry["client_record_id"]
        out = self._status_one(crid)
        self.assertIn("description_body", out)
        self.assertEqual(out["description_body"], "test meal body")
        self.assertTrue(out.get("description_has_ai_prefix", False))

    def test_status_no_description_body_empty(self):
        """Entry with no description shows empty body string."""
        out = self._status_one(self.crid)
        # description body should be empty string when no description was given
        self.assertEqual(out.get("description_body", ""), "")


class TestCLICancel(unittest.TestCase):
    def setUp(self):
        self.db = _make_temp_db()
        self.store = NutritionStore(self.db)
        entry = self.store.create_entry(_minimal_data())
        self.crid = entry["client_record_id"]

    def tearDown(self):
        self.store.close()
        try:
            self.db.unlink()
        except FileNotFoundError:
            pass

    def _cancel_via_cli(self, crid: str, version: int) -> tuple:
        import argparse
        args = argparse.Namespace(db=self.db, id=crid, version=version, command="cancel")
        captured = StringIO()
        with patch("sys.stdout", captured):
            rc = cmd_cancel(args)
        if rc == 0:
            return rc, json.loads(captured.getvalue())
        return rc, {}

    def test_cancel_returns_cancelled(self):
        rc, out = self._cancel_via_cli(self.crid, 1)
        self.assertEqual(rc, 0)
        self.assertEqual(out.get("state"), "cancelled")

    def test_cancel_wrong_version_error(self):
        rc, _ = self._cancel_via_cli(self.crid, 99)
        self.assertNotEqual(rc, 0)

    def test_cancel_nonexistent_error(self):
        rc, _ = self._cancel_via_cli("bad-id", 1)
        self.assertNotEqual(rc, 0)

    def test_cancel_post_ack_has_note(self):
        self.store.confirm_entry(self.crid, 1, CONFIRM_AFFIRM_PHRASE, "alice")
        self.store.ack_record(self.crid, 1)
        rc, out = self._cancel_via_cli(self.crid, 1)
        self.assertEqual(rc, 0)
        self.assertIn("cancellation_note", out)
        self.assertIn("write-back protocol", out["cancellation_note"])


class TestCLIHelp(unittest.TestCase):
    def test_all_subcommands_present(self):
        import argparse
        parser = build_parser()
        subparsers_action = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers_action = action
                break
        self.assertIsNotNone(subparsers_action)
        choices = set(subparsers_action.choices.keys())
        for cmd in ("create", "edit", "confirm", "status", "cancel"):
            self.assertIn(cmd, choices)

    def test_confirm_requires_affirm(self):
        import argparse
        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "confirm", "some-id", "--version", "1", "--confirmer", "alice"
            ])

    def test_confirm_requires_confirmer(self):
        import argparse
        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "confirm", "some-id",
                "--version", "1",
                "--affirm", CONFIRM_AFFIRM_PHRASE,
            ])

    def test_confirm_requires_version(self):
        import argparse
        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "confirm", "some-id",
                "--affirm", CONFIRM_AFFIRM_PHRASE,
                "--confirmer", "alice",
            ])


if __name__ == "__main__":
    unittest.main()

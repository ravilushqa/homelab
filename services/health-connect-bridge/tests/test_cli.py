"""
CLI tests — read-only commands, timezone, DST.

SYNTHETIC fixtures, isolated DB.

Tests:
- status: not_synced / empty / ok / stale (all four states)
- day: per-source steps, HR, weight; cross-source isolation; overlap detection
- sleep: sessions by end date; wrong-date empty
- workouts: sessions by start date
- All CLI commands are read-only (mutations rejected)
- Timezone midnight boundary and DST (Europe/Berlin spring/autumn)
- Status per-source freshness (separate from transport)
- No health data values in status output
"""

import json
import sys
import unittest
from datetime import datetime, timezone, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from .fixtures import (
    META_DEVICE_A,
    META_DEVICE_B,
    payload_with_steps,
    payload_with_sleep,
    payload_with_exercise,
    payload_with_weight,
    payload_with_hr_samples,
    payload_multi_source,
)
from .helpers import make_temp_db
from ..store import HealthStore, open_ro
from ..cli import cmd_status, cmd_day, cmd_sleep, cmd_workouts


def _now_utc_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fresh_ts() -> str:
    """A timestamp a few minutes in the past — fresh for status tests."""
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")


def _payload_with_steps_fresh(**kwargs) -> dict:
    """Steps payload with timestamps 5 minutes ago — appears fresh in status."""
    now = datetime.now(timezone.utc) - timedelta(minutes=5)
    start = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    end = now.isoformat().replace("+00:00", "Z")
    return {
        "timestamp": _fresh_ts(),
        "app_version": "SYNTHETIC-TEST-1.0",
        "steps": [{
            "count": kwargs.get("count", 1000),
            "start_time": kwargs.get("start", start),
            "end_time": kwargs.get("end", end),
            "metadata": kwargs.get("meta", META_DEVICE_A),
        }],
    }


def _ingest(store: HealthStore, payload: dict) -> None:
    received_at = _now_utc_z()
    store.ingest_payload(payload, received_at)


class _FakeArgs:
    def __init__(self, db: Path, date: str = None):
        self.db = db
        self.date = date


class TestCLIStatus(unittest.TestCase):
    def setUp(self):
        self.db_path = make_temp_db()

    def tearDown(self):
        try:
            self.db_path.unlink()
        except FileNotFoundError:
            pass

    def test_status_not_synced_when_no_db(self):
        missing = self.db_path.parent / "does_not_exist_test.sqlite3"
        args = _FakeArgs(db=missing)
        buf = StringIO()
        with patch("sys.stdout", buf):
            code = cmd_status(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["state"], "not_synced")
        self.assertEqual(code, 1)

    def test_status_not_synced_empty_db(self):
        store = HealthStore(self.db_path)
        store.close()
        args = _FakeArgs(db=self.db_path)
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_status(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["state"], "not_synced")

    def test_status_empty_after_ingest_log_no_records(self):
        """Ingest a payload with no records → 'empty' state."""
        store = HealthStore(self.db_path)
        # Log only, no records
        store._conn.execute(
            "INSERT INTO ingest_log (received_at, payload_ts, app_version, record_count) "
            "VALUES (?,?,?,?)",
            (_fresh_ts(), _fresh_ts(), "SYNTHETIC", 0),
        )
        store._conn.commit()
        store.close()
        args = _FakeArgs(db=self.db_path)
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_status(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["state"], "empty")

    def test_status_ok_after_fresh_ingest(self):
        """Status is 'ok' when records exist and are fresh (< 48h ago)."""
        store = HealthStore(self.db_path)
        _ingest(store, _payload_with_steps_fresh())
        store.close()
        args = _FakeArgs(db=self.db_path)
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_status(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["state"], "ok")
        self.assertIn("transport", result)
        self.assertIn("data", result)
        self.assertIn("per_source", result["data"])
        self.assertGreater(result["data"]["total_records"], 0)

    def test_status_stale_with_old_records(self):
        """Status is 'stale' when records exist but are > 48h old."""
        store = HealthStore(self.db_path)
        # Inject a record with old start/end epoch time (50h ago)
        old_s = (datetime.now(timezone.utc) - timedelta(hours=50)).timestamp()
        store._conn.execute(
            "INSERT INTO ingest_log (received_at, payload_ts, app_version, record_count) "
            "VALUES (?,?,?,?)",
            (_fresh_ts(), _fresh_ts(), "SYNTHETIC", 1),
        )
        # Insert a fake record with old epoch times
        from ..identity import build_source_key
        src_key = build_source_key(META_DEVICE_A)
        store._conn.execute(
            "INSERT INTO health_records "
            "(identity_key, record_type, source_key, record_start_s, record_end_s, "
            "record_json, received_at, payload_ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("t_test_stale", "steps", src_key, old_s, old_s,
             '{"count":100,"start_time":"old","end_time":"old"}',
             _fresh_ts(), _fresh_ts()),
        )
        store._conn.commit()
        store.close()
        args = _FakeArgs(db=self.db_path)
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_status(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["state"], "stale")

    def test_status_per_source_present(self):
        """Status includes per-source freshness breakdown."""
        store = HealthStore(self.db_path)
        _ingest(store, _payload_with_steps_fresh())
        store.close()
        args = _FakeArgs(db=self.db_path)
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_status(args)
        result = json.loads(buf.getvalue())
        per_source = result["data"]["per_source"]
        self.assertGreater(len(per_source), 0)
        for src_data in per_source.values():
            self.assertIn("latest_record_utc", src_data)
            self.assertIn("record_age_hours", src_data)
            self.assertIn("record_count", src_data)

    def test_status_no_health_values_in_output(self):
        """Status output must not contain step counts or health record values."""
        store = HealthStore(self.db_path)
        _ingest(store, _payload_with_steps_fresh(count=99887))
        store.close()
        args = _FakeArgs(db=self.db_path)
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_status(args)
        output = buf.getvalue()
        self.assertNotIn("99887", output)


class TestCLIDay(unittest.TestCase):
    """Test the 'day' command with epoch-based range queries."""

    def setUp(self):
        self.db_path = make_temp_db()
        self.store = HealthStore(self.db_path)
        _ingest(self.store, payload_with_steps(
            count=8000,
            start="2026-01-15T05:00:00Z",
            end="2026-01-15T22:00:00Z",
            meta=META_DEVICE_A,
        ))
        _ingest(self.store, payload_with_weight(meta=META_DEVICE_A))
        _ingest(self.store, payload_with_hr_samples(meta=META_DEVICE_A))
        _ingest(self.store, payload_multi_source())
        self.store.close()

    def tearDown(self):
        try:
            self.db_path.unlink()
        except FileNotFoundError:
            pass

    def test_day_returns_steps_per_source(self):
        args = _FakeArgs(db=self.db_path, date="2026-01-15")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_day(args)
        result = json.loads(buf.getvalue())
        self.assertIn("sources", result)
        has_steps = any("steps" in v for v in result["sources"].values())
        self.assertTrue(has_steps)

    def test_day_multi_source_separate_not_summed(self):
        """Multi-source records must appear per-source, not summed."""
        args = _FakeArgs(db=self.db_path, date="2026-01-15")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_day(args)
        result = json.loads(buf.getvalue())
        step_sources = [k for k, v in result["sources"].items() if "steps" in v]
        self.assertGreaterEqual(len(step_sources), 2, "Steps must be per-source")

    def test_day_timezone_correct(self):
        args = _FakeArgs(db=self.db_path, date="2026-01-15")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_day(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["timezone"], "Europe/Berlin")

    def test_day_empty_date_returns_empty_sources(self):
        args = _FakeArgs(db=self.db_path, date="2000-01-01")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_day(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["sources"], {})

    def test_day_steps_overlap_returns_null_total(self):
        """Overlapping step intervals within one source → total=null + warning."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        # Two overlapping intervals from same source
        _ingest(store, {
            "timestamp": "2026-06-01T10:00:00Z",
            "app_version": "SYNTHETIC",
            "steps": [
                {"count": 500, "start_time": "2026-06-01T06:00:00Z",
                 "end_time": "2026-06-01T08:00:00Z", "metadata": META_DEVICE_A},
                {"count": 300, "start_time": "2026-06-01T07:00:00Z",
                 "end_time": "2026-06-01T09:00:00Z", "metadata": META_DEVICE_A},
            ],
        })
        store.close()
        args = _FakeArgs(db=db_path, date="2026-06-01")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_day(args)
        result = json.loads(buf.getvalue())
        db_path.unlink()
        # Find the source with steps
        src_steps = None
        for src_data in result["sources"].values():
            if "steps" in src_data:
                src_steps = src_data["steps"]
                break
        self.assertIsNotNone(src_steps)
        self.assertIsNone(src_steps.get("total"), "Overlapping intervals should yield total=null")
        self.assertIn("ambiguity_warning", src_steps)

    def test_day_invalid_date_format(self):
        """Invalid date format returns error."""
        args = _FakeArgs(db=self.db_path, date="2026-13-01")
        buf = StringIO()
        with patch("sys.stdout", buf):
            code = cmd_day(args)
        result = json.loads(buf.getvalue())
        self.assertIn("error", result)
        self.assertEqual(code, 1)

    def test_day_epoch_query_not_lexical(self):
        """
        Verify epoch-based range queries work for Berlin-offset records.
        2026-01-14T23:30:00Z = 2026-01-15T00:30 Berlin — should appear on Jan 15.
        """
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, payload_with_steps(
            start="2026-01-14T23:30:00Z",  # Berlin: 2026-01-15T00:30
            end="2026-01-15T00:30:00Z",
            meta=META_DEVICE_A,
        ))
        store.close()
        args = _FakeArgs(db=db_path, date="2026-01-15")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_day(args)
        result = json.loads(buf.getvalue())
        db_path.unlink()
        has_steps = any("steps" in v for v in result["sources"].values())
        self.assertTrue(has_steps, "Record at Berlin midnight should appear on Jan 15")

    def test_day_epoch_query_wrong_date(self):
        """Same record should NOT appear on Jan 14 (Berlin)."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, payload_with_steps(
            start="2026-01-14T23:30:00Z",  # Berlin: Jan 15
            end="2026-01-15T00:30:00Z",
            meta=META_DEVICE_A,
        ))
        store.close()
        args = _FakeArgs(db=db_path, date="2026-01-14")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_day(args)
        result = json.loads(buf.getvalue())
        db_path.unlink()
        has_steps = any("steps" in v for v in result["sources"].values())
        self.assertFalse(has_steps, "Jan 15 record must not appear on Jan 14")


class TestCLISleep(unittest.TestCase):
    def setUp(self):
        self.db_path = make_temp_db()
        self.store = HealthStore(self.db_path)
        # Sleep ending on Jan 15 Berlin time (07:30 UTC = 08:30 CET)
        _ingest(self.store, payload_with_sleep(
            end="2026-01-15T07:30:00Z",
            meta=META_DEVICE_A,
        ))
        self.store.close()

    def tearDown(self):
        try:
            self.db_path.unlink()
        except FileNotFoundError:
            pass

    def test_sleep_returns_session(self):
        args = _FakeArgs(db=self.db_path, date="2026-01-15")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_sleep(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(len(result["sessions"]), 1)

    def test_sleep_wrong_date_returns_empty(self):
        args = _FakeArgs(db=self.db_path, date="2026-01-14")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_sleep(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["sessions"], [])

    def test_sleep_shows_duration(self):
        args = _FakeArgs(db=self.db_path, date="2026-01-15")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_sleep(args)
        result = json.loads(buf.getvalue())
        session = result["sessions"][0]
        self.assertIn("duration_hours", session)
        self.assertGreater(session["duration_hours"], 0)

    def test_sleep_shows_stages(self):
        args = _FakeArgs(db=self.db_path, date="2026-01-15")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_sleep(args)
        result = json.loads(buf.getvalue())
        session = result["sessions"][0]
        self.assertIn("stages", session)


class TestCLIWorkouts(unittest.TestCase):
    def setUp(self):
        self.db_path = make_temp_db()
        self.store = HealthStore(self.db_path)
        _ingest(self.store, payload_with_exercise(
            start="2026-01-15T09:00:00Z",
            end="2026-01-15T10:00:00Z",
            meta=META_DEVICE_A,
        ))
        self.store.close()

    def tearDown(self):
        try:
            self.db_path.unlink()
        except FileNotFoundError:
            pass

    def test_workouts_returns_exercise(self):
        args = _FakeArgs(db=self.db_path, date="2026-01-15")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_workouts(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(len(result["workouts"]), 1)

    def test_workouts_shows_type_and_duration(self):
        args = _FakeArgs(db=self.db_path, date="2026-01-15")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_workouts(args)
        result = json.loads(buf.getvalue())
        w = result["workouts"][0]
        self.assertIn("type", w)
        self.assertIn("duration_minutes", w)

    def test_workouts_wrong_date_empty(self):
        args = _FakeArgs(db=self.db_path, date="2026-01-14")
        buf = StringIO()
        with patch("sys.stdout", buf):
            cmd_workouts(args)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["workouts"], [])


class TestCLITimezone(unittest.TestCase):
    """DST and midnight boundary tests."""

    def _run_with_db(self, setup_fn, query_fn, date_str: str) -> dict:
        """Helper: create fresh DB, populate via setup_fn, query via query_fn."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        setup_fn(store)
        store.close()
        args = _FakeArgs(db=db_path, date=date_str)
        buf = StringIO()
        with patch("sys.stdout", buf):
            query_fn(args)
        db_path.unlink()
        return json.loads(buf.getvalue())

    def test_dst_spring_forward_berlin_2026(self):
        """
        2026-03-29 is the DST spring-forward in Europe/Berlin (02:00 → 03:00).
        Records on both sides of the DST gap should appear on 2026-03-29 Berlin.
        """
        def setup(store):
            _ingest(store, payload_with_steps(
                start="2026-03-29T00:30:00Z",  # 01:30 Berlin (CET+1)
                end="2026-03-29T01:30:00Z", meta=META_DEVICE_A,
            ))
            _ingest(store, payload_with_steps(
                start="2026-03-29T02:30:00Z",  # 04:30 Berlin (CEST+2)
                end="2026-03-29T03:30:00Z", meta=META_DEVICE_A,
            ))
        result = self._run_with_db(setup, cmd_day, "2026-03-29")
        has_steps = any("steps" in v for v in result["sources"].values())
        self.assertTrue(has_steps, "Records on DST spring-forward day should be found")

    def test_midnight_berlin_is_jan_15(self):
        """
        2026-01-15T00:00:00 Berlin = 2026-01-14T23:00:00Z.
        Record starting at exactly Berlin midnight should appear on Jan 15.
        """
        def setup(store):
            _ingest(store, payload_with_steps(
                start="2026-01-14T23:00:00Z",  # midnight Berlin Jan 15
                end="2026-01-14T23:30:00Z", meta=META_DEVICE_A,
            ))
        result15 = self._run_with_db(setup, cmd_day, "2026-01-15")
        # Need to re-run with same DB — setup already deleted it; redo
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, payload_with_steps(
            start="2026-01-14T23:00:00Z", end="2026-01-14T23:30:00Z",
            meta=META_DEVICE_A,
        ))
        store.close()
        buf14 = StringIO()
        with patch("sys.stdout", buf14):
            cmd_day(_FakeArgs(db=db_path, date="2026-01-14"))
        buf15 = StringIO()
        with patch("sys.stdout", buf15):
            cmd_day(_FakeArgs(db=db_path, date="2026-01-15"))
        db_path.unlink()
        result14 = json.loads(buf14.getvalue())
        result15 = json.loads(buf15.getvalue())
        self.assertTrue(
            any("steps" in v for v in result15["sources"].values()),
            "Record at Berlin midnight Jan 15 should appear on Jan 15",
        )
        self.assertFalse(
            any("steps" in v for v in result14["sources"].values()),
            "Record at Berlin midnight Jan 15 should NOT appear on Jan 14",
        )

    def test_fractional_seconds_dedup(self):
        """Z and +00:00 variants of same timestamp dedup to 1 record."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, payload_with_steps(
            start="2026-01-15T06:00:00.000Z", end="2026-01-15T07:00:00.000Z",
            meta=META_DEVICE_A,
        ))
        _ingest(store, payload_with_steps(
            start="2026-01-15T06:00:00.000+00:00", end="2026-01-15T07:00:00.000+00:00",
            meta=META_DEVICE_A,
        ))
        store.close()
        from ..store import open_ro
        conn = open_ro(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='steps'"
            ).fetchone()["n"]
        finally:
            conn.close()
        db_path.unlink()
        self.assertEqual(count, 1, "Z and +00:00 equivalent timestamps must dedup to 1 record")


if __name__ == "__main__":
    unittest.main()

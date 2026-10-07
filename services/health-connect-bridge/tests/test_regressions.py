"""
Regression tests for the 7 numbered correctness issues plus 5b.

SYNTHETIC fixtures, isolated DBs.  Never touches production data.

Issue 1: build_identity_key namespaces explicit IDs by origin
Issue 2: Step day query uses interval intersection; overlaps detected across all intervals
Issue 3: normalise_ts_to_epoch_us preserves nanosecond precision; no float truncation
Issue 4: bmi is in INSTANT_TYPES; records stored and retrievable
Issue 5: Mixed HR sample+bucket records: separate sample_summary and bucket_summary
Issue 5b: Explicit-ID heart_rate sample_disc includes shape prefix (s/b) so sample and
          bucket at same explicit_id+origin+time remain two distinct records
Issue 6: Sleep/exercise duration rejects NaN/Inf/bool; stages null normalised; exercise 0 allowed
Issue 7: Stale-ID protection: versionless update to versioned record is rejected
"""

import json
import math
import unittest
from datetime import datetime, timezone, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from .fixtures import META_DEVICE_A, META_DEVICE_B, minimal_payload
from .helpers import make_temp_db
from ..identity import (
    build_identity_key,
    build_temporal_key,
    normalise_ts_to_epoch_us,
    INSTANT_TYPES,
    SERIES_TYPES,
)
from ..store import HealthStore, open_ro, query_day, _day_epoch_bounds
from ..validation import validate_payload
from ..cli import cmd_day


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ingest(store: HealthStore, payload: dict) -> None:
    store.ingest_payload(payload, _now_z())


# ── Issue 1: build_identity_key origin namespacing ────────────────────────────

class TestIssue1OriginNamespacing(unittest.TestCase):
    """Same client_record_id in different origins must remain two separate records."""

    def test_same_id_different_origins_are_distinct_records(self):
        """Two apps with identical client_record_id but different data_origin stay separate."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        meta_app_a = {"data_origin": "com.app.alpha", "recording_method": 2}
        meta_app_b = {"data_origin": "com.app.beta", "recording_method": 2}
        _ingest(store, minimal_payload(weight=[{
            "kilograms": 80.0,
            "time": "2026-01-15T08:00:00Z",
            "metadata": {**meta_app_a, "id": "weight-record-001"},
        }]))
        _ingest(store, minimal_payload(weight=[{
            "kilograms": 85.0,
            "time": "2026-01-15T08:00:00Z",
            "metadata": {**meta_app_b, "id": "weight-record-001"},
        }]))
        conn = open_ro(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='weight'"
            ).fetchone()["n"]
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(count, 2,
            "Same explicit ID from different origins must remain two distinct records")

    def test_same_id_same_origin_different_device_stays_one(self):
        """Same origin + same ID but device metadata changes → stays one record (device not in key)."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        base = {"data_origin": "com.app", "recording_method": 2, "id": "weight-id-X"}
        _ingest(store, minimal_payload(weight=[{
            "kilograms": 80.0,
            "time": "2026-01-15T08:00:00Z",
            "metadata": {**base, "device": {"manufacturer": "A", "model": "M1", "type": 1}},
        }]))
        _ingest(store, minimal_payload(weight=[{
            "kilograms": 82.0,
            "time": "2026-01-15T08:00:00Z",
            "metadata": {**base, "device": {"manufacturer": "A", "model": "M2", "type": 2}},
        }]))
        conn = open_ro(db_path)
        try:
            rows = conn.execute(
                "SELECT record_json FROM health_records WHERE record_type='weight'"
            ).fetchall()
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(len(rows), 1,
            "Same origin + explicit ID; device metadata update must not create a second record")
        self.assertAlmostEqual(json.loads(rows[0]["record_json"])["kilograms"], 82.0)

    def test_explicit_id_no_origin_falls_back_to_temporal(self):
        """Explicit ID without data_origin must use temporal+source fallback, not a global ID."""
        src = '{"origin":null,"method":1,"device":null}'
        tkey = "i:12345678901234567"
        expl = ("server", "record-no-origin")
        key = build_identity_key("weight", src, tkey, expl, None, origin=None)
        decoded = json.loads(key)
        self.assertEqual(decoded[0], "t",
            "No origin → must fall back to temporal key, not use explicit ID globally")
        self.assertEqual(decoded[2], src)


# ── Issue 2: Step interval intersection + cross-boundary overlap detection ─────

class TestIssue2StepIntersection(unittest.TestCase):
    """Prior-day interval plus overlapping current-day entry must not report clean total."""

    def _make_store(self):
        db_path = make_temp_db()
        return db_path, HealthStore(db_path)

    def test_cross_boundary_interval_fetched_and_makes_total_null(self):
        """
        A step interval that starts before midnight local time crosses the day boundary.
        It must be fetched by the intersection query and cause total=null.
        """
        db_path, store = self._make_store()
        # Berlin Jan 15: day_start = 2026-01-14T23:00:00Z, day_end = 2026-01-15T23:00:00Z
        # This interval starts at 22:00 UTC Jan 14 (before the day starts in Berlin)
        _ingest(store, minimal_payload(steps=[{
            "count": 500,
            "start_time": "2026-01-14T22:00:00Z",  # before Berlin midnight Jan 15
            "end_time": "2026-01-15T00:30:00Z",     # within Jan 15 Berlin
            "metadata": META_DEVICE_A,
        }]))
        store.close()
        conn = open_ro(db_path)
        try:
            result = query_day(conn, "2026-01-15", "Europe/Berlin")
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        # The interval must appear (intersection fetched it)
        src_data = next(iter(result["sources"].values()), {})
        steps = src_data.get("steps", {})
        self.assertIsNotNone(steps, "Cross-boundary interval must be fetched")
        self.assertIsNone(steps.get("total"),
            "Cross-boundary interval must cause total=null (can't compute full-day total)")
        self.assertIn("ambiguity_warning", steps)
        self.assertGreater(len(steps["cross_boundary"]), 0)

    def test_prior_day_plus_overlapping_entry_no_clean_total(self):
        """
        Prior-day interval that overlaps with a within-day interval:
        overlap must be detected across ALL intervals, not just contained ones.
        """
        db_path, store = self._make_store()
        # Interval A: starts before Berlin day_start, overlaps with B
        _ingest(store, minimal_payload(steps=[
            {
                "count": 300,
                "start_time": "2026-01-14T22:30:00Z",  # before Berlin Jan 15 midnight
                "end_time": "2026-01-15T01:00:00Z",     # within Jan 15
                "metadata": META_DEVICE_A,
            },
            {
                "count": 200,
                "start_time": "2026-01-15T00:30:00Z",  # overlaps with A (00:30–01:00 UTC)
                "end_time": "2026-01-15T03:00:00Z",
                "metadata": META_DEVICE_A,
            },
        ]))
        store.close()
        conn = open_ro(db_path)
        try:
            result = query_day(conn, "2026-01-15", "Europe/Berlin")
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        src_data = next(iter(result["sources"].values()), {})
        steps = src_data.get("steps", {})
        self.assertIsNone(steps.get("total"),
            "Overlap between cross-boundary and contained interval must yield total=null")
        self.assertIn("ambiguity_warning", steps)

    def test_wholly_contained_disjoint_returns_sum(self):
        """Disjoint intervals wholly within the day → total = sum of counts."""
        db_path, store = self._make_store()
        _ingest(store, minimal_payload(steps=[
            {
                "count": 1000,
                "start_time": "2026-01-15T06:00:00Z",
                "end_time": "2026-01-15T08:00:00Z",
                "metadata": META_DEVICE_A,
            },
            {
                "count": 500,
                "start_time": "2026-01-15T09:00:00Z",
                "end_time": "2026-01-15T11:00:00Z",
                "metadata": META_DEVICE_A,
            },
        ]))
        store.close()
        conn = open_ro(db_path)
        try:
            result = query_day(conn, "2026-01-15", "Europe/Berlin")
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        src_data = next(iter(result["sources"].values()), {})
        steps = src_data.get("steps", {})
        self.assertEqual(steps.get("total"), 1500,
            "Disjoint contained intervals must have total = sum of counts")


# ── Issue 3: Nanosecond precision in temporal keys ────────────────────────────

class TestIssue3NanosecondPrecision(unittest.TestCase):
    """Distinct nanosecond timestamps must produce distinct keys; Z == +00:00."""

    def test_z_and_plus00_same_key(self):
        """Z and +00:00 variants of the same timestamp deduplicate."""
        k1 = normalise_ts_to_epoch_us("2026-01-15T08:00:00.123456789Z")
        k2 = normalise_ts_to_epoch_us("2026-01-15T08:00:00.123456789+00:00")
        self.assertEqual(k1, k2, "Z and +00:00 must produce identical epoch-ns keys")

    def test_distinct_nanoseconds_remain_distinct(self):
        """.123456001Z and .123456999Z have distinct nanoseconds → distinct keys."""
        k1 = normalise_ts_to_epoch_us("2026-01-15T08:00:00.123456001Z")
        k2 = normalise_ts_to_epoch_us("2026-01-15T08:00:00.123456999Z")
        self.assertNotEqual(k1, k2,
            "Timestamps differing only in nanoseconds must produce distinct keys")

    def test_same_microsecond_different_nanosecond_distinct(self):
        """.123456000 vs .123456001 differ only in sub-microsecond: must be distinct."""
        k1 = normalise_ts_to_epoch_us("2026-01-15T08:00:00.123456000Z")
        k2 = normalise_ts_to_epoch_us("2026-01-15T08:00:00.123456001Z")
        self.assertNotEqual(k1, k2)

    def test_no_float_epoch_truncation(self):
        """Key must be an integer string, not a float."""
        k = normalise_ts_to_epoch_us("2026-01-15T08:00:00Z")
        self.assertTrue(k.isdigit(), f"Expected integer string, got {k!r}")

    def test_nanosecond_dedup_in_store(self):
        """
        Two ingestions of the same timestamp (.123456789Z and .123456789+00:00)
        must dedup to exactly one record.
        """
        db_path = make_temp_db()
        store = HealthStore(db_path)
        for tz in ("Z", "+00:00"):
            _ingest(store, minimal_payload(weight=[{
                "kilograms": 80.0,
                "time": f"2026-01-15T08:00:00.123456789{tz}",
                "metadata": META_DEVICE_A,
            }]))
        store.close()
        conn = open_ro(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='weight'"
            ).fetchone()["n"]
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(count, 1, "Z and +00:00 with same nanoseconds must dedup to 1 record")

    def test_distinct_nanosecond_records_stay_two(self):
        """
        .123456001Z and .123456999Z are two distinct records — sub-microsecond precision
        must not collapse them.
        """
        db_path = make_temp_db()
        store = HealthStore(db_path)
        for ts in ("2026-01-15T08:00:00.123456001Z", "2026-01-15T08:00:00.123456999Z"):
            _ingest(store, minimal_payload(weight=[{
                "kilograms": 80.0,
                "time": ts,
                "metadata": META_DEVICE_A,
            }]))
        store.close()
        conn = open_ro(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='weight'"
            ).fetchone()["n"]
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(count, 2,
            "Records with distinct nanosecond timestamps must not be collapsed")


# ── Issue 4: bmi in INSTANT_TYPES ─────────────────────────────────────────────

class TestIssue4BmiInInstantTypes(unittest.TestCase):
    def test_bmi_in_instant_types(self):
        self.assertIn("bmi", INSTANT_TYPES, "bmi must be in INSTANT_TYPES")

    def test_bmi_temporal_key_built(self):
        rec = {"value": 22.5, "time": "2026-01-15T08:00:00Z"}
        key = build_temporal_key("bmi", rec)
        self.assertIsNotNone(key, "build_temporal_key must return a key for bmi")

    def test_bmi_stored_and_retrievable(self):
        """BMI record with time field must be stored (not silently discarded)."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, minimal_payload(bmi=[{
            "value": 22.5,
            "time": "2026-01-15T08:00:00Z",
            "weight_kg": 75.0,
            "height_meters": 1.82,
            "metadata": META_DEVICE_A,
        }]))
        store.close()
        conn = open_ro(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='bmi'"
            ).fetchone()["n"]
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(count, 1, "BMI record must be stored, not silently discarded")

    def test_bmi_unknown_source_flags_preserved(self):
        """BMI without metadata must be stored with unknown-source flags in source_key."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, minimal_payload(bmi=[{
            "value": 22.5,
            "time": "2026-01-15T08:00:00Z",
        }]))
        store.close()
        conn = open_ro(db_path)
        try:
            row = conn.execute(
                "SELECT source_key, record_json FROM health_records WHERE record_type='bmi'"
            ).fetchone()
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertIsNotNone(row, "BMI without metadata must still be stored")
        src = json.loads(row["source_key"])
        self.assertTrue(src.get("_no_metadata"), "No-metadata source_key flag must be set")
        # record_json must contain the original data
        rec = json.loads(row["record_json"])
        self.assertAlmostEqual(rec["value"], 22.5)


# ── Issue 5: Mixed HR samples and buckets ─────────────────────────────────────

class TestIssue5MixedHRShapes(unittest.TestCase):
    """Mixed HR bucket/sample records must appear as separate summaries, not drop samples."""

    def _setup_mixed_hr(self):
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, minimal_payload(heart_rate=[
            # Raw sample (high value)
            {"bpm": 150, "time": "2026-01-15T10:00:00Z", "metadata": META_DEVICE_A},
            # Aggregate bucket (low avg)
            {"time": "2026-01-15T10:05:00Z", "avg": 70.0, "min": 65.0, "max": 80.0,
             "metadata": META_DEVICE_A},
        ]))
        store.close()
        return db_path

    def test_raw_sample_not_dropped_when_bucket_present(self):
        """150bpm raw sample must not be discarded when a 70-avg bucket is also present."""
        db_path = self._setup_mixed_hr()
        conn = open_ro(db_path)
        try:
            result = query_day(conn, "2026-01-15", "Europe/Berlin")
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        src_data = next(iter(result["sources"].values()), {})
        hr = src_data.get("heart_rate", {})
        self.assertIn("sample_summary", hr,
            "Mixed HR: sample_summary must be present when raw samples exist")
        self.assertIn("bucket_summary", hr,
            "Mixed HR: bucket_summary must be present when aggregate buckets exist")

    def test_max_not_capped_at_bucket_max(self):
        """
        With a 150bpm raw sample, the sample_summary max_bpm must be 150,
        not capped at the aggregate bucket's max of 80.
        """
        db_path = self._setup_mixed_hr()
        conn = open_ro(db_path)
        try:
            result = query_day(conn, "2026-01-15", "Europe/Berlin")
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        src_data = next(iter(result["sources"].values()), {})
        sample_sum = src_data.get("heart_rate", {}).get("sample_summary", {})
        self.assertEqual(sample_sum.get("max_bpm"), 150.0,
            "sample_summary max_bpm must reflect the raw sample (150), not bucket max (80)")

    def test_sample_and_bucket_same_timestamp_stored_separately(self):
        """
        A raw sample and an aggregate bucket at the same timestamp must not collide
        in the identity key — they are stored as two separate records.
        """
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, minimal_payload(heart_rate=[
            {"bpm": 150, "time": "2026-01-15T10:00:00Z", "metadata": META_DEVICE_A},
            {"time": "2026-01-15T10:00:00Z", "avg": 70.0, "min": 65.0, "max": 80.0,
             "metadata": META_DEVICE_A},
        ]))
        store.close()
        conn = open_ro(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='heart_rate'"
            ).fetchone()["n"]
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(count, 2,
            "Raw sample and aggregate bucket at same timestamp must be stored as 2 records")

    def test_pure_samples_only_has_sample_summary(self):
        """If only raw samples present, only sample_summary in result (no bucket_summary)."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, minimal_payload(heart_rate=[
            {"bpm": 65, "time": "2026-01-15T08:00:00Z", "metadata": META_DEVICE_A},
            {"bpm": 70, "time": "2026-01-15T08:01:00Z", "metadata": META_DEVICE_A},
        ]))
        store.close()
        conn = open_ro(db_path)
        try:
            result = query_day(conn, "2026-01-15", "Europe/Berlin")
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        src_data = next(iter(result["sources"].values()), {})
        hr = src_data.get("heart_rate", {})
        self.assertIn("sample_summary", hr)
        self.assertNotIn("bucket_summary", hr)


# ── Issue 5b: Explicit-ID heart_rate sample_disc shape prefix ────────────────

class TestIssue5bExplicitIdMixedHRShapes(unittest.TestCase):
    """
    Explicit-ID heart_rate: sample and bucket at same id+origin+time must remain
    two distinct records because sample_disc includes shape prefix (s: vs b:).
    """

    def test_explicit_id_sample_and_bucket_same_time_two_records(self):
        """
        With same explicit ID, same origin, same timestamp: one bpm record (sample)
        and one avg bucket must be stored as two separate records.
        """
        db_path = make_temp_db()
        store = HealthStore(db_path)
        meta = {"data_origin": "com.test.app", "recording_method": 2, "id": "hr-session-001"}
        _ingest(store, minimal_payload(heart_rate=[
            {"bpm": 80, "time": "2026-01-15T10:00:00Z", "metadata": meta},
            {"time": "2026-01-15T10:00:00Z", "avg": 70.0, "min": 65.0, "max": 80.0,
             "metadata": meta},
        ]))
        store.close()
        conn = open_ro(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='heart_rate'"
            ).fetchone()["n"]
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(count, 2,
            "Explicit-ID heart_rate: sample and bucket at same time must be two distinct records")

    def test_mixed_day_explicit_id_retains_both_summaries(self):
        """
        After storing explicit-ID sample and bucket, query_day returns both
        sample_summary and bucket_summary for the source.
        """
        db_path = make_temp_db()
        store = HealthStore(db_path)
        meta = {"data_origin": "com.test.app", "recording_method": 2, "id": "hr-session-002"}
        _ingest(store, minimal_payload(heart_rate=[
            {"bpm": 150, "time": "2026-01-15T10:00:00Z", "metadata": meta},
            {"time": "2026-01-15T10:00:00Z", "avg": 70.0, "min": 65.0, "max": 80.0,
             "metadata": meta},
        ]))
        store.close()
        conn = open_ro(db_path)
        try:
            result = query_day(conn, "2026-01-15", "Europe/Berlin")
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        src_data = next(iter(result["sources"].values()), {})
        hr = src_data.get("heart_rate", {})
        self.assertIn("sample_summary", hr,
            "Explicit-ID mixed HR day: sample_summary must be present")
        self.assertIn("bucket_summary", hr,
            "Explicit-ID mixed HR day: bucket_summary must be present")
        self.assertEqual(hr["sample_summary"]["max_bpm"], 150.0,
            "Explicit-ID sample max_bpm must reflect the raw sample (150), not the bucket")


# ── Issue 6: Validation — sleep/exercise NaN/Inf/bool/null-stages ─────────────

class TestIssue6ValidationEdgeCases(unittest.TestCase):
    def _sleep_payload(self, **overrides):
        rec = {
            "session_end_time": "2026-01-15T07:30:00Z",
            "duration_seconds": 28800,
            "stages": [],
        }
        rec.update(overrides)
        return minimal_payload(sleep=[rec])

    def _exercise_payload(self, **overrides):
        rec = {
            "type": "EXERCISE_TYPE_RUNNING",
            "start_time": "2026-01-15T09:00:00Z",
            "end_time": "2026-01-15T10:00:00Z",
            "duration_seconds": 3600,
        }
        rec.update(overrides)
        return minimal_payload(exercise=[rec])

    def test_sleep_inf_duration_rejected(self):
        """1e309 (JSON Infinity) duration must be rejected with 400."""
        errs = validate_payload(self._sleep_payload(duration_seconds=1e309))
        self.assertTrue(errs, "Infinite duration_seconds must be rejected")

    def test_sleep_nan_duration_rejected(self):
        """NaN duration must be rejected."""
        errs = validate_payload(self._sleep_payload(duration_seconds=float("nan")))
        self.assertTrue(errs, "NaN duration_seconds must be rejected")

    def test_sleep_bool_duration_rejected(self):
        """Boolean duration must be rejected (True is isinstance int in Python)."""
        errs = validate_payload(self._sleep_payload(duration_seconds=True))
        self.assertTrue(errs, "Boolean duration_seconds must be rejected")

    def test_sleep_zero_duration_accepted(self):
        """Zero duration is a valid finite non-negative value."""
        errs = validate_payload(self._sleep_payload(duration_seconds=0))
        self.assertEqual(errs, [], "duration_seconds=0 must be accepted")

    def test_sleep_null_stages_accepted_no_typeerror(self):
        """
        Explicit null stages must not raise TypeError in query_sleep.
        (Validation permits it; query normalises null to [].)
        """
        db_path = make_temp_db()
        store = HealthStore(db_path)
        # Inject record with explicit null stages bypassing validation
        received_at = _now_z()
        payload = minimal_payload(sleep=[{
            "session_end_time": "2026-01-15T07:30:00Z",
            "duration_seconds": 28800,
            "stages": None,  # explicitly null
        }])
        store.ingest_payload(payload, received_at)
        store.close()
        # query_sleep must not raise TypeError
        conn = open_ro(db_path)
        try:
            from ..store import query_sleep
            result = query_sleep(conn, "2026-01-15", "Europe/Berlin")
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["stages"], [])

    def test_sleep_non_list_stages_rejected(self):
        """stages must be a list if present; dict is rejected."""
        payload = self._sleep_payload(stages={"bad": "value"})
        errs = validate_payload(payload)
        self.assertTrue(errs, "Non-list stages must be rejected")

    def test_sleep_stage_inf_duration_rejected(self):
        """Stage duration_seconds=Inf must be rejected."""
        payload = minimal_payload(sleep=[{
            "session_end_time": "2026-01-15T07:30:00Z",
            "duration_seconds": 28800,
            "stages": [{
                "stage": "STAGE_AWAKE",
                "start_time": "2026-01-14T23:00:00Z",
                "end_time": "2026-01-14T23:10:00Z",
                "duration_seconds": float("inf"),
            }],
        }])
        errs = validate_payload(payload)
        self.assertTrue(errs, "Stage with Inf duration must be rejected")

    def test_http_large_float_returns_400_no_partial_write(self):
        """
        Payload containing 1e309 (JSON Infinity) in duration must return 400 with no records written.
        """
        from .helpers import TestServer
        srv = TestServer()
        try:
            import socket as _socket
            # Build raw request with 1e309 — Python json.dumps won't emit it,
            # so craft manually
            body = (
                b'{"timestamp":"2026-01-15T10:00:00Z",'
                b'"sleep":[{"session_end_time":"2026-01-15T07:30:00Z",'
                b'"duration_seconds":1e309,"stages":[]}]}'
            )
            status, _ = srv.request("POST", raw_body=body)
            self.assertEqual(status, 400, "1e309 duration must return 400")
            conn = open_ro(srv.db_path)
            try:
                total = conn.execute(
                    "SELECT COUNT(*) AS n FROM health_records"
                ).fetchone()["n"]
            finally:
                conn.close()
            self.assertEqual(total, 0, "No partial writes on invalid payload")
        finally:
            srv.stop()

    def test_exercise_zero_duration_accepted(self):
        """Exercise duration=0 is valid (sub-second Kotlin interval)."""
        errs = validate_payload(self._exercise_payload(duration_seconds=0))
        self.assertEqual(errs, [], "exercise duration_seconds=0 must be accepted")

    def test_exercise_inf_duration_rejected(self):
        """Exercise duration=Inf must be rejected."""
        errs = validate_payload(self._exercise_payload(duration_seconds=float("inf")))
        self.assertTrue(errs)

    def test_exercise_bool_duration_rejected(self):
        """Exercise bool duration must be rejected."""
        errs = validate_payload(self._exercise_payload(duration_seconds=True))
        self.assertTrue(errs)


# ── Issue 7: Stale-ID update protection ───────────────────────────────────────

class TestIssue7StaleIdProtection(unittest.TestCase):
    """
    version10 → unversioned → version9 replay must NOT clear the watermark.
    A versionless update to a versioned record must be rejected.
    """

    def _make_steps(self, ver=None, count=1000):
        meta = {"data_origin": "com.synthetic.app", "recording_method": 2,
                "id": "stale-id-test-v2"}
        if ver is not None:
            meta["client_record_version"] = ver
        return minimal_payload(steps=[{
            "count": count,
            "start_time": "2026-01-15T06:00:00Z",
            "end_time": "2026-01-15T07:00:00Z",
            "metadata": meta,
        }])

    def test_versionless_update_does_not_clear_watermark(self):
        """
        Sequence: version=10 → unversioned → version=9.
        The unversioned update must be rejected (version watermark preserved),
        and then the version=9 update must also be rejected (still older).
        """
        db_path = make_temp_db()
        store = HealthStore(db_path)
        # Step 1: store v10
        _ingest(store, self._make_steps(ver=10, count=1000))
        # Step 2: unversioned update (no client_record_version)
        _ingest(store, self._make_steps(ver=None, count=999))
        # Step 3: older version=9 — should still be rejected even after step 2
        _ingest(store, self._make_steps(ver=9, count=888))
        store.close()
        conn = open_ro(db_path)
        try:
            row = conn.execute(
                "SELECT record_json, client_record_version FROM health_records "
                "WHERE record_type='steps'"
            ).fetchone()
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(json.loads(row["record_json"])["count"], 1000,
            "Versionless update must not overwrite versioned record (v10 count must remain 1000)")
        self.assertEqual(row["client_record_version"], 10,
            "client_record_version watermark must be preserved after versionless update attempt")

    def test_newer_version_still_accepted(self):
        """A newer version (v10 → v11) must still be accepted."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, self._make_steps(ver=10, count=1000))
        _ingest(store, self._make_steps(ver=11, count=1100))
        store.close()
        conn = open_ro(db_path)
        try:
            row = conn.execute(
                "SELECT record_json FROM health_records WHERE record_type='steps'"
            ).fetchone()
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(json.loads(row["record_json"])["count"], 1100,
            "Newer version (v11 > v10) must be accepted")

    def test_equal_version_is_idempotent(self):
        """Same version twice is idempotent (UPDATE with same values)."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        _ingest(store, self._make_steps(ver=5, count=500))
        _ingest(store, self._make_steps(ver=5, count=500))
        store.close()
        conn = open_ro(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='steps'"
            ).fetchone()["n"]
        finally:
            conn.close()
        db_path.unlink(missing_ok=True)
        self.assertEqual(count, 1, "Same version twice must stay as one record")


# ── Issue 2c: Unknown/missing-origin/device source provenance ambiguity ──────

class TestIssue2cUnknownSourceProvenanceAmbiguity(unittest.TestCase):
    """
    SYNTHETIC — no metadata or absent origin/device must produce total=null with
    an explicit ambiguity_warning for steps.  Known complete sources keep their
    authoritative total.  Distinct sources are never combined.
    """

    def _ingest_steps(self, store, meta, start="2026-01-15T06:00:00Z", end="2026-01-15T07:00:00Z", count=1000):
        store.ingest_payload(
            minimal_payload(steps=[{"count": count, "start_time": start, "end_time": end,
                                    **({"metadata": meta} if meta is not None else {})}]),
            _now_z(),
        )

    def _query(self, db_path):
        conn = open_ro(db_path)
        try:
            return query_day(conn, "2026-01-15", "Europe/Berlin")
        finally:
            conn.close()

    def test_no_metadata_steps_total_null_with_warning(self):
        """Steps with no metadata at all must have total=null and an ambiguity_warning."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        self._ingest_steps(store, meta=None)
        store.close()
        result = self._query(db_path)
        db_path.unlink(missing_ok=True)
        self.assertEqual(len(result["sources"]), 1)
        src = next(iter(result["sources"].values()))
        steps = src["steps"]
        self.assertIsNone(steps["total"],
            "No-metadata source steps must have total=null")
        self.assertIn("ambiguity_warning", steps,
            "No-metadata source steps must have an ambiguity_warning")
        self.assertIn("provenance_flags", src,
            "No-metadata source must expose provenance_flags in source entry")
        self.assertTrue(src["provenance_flags"].get("no_metadata"),
            "provenance_flags must include no_metadata=True")
        # Diagnostic subtotal must be present but labeled not-authoritative
        self.assertIn("raw_unallocated_subtotal", steps)
        self.assertIn("raw_unallocated_note", steps)

    def test_absent_origin_steps_total_null_with_warning(self):
        """Metadata with no data_origin must cause total=null for steps."""
        meta_no_origin = {"recording_method": 2, "device": {"manufacturer": "SyntheticCorp", "model": "T1", "type": 3}}
        db_path = make_temp_db()
        store = HealthStore(db_path)
        self._ingest_steps(store, meta=meta_no_origin)
        store.close()
        result = self._query(db_path)
        db_path.unlink(missing_ok=True)
        src = next(iter(result["sources"].values()))
        steps = src["steps"]
        self.assertIsNone(steps["total"],
            "Absent-origin source steps must have total=null")
        self.assertIn("ambiguity_warning", steps)
        self.assertTrue(src.get("provenance_flags", {}).get("unknown_origin"),
            "provenance_flags must include unknown_origin=True")

    def test_absent_device_steps_total_null_with_warning(self):
        """Metadata with origin but no device must cause total=null for steps."""
        meta_no_device = {"data_origin": "com.synthetic.testapp", "recording_method": 2}
        db_path = make_temp_db()
        store = HealthStore(db_path)
        self._ingest_steps(store, meta=meta_no_device)
        store.close()
        result = self._query(db_path)
        db_path.unlink(missing_ok=True)
        src = next(iter(result["sources"].values()))
        steps = src["steps"]
        self.assertIsNone(steps["total"],
            "Absent-device source steps must have total=null")
        self.assertIn("ambiguity_warning", steps)
        self.assertTrue(src.get("provenance_flags", {}).get("unknown_device"),
            "provenance_flags must include unknown_device=True")

    def test_complete_known_source_still_totals(self):
        """Full origin+device metadata → disjoint steps get an authoritative total."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        # Two disjoint contained intervals
        self._ingest_steps(store, meta=META_DEVICE_A, count=600,
                           start="2026-01-15T06:00:00Z", end="2026-01-15T08:00:00Z")
        self._ingest_steps(store, meta=META_DEVICE_A, count=400,
                           start="2026-01-15T09:00:00Z", end="2026-01-15T11:00:00Z")
        store.close()
        result = self._query(db_path)
        db_path.unlink(missing_ok=True)
        src = next(iter(result["sources"].values()))
        steps = src["steps"]
        self.assertEqual(steps["total"], 1000,
            "Complete known source with disjoint intervals must have authoritative total")
        self.assertNotIn("ambiguity_warning", steps)
        self.assertNotIn("provenance_flags", src,
            "Complete known source must not emit provenance_flags")

    def test_distinct_sources_never_combined(self):
        """Two different complete sources must appear as separate entries with separate totals."""
        db_path = make_temp_db()
        store = HealthStore(db_path)
        self._ingest_steps(store, meta=META_DEVICE_A, count=500,
                           start="2026-01-15T06:00:00Z", end="2026-01-15T07:00:00Z")
        self._ingest_steps(store, meta=META_DEVICE_B, count=300,
                           start="2026-01-15T08:00:00Z", end="2026-01-15T09:00:00Z")
        store.close()
        result = self._query(db_path)
        db_path.unlink(missing_ok=True)
        self.assertEqual(len(result["sources"]), 2,
            "Two distinct source keys must produce two separate source entries")
        totals = [s["steps"]["total"] for s in result["sources"].values()]
        self.assertIn(500, totals, "Source A total must be 500")
        self.assertIn(300, totals, "Source B total must be 300")
        self.assertNotIn(800, totals, "Cross-source sum (800) must never appear")


if __name__ == "__main__":
    unittest.main()

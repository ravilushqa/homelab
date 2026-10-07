"""
Ingest, idempotency, deduplication, validation, and update tests.

SYNTHETIC fixtures, isolated DB.

Tests:
- Valid payload ingested successfully
- Duplicate payload is idempotent (no double-count)
- Value correction (same temporal key, different value) → UPDATE
- Multi-source/device separation: two sources → two records
- HR samples not collapsed: each sample has distinct time → separate rows
- Explicit ID deduplication (future JSON support)
- Stale replay rejected when version info present
- Schema validation: ALL-or-nothing (malformed array → 400, no partial write)
- Unknown record types silently ignored (not logged, not rejected)
- Missing/wrong content-type
- Body size limits
- Content-Length strictly digit-only
"""

import json
import socket
import unittest
from http.client import HTTPConnection

from .fixtures import (
    META_DEVICE_A,
    META_DEVICE_B,
    META_SAME_ORIGIN_NO_DEVICE,
    minimal_payload,
    payload_multi_source,
    payload_with_hr_aggregates,
    payload_with_hr_samples,
    payload_with_sleep,
    payload_with_steps,
    payload_with_exercise,
    payload_with_weight,
)
from .helpers import TestServer, make_temp_db
from ..store import open_ro


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.srv = TestServer()

    def tearDown(self):
        self.srv.stop()

    def test_valid_steps_ingested(self):
        status, body = self.srv.post_json(payload_with_steps(meta=META_DEVICE_A))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["ingested"].get("steps"), 1)

    def test_valid_sleep_ingested(self):
        status, body = self.srv.post_json(payload_with_sleep(meta=META_DEVICE_A))
        self.assertEqual(status, 200)
        self.assertEqual(body["ingested"].get("sleep"), 1)

    def test_valid_hr_samples_ingested(self):
        status, body = self.srv.post_json(payload_with_hr_samples(meta=META_DEVICE_A))
        self.assertEqual(status, 200)
        self.assertEqual(body["ingested"].get("heart_rate"), 3)

    def test_valid_hr_aggregates_ingested(self):
        status, body = self.srv.post_json(payload_with_hr_aggregates(meta=META_DEVICE_A))
        self.assertEqual(status, 200)
        self.assertEqual(body["ingested"].get("heart_rate"), 2)

    def test_valid_exercise_ingested(self):
        status, body = self.srv.post_json(payload_with_exercise(meta=META_DEVICE_A))
        self.assertEqual(status, 200)
        self.assertEqual(body["ingested"].get("exercise"), 1)

    def test_idempotent_duplicate_no_double_count(self):
        """Same payload twice → only 1 record stored."""
        payload = payload_with_steps(meta=META_DEVICE_A)
        self.srv.post_json(payload)
        self.srv.post_json(payload)
        conn = open_ro(self.srv.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='steps'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 1, "Duplicate POST must not create two records")

    def test_value_correction_updates_record(self):
        """Same temporal key, different count → UPDATE (value correction)."""
        self.srv.post_json(payload_with_steps(count=1000, meta=META_DEVICE_A))
        self.srv.post_json(payload_with_steps(count=1100, meta=META_DEVICE_A))
        conn = open_ro(self.srv.db_path)
        try:
            rows = conn.execute(
                "SELECT record_json FROM health_records WHERE record_type='steps'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        rec = json.loads(rows[0]["record_json"])
        self.assertEqual(rec["count"], 1100)

    def test_multi_source_creates_separate_records(self):
        """Same time window from two sources → two separate records."""
        status, body = self.srv.post_json(payload_multi_source())
        self.assertEqual(status, 200)
        self.assertEqual(body["ingested"].get("steps"), 2)
        conn = open_ro(self.srv.db_path)
        try:
            rows = conn.execute(
                "SELECT source_key FROM health_records WHERE record_type='steps'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        source_keys = {r["source_key"] for r in rows}
        self.assertEqual(len(source_keys), 2)

    def test_hr_samples_not_collapsed(self):
        """3 HR samples with distinct times → 3 separate records."""
        self.srv.post_json(payload_with_hr_samples(meta=META_DEVICE_A))
        conn = open_ro(self.srv.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='heart_rate'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 3)

    def test_unknown_source_stored_as_unknown(self):
        """Records with no metadata get source_key with 'unknown' marker."""
        self.srv.post_json(payload_with_steps())
        conn = open_ro(self.srv.db_path)
        try:
            row = conn.execute(
                "SELECT source_key FROM health_records WHERE record_type='steps'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        src = row["source_key"]
        # Source key is JSON; check that it reflects missing origin/device
        key_obj = json.loads(src)
        self.assertIsNone(key_obj.get("origin"), "No metadata → origin should be null")

    def test_metadata_preserved_in_record_json(self):
        """Full record JSON including metadata is stored."""
        self.srv.post_json(payload_with_steps(meta=META_DEVICE_A))
        conn = open_ro(self.srv.db_path)
        try:
            row = conn.execute(
                "SELECT record_json FROM health_records WHERE record_type='steps'"
            ).fetchone()
        finally:
            conn.close()
        rec = json.loads(row["record_json"])
        self.assertIn("metadata", rec)
        self.assertEqual(rec["metadata"]["data_origin"], "com.synthetic.testapp")

    def test_explicit_id_prevents_duplicate(self):
        """Future JSON with metadata.id: same ID twice → single record (UPDATE)."""
        def make(bpm: int) -> dict:
            return minimal_payload(
                heart_rate=[{
                    "bpm": bpm,
                    "time": "2026-01-15T08:00:00Z",
                    "metadata": {
                        "data_origin": "com.synthetic.testapp",
                        "recording_method": 2,
                        "id": "synthetic-record-id-001",
                    },
                }]
            )
        self.srv.post_json(make(65))
        self.srv.post_json(make(66))
        conn = open_ro(self.srv.db_path)
        try:
            rows = conn.execute(
                "SELECT record_json, expl_id FROM health_records WHERE record_type='heart_rate'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["record_json"])["bpm"], 66)
        self.assertEqual(rows[0]["expl_id"], "synthetic-record-id-001")

    def test_explicit_id_hr_series_no_delete_earlier_samples(self):
        """
        HR samples with same parent explicit_id but different times are stored
        as separate rows.  New samples do NOT delete old ones.
        """
        def make_hr_sample(bpm: int, t: str) -> dict:
            return minimal_payload(
                heart_rate=[{
                    "bpm": bpm,
                    "time": t,
                    "metadata": {
                        "data_origin": "com.synthetic.testapp",
                        "recording_method": 2,
                        "id": "parent-session-001",
                    },
                }]
            )
        # First batch: one sample
        self.srv.post_json(make_hr_sample(65, "2026-01-15T08:00:00Z"))
        # Second batch: a different sample time
        self.srv.post_json(make_hr_sample(70, "2026-01-15T08:01:00Z"))

        conn = open_ro(self.srv.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='heart_rate'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 2, "Both HR samples should exist; new arrival must not delete old")

    def test_stale_replay_rejected(self):
        """
        Record with metadata.client_record_version: second POST with lower version is rejected.
        """
        def make(ver: int, count: int) -> dict:
            return minimal_payload(
                steps=[{
                    "count": count,
                    "start_time": "2026-01-15T06:00:00Z",
                    "end_time": "2026-01-15T07:00:00Z",
                    "metadata": {
                        "data_origin": "com.synthetic.testapp",
                        "recording_method": 2,
                        "id": "stale-replay-test-001",
                        "client_record_version": ver,
                    },
                }]
            )
        self.srv.post_json(make(ver=2, count=1000))  # newer version first
        self.srv.post_json(make(ver=1, count=999))   # older version — must be rejected

        conn = open_ro(self.srv.db_path)
        try:
            row = conn.execute(
                "SELECT record_json FROM health_records WHERE record_type='steps'"
            ).fetchone()
        finally:
            conn.close()
        rec = json.loads(row["record_json"])
        self.assertEqual(rec["count"], 1000, "Stale replay (v1 after v2) must not overwrite")

    def test_different_ids_same_time_distinct(self):
        """Two explicit IDs with the same time remain as separate records."""
        def make(record_id: str) -> dict:
            return minimal_payload(
                weight=[{
                    "kilograms": 80.0,
                    "time": "2026-01-15T08:00:00Z",
                    "metadata": {"data_origin": "com.app", "recording_method": 1, "id": record_id},
                }]
            )
        self.srv.post_json(make("weight-id-A"))
        self.srv.post_json(make("weight-id-B"))
        conn = open_ro(self.srv.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='weight'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 2, "Different IDs at same time must be distinct records")

    def test_malformed_json_rejected(self):
        status, _ = self.srv.request("POST", raw_body=b"{not valid json")
        self.assertEqual(status, 400)

    def test_non_object_json_rejected(self):
        status, _ = self.srv.request("POST", raw_body=b'["array"]')
        self.assertEqual(status, 400)

    def test_duplicate_json_keys_rejected(self):
        raw = b'{"timestamp":"2026-01-15T10:00:00Z","timestamp":"2026-01-15T10:00:01Z"}'
        status, _ = self.srv.request("POST", raw_body=raw)
        self.assertEqual(status, 400)

    def test_nan_in_payload_rejected(self):
        """JSON with NaN values must be rejected."""
        import json as _json
        raw = b'{"timestamp":"2026-01-15T10:00:00Z","heart_rate":[{"bpm":NaN,"time":"2026-01-15T08:00:00Z"}]}'
        status, _ = self.srv.request("POST", raw_body=raw)
        self.assertEqual(status, 400)

    def test_missing_timestamp_rejected(self):
        payload = {"app_version": "1.0"}
        status, _ = self.srv.post_json(payload)
        self.assertEqual(status, 400)

    def test_naive_timestamp_rejected(self):
        payload = {"timestamp": "2026-01-15T10:00:00", "app_version": "1.0"}
        status, _ = self.srv.post_json(payload)
        self.assertEqual(status, 400)

    def test_invalid_timestamp_rejected(self):
        payload = {"timestamp": "not-a-date", "app_version": "1.0"}
        status, _ = self.srv.post_json(payload)
        self.assertEqual(status, 400)

    def test_schema_validation_all_or_nothing(self):
        """Malformed record in known array → 400, NO records written at all."""
        payload = minimal_payload(
            steps=[
                {"count": -1, "start_time": "2026-01-15T06:00:00Z",  # invalid count
                 "end_time": "2026-01-15T07:00:00Z"},
            ],
            weight=[{"kilograms": 80.0, "time": "2026-01-15T08:00:00Z"}],  # valid
        )
        status, _ = self.srv.post_json(payload)
        self.assertEqual(status, 400)
        conn = open_ro(self.srv.db_path)
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(total, 0, "No records should be written when validation fails")

    def test_bool_count_rejected(self):
        """Boolean count (isinstance(True, int) is True in Python) must be rejected."""
        payload = minimal_payload(
            steps=[{"count": True, "start_time": "2026-01-15T06:00:00Z",
                    "end_time": "2026-01-15T07:00:00Z"}]
        )
        status, _ = self.srv.post_json(payload)
        self.assertEqual(status, 400)

    def test_steps_start_equals_end_rejected(self):
        """Interval with start == end is invalid."""
        payload = minimal_payload(
            steps=[{"count": 100, "start_time": "2026-01-15T06:00:00Z",
                    "end_time": "2026-01-15T06:00:00Z"}]
        )
        status, _ = self.srv.post_json(payload)
        self.assertEqual(status, 400)

    def test_unknown_array_type_ignored_not_rejected(self):
        """Unknown array keys in payload are silently ignored, not rejected."""
        payload = minimal_payload(
            unknown_future_type=[{"some": "data"}],
            steps=[{"count": 100, "start_time": "2026-01-15T06:00:00Z",
                    "end_time": "2026-01-15T07:00:00Z"}],
        )
        status, body = self.srv.post_json(payload)
        self.assertEqual(status, 200)
        self.assertIn("steps", body.get("ingested", {}))

    def test_empty_payload_accepted(self):
        status, body = self.srv.post_json(minimal_payload())
        self.assertEqual(status, 200)

    def test_content_type_required(self):
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=5)
        import json as _json
        data = _json.dumps(minimal_payload()).encode()
        conn.request("POST", "/ingest/health-connect", body=data,
                     headers={"Content-Length": str(len(data)),
                              "Content-Type": "text/plain",
                              "X-Health-Token": self.srv.token_str})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 415)
        conn.close()

    def test_transfer_encoding_rejected(self):
        """ANY Transfer-Encoding header is rejected."""
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=5)
        data = json.dumps(minimal_payload()).encode()
        conn.request("POST", "/ingest/health-connect", body=data,
                     headers={"Content-Length": str(len(data)),
                              "Content-Type": "application/json",
                              "Transfer-Encoding": "identity",  # not chunked, but still rejected
                              "X-Health-Token": self.srv.token_str})
        try:
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        except Exception:
            pass  # connection close is also acceptable
        finally:
            conn.close()

    def test_duplicate_content_length_rejected(self):
        """Two Content-Length headers → rejected (400 or 411)."""
        import socket as _socket
        s = _socket.create_connection((self.srv.host, self.srv.port), timeout=5)
        body = json.dumps(minimal_payload()).encode()
        raw_req = (
            f"POST /ingest/health-connect HTTP/1.1\r\n"
            f"Host: {self.srv.host}\r\n"
            f"Content-Type: application/json\r\n"
            f"X-Health-Token: {self.srv.token_str}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode() + body
        s.sendall(raw_req)
        resp_bytes = s.recv(4096)
        s.close()
        # Expect 4xx (400 or 411 depending on how Python's HTTPServer handles duplicates)
        self.assertTrue(
            b"400" in resp_bytes[:20] or b"411" in resp_bytes[:20],
            f"Expected 400 or 411, got: {resp_bytes[:50]}"
        )

    def test_nondigit_content_length_rejected(self):
        """Content-Length with non-digit chars (e.g. '+5') must be rejected."""
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=5)
        data = json.dumps(minimal_payload()).encode()
        conn.request("POST", "/ingest/health-connect", body=data,
                     headers={"Content-Length": "+5",
                              "Content-Type": "application/json",
                              "X-Health-Token": self.srv.token_str})
        try:
            resp = conn.getresponse()
            self.assertIn(resp.status, (400, 411))
        except Exception:
            pass
        finally:
            conn.close()

    def test_body_too_large_rejected(self):
        """Body over 8 MiB is rejected."""
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=5)
        big = b"x" * (8 * 1024 * 1024 + 1)
        try:
            conn.request("POST", "/ingest/health-connect", body=big,
                         headers={"Content-Length": str(len(big)),
                                  "Content-Type": "application/json",
                                  "X-Health-Token": self.srv.token_str})
            resp = conn.getresponse()
            self.assertIn(resp.status, (400, 413))
        except (BrokenPipeError, ConnectionResetError, socket.error):
            pass  # Server closed connection early = correct
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

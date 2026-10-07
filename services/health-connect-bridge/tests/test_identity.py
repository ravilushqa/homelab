"""
Identity / source key / temporal key / explicit ID unit tests.

SYNTHETIC — no production data.
"""

import json
import unittest

from ..identity import (
    build_source_key,
    build_temporal_key,
    build_identity_key,
    extract_explicit_id,
    source_key_flags,
    normalise_ts_to_epoch_us,
    parse_aware_dt,
    SERIES_TYPES,
)


class TestSourceKey(unittest.TestCase):
    def test_full_metadata_includes_all_fields(self):
        meta = {
            "data_origin": "com.example.app",
            "recording_method": 2,
            "device": {"manufacturer": "ACME", "model": "Widget-1", "type": 3},
        }
        key = build_source_key(meta)
        obj = json.loads(key)
        self.assertEqual(obj["origin"], "com.example.app")
        self.assertEqual(obj["method"], 2)
        self.assertEqual(obj["device"]["manufacturer"], "ACME")
        self.assertEqual(obj["device"]["model"], "Widget-1")
        self.assertEqual(obj["device"]["type"], 3)

    def test_recording_method_included(self):
        meta_m1 = {"data_origin": "com.app", "recording_method": 1,
                   "device": {"manufacturer": "X", "model": "Y", "type": 0}}
        meta_m2 = {"data_origin": "com.app", "recording_method": 2,
                   "device": {"manufacturer": "X", "model": "Y", "type": 0}}
        self.assertNotEqual(build_source_key(meta_m1), build_source_key(meta_m2))

    def test_device_type_included(self):
        meta_t1 = {"data_origin": "com.app", "recording_method": 1,
                   "device": {"manufacturer": "X", "model": "Y", "type": 1}}
        meta_t2 = {"data_origin": "com.app", "recording_method": 1,
                   "device": {"manufacturer": "X", "model": "Y", "type": 2}}
        self.assertNotEqual(build_source_key(meta_t1), build_source_key(meta_t2))

    def test_no_metadata_encoded_as_no_metadata(self):
        key = build_source_key(None)
        obj = json.loads(key)
        self.assertTrue(obj.get("_no_metadata"))

    def test_missing_device_uses_null(self):
        meta = {"data_origin": "com.example.app", "recording_method": 1}
        key = build_source_key(meta)
        obj = json.loads(key)
        self.assertIsNone(obj["device"])

    def test_collision_free_special_chars(self):
        """Pipe, slash, colon in field values cannot create collisions."""
        meta_a = {"data_origin": "a|b", "recording_method": 1, "device": None}
        meta_b = {"data_origin": "a", "recording_method": 1,
                  "device": {"manufacturer": None, "model": None, "type": None}}
        self.assertNotEqual(build_source_key(meta_a), build_source_key(meta_b))

    def test_two_different_devices_different_keys(self):
        meta_a = {"data_origin": "com.app", "recording_method": 1,
                  "device": {"manufacturer": "A", "model": "M", "type": 0}}
        meta_b = {"data_origin": "com.app", "recording_method": 1,
                  "device": {"manufacturer": "A", "model": "N", "type": 0}}
        self.assertNotEqual(build_source_key(meta_a), build_source_key(meta_b))

    def test_flags_unknown_origin(self):
        flags = source_key_flags({"recording_method": 1})
        self.assertTrue(flags.get("unknown_origin"))

    def test_flags_no_metadata(self):
        flags = source_key_flags(None)
        self.assertTrue(flags.get("no_metadata"))


class TestExplicitId(unittest.TestCase):
    def test_no_id_returns_none(self):
        meta = {"data_origin": "com.app", "recording_method": 2}
        self.assertIsNone(extract_explicit_id(meta))

    def test_server_id_extracted_as_tuple(self):
        meta = {"id": "record-uuid-001"}
        result = extract_explicit_id(meta)
        self.assertIsNotNone(result)
        ns, val = result
        self.assertEqual(ns, "server")
        self.assertEqual(val, "record-uuid-001")

    def test_client_record_id_extracted(self):
        meta = {"client_record_id": "client-001"}
        ns, val = extract_explicit_id(meta)
        self.assertEqual(ns, "client")
        self.assertEqual(val, "client-001")

    def test_server_id_takes_precedence(self):
        meta = {"id": "server-id", "client_record_id": "client-id"}
        ns, val = extract_explicit_id(meta)
        self.assertEqual(ns, "server")
        self.assertEqual(val, "server-id")

    def test_none_metadata_returns_none(self):
        self.assertIsNone(extract_explicit_id(None))


class TestTemporalKey(unittest.TestCase):
    def test_z_and_offset_normalize_to_same(self):
        """Z and +00:00 must produce the same temporal key (epoch-µs)."""
        rec_z = {"count": 100, "start_time": "2026-01-15T06:00:00Z",
                 "end_time": "2026-01-15T07:00:00Z"}
        rec_off = {"count": 100, "start_time": "2026-01-15T06:00:00+00:00",
                   "end_time": "2026-01-15T07:00:00+00:00"}
        self.assertEqual(
            build_temporal_key("steps", rec_z),
            build_temporal_key("steps", rec_off),
        )

    def test_fractional_seconds_same(self):
        """Sub-second precision preserved: .123Z == .123+00:00."""
        t1 = normalise_ts_to_epoch_us("2026-01-15T08:00:00.123Z")
        t2 = normalise_ts_to_epoch_us("2026-01-15T08:00:00.123+00:00")
        self.assertEqual(t1, t2)

    def test_steps_missing_start_returns_none(self):
        rec = {"count": 100, "end_time": "2026-01-15T07:00:00Z"}
        self.assertIsNone(build_temporal_key("steps", rec))

    def test_different_times_different_keys(self):
        rec1 = {"bpm": 72, "time": "2026-01-15T08:00:00Z"}
        rec2 = {"bpm": 72, "time": "2026-01-15T08:01:00Z"}
        self.assertNotEqual(
            build_temporal_key("heart_rate", rec1),
            build_temporal_key("heart_rate", rec2),
        )

    def test_sleep_uses_end_time(self):
        rec = {"session_end_time": "2026-01-15T07:30:00Z",
               "duration_seconds": 28800, "stages": []}
        key = build_temporal_key("sleep", rec)
        self.assertIsNotNone(key)
        # Key should encode epoch of session_end_time
        expected_epoch_us = normalise_ts_to_epoch_us("2026-01-15T07:30:00Z")
        self.assertIn(expected_epoch_us, key)


class TestIdentityKey(unittest.TestCase):
    def test_temporal_fallback_includes_source_key(self):
        src = '{"origin":"com.app","method":1,"device":null}'
        tkey = "iv:12345:67890"
        ikey = build_identity_key("steps", src, tkey, None, None)
        decoded = json.loads(ikey)
        self.assertEqual(decoded[0], "t")
        self.assertEqual(decoded[1], "steps")
        self.assertEqual(decoded[2], src)

    def test_explicit_ordinary_namespaced_by_origin(self):
        """
        Explicit IDs are namespaced by data_origin so that two apps with the same
        client_record_id but different origins remain distinct records.
        Device metadata (manufacturer/model) is NOT part of identity.
        """
        # Same origin, different device metadata → same identity (device not in key)
        src_same_origin_dev_a = '{"origin":"com.app","method":1,"device":{"manufacturer":"A","model":"X","type":1}}'
        src_same_origin_dev_b = '{"origin":"com.app","method":1,"device":{"manufacturer":"B","model":"Y","type":2}}'
        expl = ("server", "record-001")
        tkey = "i:12345"
        ikey_dev_a = build_identity_key("weight", src_same_origin_dev_a, tkey, expl, None, origin="com.app")
        ikey_dev_b = build_identity_key("weight", src_same_origin_dev_b, tkey, expl, None, origin="com.app")
        self.assertEqual(ikey_dev_a, ikey_dev_b,
                         "Same origin + explicit ID must have same identity regardless of device metadata")

        # Different origins, same explicit ID → different identities
        ikey_origin_a = build_identity_key("weight", src_same_origin_dev_a, tkey, expl, None, origin="com.app.a")
        ikey_origin_b = build_identity_key("weight", src_same_origin_dev_a, tkey, expl, None, origin="com.app.b")
        self.assertNotEqual(ikey_origin_a, ikey_origin_b,
                            "Same explicit ID from different origins must be distinct records")

        # No origin → falls back to temporal key (not explicit ID path)
        ikey_no_origin = build_identity_key("weight", src_same_origin_dev_a, tkey, expl, None, origin=None)
        decoded = json.loads(ikey_no_origin)
        self.assertEqual(decoded[0], "t", "Missing origin must fall back to temporal key")

    def test_series_explicit_uses_sample_disc(self):
        expl = ("server", "session-123")
        disc_t1 = "1737000000000000"
        disc_t2 = "1737000060000000"
        src = '{"origin":"com.app","method":1,"device":null}'
        ikey_t1 = build_identity_key("heart_rate", src, None, expl, disc_t1, origin="com.app")
        ikey_t2 = build_identity_key("heart_rate", src, None, expl, disc_t2, origin="com.app")
        self.assertNotEqual(ikey_t1, ikey_t2)
        d1 = json.loads(ikey_t1)
        self.assertEqual(d1[0], "es")
        # Format: ["es", type, ns, origin, id, disc]
        self.assertEqual(d1[5], disc_t1)

    def test_no_temporal_key_no_explicit_returns_none(self):
        self.assertIsNone(build_identity_key("steps", "src", None, None, None))

    def test_collision_free_across_types(self):
        src = "src"
        tkey = "iv:1:2"
        ikey_steps = build_identity_key("steps", src, tkey, None, None)
        ikey_distance = build_identity_key("distance", src, tkey, None, None)
        self.assertNotEqual(ikey_steps, ikey_distance)


class TestTimeParsing(unittest.TestCase):
    def test_naive_datetime_raises(self):
        with self.assertRaises(ValueError):
            parse_aware_dt("2026-01-15T10:00:00")

    def test_z_suffix_parsed(self):
        dt = parse_aware_dt("2026-01-15T10:00:00Z")
        self.assertIsNotNone(dt.tzinfo)

    def test_offset_parsed(self):
        dt = parse_aware_dt("2026-01-15T11:00:00+01:00")
        self.assertIsNotNone(dt.tzinfo)

    def test_z_and_plus00_equal_epoch(self):
        e1 = parse_aware_dt("2026-01-15T10:00:00Z").timestamp()
        e2 = parse_aware_dt("2026-01-15T10:00:00+00:00").timestamp()
        self.assertEqual(e1, e2)


if __name__ == "__main__":
    unittest.main()

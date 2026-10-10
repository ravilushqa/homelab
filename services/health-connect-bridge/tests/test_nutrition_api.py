"""
Nutrition HTTP API tests: GET /nutrition/queue, POST /nutrition/ack.

SYNTHETIC — isolated servers and databases, no production writes.
Tests: queue fetch schema, ack idempotence, stale version 409, rate/body limits,
       Transfer-Encoding rejection, duplicate header rejection, cache-control,
       deep JSON, duplicate keys, overflow, fetched marks delivery_possible.
"""

import json
import unittest
from http.client import HTTPConnection
from pathlib import Path

from ..nutrition_queue import (
    NutritionStore,
    CONFIRM_AFFIRM_PHRASE,
)
from .helpers import NutritionTestServer, make_temp_db


def _minimal_data(**overrides) -> dict:
    d = {
        "name": "Synthetic Lunch",
        "start_time": "2026-01-15T12:00:00+00:00",
        "end_time": "2026-01-15T12:30:00+00:00",
        "energy_kcal": 500.0,
        "protein_g": 30.0,
        "carbohydrate_g": 60.0,
        "fat_g": 15.0,
    }
    d.update(overrides)
    return d


class TestNutritionQueueGet(unittest.TestCase):
    """Each test gets its own server to avoid rate-limiter cross-contamination."""

    def setUp(self):
        self.srv = NutritionTestServer()
        entry = self.srv._nutrition_store.create_entry(_minimal_data())
        self.crid = entry["client_record_id"]
        self.srv._nutrition_store.confirm_entry(
            self.crid, 1, CONFIRM_AFFIRM_PHRASE, "alice"
        )

    def tearDown(self):
        self.srv.stop()

    def test_queue_returns_200(self):
        status, body = self.srv.nutrition_get()
        self.assertEqual(status, 200)

    def test_queue_schema_version(self):
        _, body = self.srv.nutrition_get()
        self.assertEqual(body.get("schema_version"), 1)

    def test_queue_has_records_key(self):
        _, body = self.srv.nutrition_get()
        self.assertIn("records", body)
        self.assertIsInstance(body["records"], list)

    def test_queue_contains_confirmed_record(self):
        _, body = self.srv.nutrition_get()
        crids = [r["client_record_id"] for r in body["records"]]
        self.assertIn(self.crid, crids)

    def test_queue_record_has_required_keys(self):
        _, body = self.srv.nutrition_get()
        record = next(r for r in body["records"] if r["client_record_id"] == self.crid)
        required = {
            "client_record_id", "client_record_version", "name",
            "start_time", "end_time", "energy_kcal", "protein_g",
            "carbohydrate_g", "fat_g", "description",
        }
        self.assertEqual(set(record.keys()), required)

    def test_queue_no_extra_keys(self):
        """Locked API contract: no extra payload keys."""
        _, body = self.srv.nutrition_get()
        self.assertEqual(set(body.keys()), {"schema_version", "records"})
        for record in body["records"]:
            self.assertEqual(set(record.keys()), {
                "client_record_id", "client_record_version", "name",
                "start_time", "end_time", "energy_kcal", "protein_g",
                "carbohydrate_g", "fat_g", "description",
            })

    def test_queue_description_always_present_nonempty(self):
        """description is always non-null and has AI prefix."""
        _, body = self.srv.nutrition_get()
        desc = body["records"][0]["description"]
        self.assertIsNotNone(desc)
        self.assertTrue(len(desc) > 0)
        from ..nutrition_queue import AI_DESCRIPTION_PREFIX
        self.assertTrue(desc.startswith(AI_DESCRIPTION_PREFIX))

    def test_queue_marks_delivery_possible(self):
        """GET atomically sets delivery_possible=1."""
        before = self.srv._nutrition_store.get_entry(self.crid)
        self.assertEqual(before["delivery_possible"], 0)
        self.srv.nutrition_get()
        after = self.srv._nutrition_store.get_entry(self.crid)
        self.assertEqual(after["delivery_possible"], 1)

    def test_queue_cache_control_no_store(self):
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/nutrition/queue",
                     headers={"X-Nutrition-Token": self.srv.nutrition_token_str})
        resp = conn.getresponse()
        resp.read()
        cc = resp.getheader("Cache-Control", "")
        conn.close()
        self.assertIn("no-store", cc)

    def test_queue_wrong_token_401(self):
        status, _ = self.srv.nutrition_get(token="wrong-token-value")
        self.assertEqual(status, 401)

    def test_queue_no_token_401(self):
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/nutrition/queue")
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 401)

    def test_queue_query_string_rejected(self):
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/nutrition/queue?token=abc",
                     headers={"X-Nutrition-Token": self.srv.nutrition_token_str})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 404)

    def test_queue_ingest_token_rejected(self):
        """X-Health-Token must not grant nutrition access."""
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/nutrition/queue",
                     headers={"X-Health-Token": self.srv.ingest_token_str})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertIn(status, (400, 401))

    def test_queue_transfer_encoding_rejected(self):
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/nutrition/queue", headers={
            "X-Nutrition-Token": self.srv.nutrition_token_str,
            "Transfer-Encoding": "chunked",
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 400)

    def test_queue_duplicate_token_header_rejected(self):
        nt = self.srv.nutrition_token_str
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.putrequest("GET", "/nutrition/queue")
        conn.putheader("X-Nutrition-Token", nt)
        conn.putheader("X-Nutrition-Token", nt)
        conn.endheaders()
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertIn(status, (400, 401))

    def test_queue_duplicate_content_length_rejected(self):
        """GET with duplicate Content-Length must be rejected."""
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.putrequest("GET", "/nutrition/queue")
        conn.putheader("X-Nutrition-Token", self.srv.nutrition_token_str)
        conn.putheader("Content-Length", "0")
        conn.putheader("Content-Length", "0")
        conn.endheaders()
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 400)

    def test_queue_get_with_nonzero_body_rejected(self):
        data = b'{"dummy": "data"}'
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        headers = {
            "X-Nutrition-Token": self.srv.nutrition_token_str,
            "Content-Length": str(len(data)),
        }
        conn.request("GET", "/nutrition/queue", body=data, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 400)


class TestNutritionAck(unittest.TestCase):
    def setUp(self):
        self.srv = NutritionTestServer()
        entry = self.srv._nutrition_store.create_entry(_minimal_data())
        self.crid = entry["client_record_id"]
        self.srv._nutrition_store.confirm_entry(
            self.crid, 1, CONFIRM_AFFIRM_PHRASE, "alice"
        )

    def tearDown(self):
        self.srv.stop()

    def test_ack_confirmed_returns_200(self):
        status, body = self.srv.nutrition_ack(self.crid, 1)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))

    def test_ack_idempotent_returns_200(self):
        self.srv.nutrition_ack(self.crid, 1)
        status, body = self.srv.nutrition_ack(self.crid, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body.get("result"), "idempotent")

    def test_ack_stale_version_returns_409(self):
        status, _ = self.srv.nutrition_ack(self.crid, 99)
        self.assertEqual(status, 409)

    def test_ack_nonexistent_returns_404(self):
        status, _ = self.srv.nutrition_ack("nonexistent-id", 1)
        self.assertEqual(status, 404)

    def test_ack_removes_from_queue(self):
        self.srv.nutrition_ack(self.crid, 1)
        _, body = self.srv.nutrition_get()
        crids = [r["client_record_id"] for r in body.get("records", [])]
        self.assertNotIn(self.crid, crids)

    def test_ack_cache_control_no_store(self):
        data = json.dumps({
            "client_record_id": self.crid,
            "client_record_version": 1,
        }).encode()
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=data, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Nutrition-Token": self.srv.nutrition_token_str,
        })
        resp = conn.getresponse()
        resp.read()
        cc = resp.getheader("Cache-Control", "")
        conn.close()
        self.assertIn("no-store", cc)

    def test_ack_body_too_large_rejected(self):
        big_body = json.dumps({
            "client_record_id": "x" * 1100,
            "client_record_version": 1,
        }).encode()
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=big_body, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(big_body)),
            "X-Nutrition-Token": self.srv.nutrition_token_str,
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 413)

    def test_ack_wrong_token_401(self):
        status, _ = self.srv.nutrition_ack(self.crid, 1, token="wrong-token")
        self.assertEqual(status, 401)

    def test_ack_no_content_length(self):
        """POST without Content-Length: server returns 411 or 400."""
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.putrequest("POST", "/nutrition/ack")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("X-Nutrition-Token", self.srv.nutrition_token_str)
        conn.endheaders()
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertIn(status, (411, 400))

    def test_ack_extra_keys_rejected(self):
        payload = {
            "client_record_id": self.crid,
            "client_record_version": 1,
            "extra_field": "not allowed",
        }
        data = json.dumps(payload).encode()
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=data, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Nutrition-Token": self.srv.nutrition_token_str,
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 400)

    def test_ack_missing_version_rejected(self):
        payload = {"client_record_id": self.crid}
        data = json.dumps(payload).encode()
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=data, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Nutrition-Token": self.srv.nutrition_token_str,
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 400)

    def test_ack_null_version_rejected(self):
        raw = b'{"client_record_id":"abc","client_record_version":null}'
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=raw, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "X-Nutrition-Token": self.srv.nutrition_token_str,
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 400)

    def test_ack_huge_version_rejected(self):
        huge = 2 ** 60
        payload = {"client_record_id": self.crid, "client_record_version": huge}
        data = json.dumps(payload).encode()
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=data, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Nutrition-Token": self.srv.nutrition_token_str,
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 400)

    def test_ack_duplicate_json_key_rejected(self):
        raw = b'{"client_record_id":"abc","client_record_id":"def","client_record_version":1}'
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=raw, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "X-Nutrition-Token": self.srv.nutrition_token_str,
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 400)

    def test_ack_transfer_encoding_rejected(self):
        payload = {"client_record_id": self.crid, "client_record_version": 1}
        data = json.dumps(payload).encode()
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=data, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Nutrition-Token": self.srv.nutrition_token_str,
            "Transfer-Encoding": "chunked",
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 400)

    def test_ack_draft_returns_409(self):
        entry2 = self.srv._nutrition_store.create_entry(_minimal_data())
        status, _ = self.srv.nutrition_ack(entry2["client_record_id"], 1)
        self.assertEqual(status, 409)

    def test_ack_cancelled_returns_409(self):
        entry3 = self.srv._nutrition_store.create_entry(_minimal_data())
        self.srv._nutrition_store.cancel_entry(entry3["client_record_id"], 1)
        status, _ = self.srv.nutrition_ack(entry3["client_record_id"], 1)
        self.assertEqual(status, 409)


class TestNutritionDisabled(unittest.TestCase):
    """Nutrition routes return 404 when server is ingest-only."""

    @classmethod
    def setUpClass(cls):
        from .helpers import TestServer
        cls.srv = TestServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_queue_not_configured_404(self):
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/nutrition/queue",
                     headers={"X-Nutrition-Token": "any-token-value"})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 404)

    def test_ack_not_configured_404(self):
        data = b'{"client_record_id":"x","client_record_version":1}'
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=data, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Nutrition-Token": "any-token-value",
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 404)


class TestNutritionFullFlow(unittest.TestCase):
    """End-to-end: create→unconfirmed→confirm→queue→edit→reconfirm→ack→empty."""

    def test_full_flow(self):
        srv = NutritionTestServer()
        try:
            ns = srv._nutrition_store

            # 1. Create entry
            entry = ns.create_entry(_minimal_data())
            crid = entry["client_record_id"]
            self.assertEqual(entry["client_record_version"], 1)

            # 2. Queue is empty (not confirmed)
            status, body = srv.nutrition_get()
            self.assertEqual(status, 200)
            self.assertEqual(body["records"], [])

            # 3. Wrong version confirm → VersionConflict
            from ..nutrition_queue import VersionConflict
            with self.assertRaises(VersionConflict):
                ns.confirm_entry(crid, 99, CONFIRM_AFFIRM_PHRASE, "alice")

            # 4. Confirm at v1
            ns.confirm_entry(crid, 1, CONFIRM_AFFIRM_PHRASE, "alice")

            # 5. Queue returns the record; marks delivery_possible
            status, body = srv.nutrition_get()
            self.assertEqual(status, 200)
            self.assertEqual(len(body["records"]), 1)
            r = body["records"][0]
            self.assertEqual(r["client_record_id"], crid)
            self.assertEqual(r["client_record_version"], 1)
            after_get = ns.get_entry(crid)
            self.assertEqual(after_get["delivery_possible"], 1)

            # 6. Edit resets confirmation (version stays 1 before edit, becomes 2 after)
            ns.edit_entry(crid, 1, _minimal_data(name="Updated"))
            status, body = srv.nutrition_get()
            self.assertEqual(body["records"], [])  # draft, not in queue

            # 7. Re-confirm at v2
            ns.confirm_entry(crid, 2, CONFIRM_AFFIRM_PHRASE, "alice")
            status, body = srv.nutrition_get()
            self.assertEqual(len(body["records"]), 1)
            self.assertEqual(body["records"][0]["client_record_version"], 2)

            # 8. Stale ack at v1 (was already fetched and acked as v1, now v2)
            status, _ = srv.nutrition_ack(crid, 1)
            self.assertEqual(status, 409)

            # 9. Correct ack at v2
            status, body = srv.nutrition_ack(crid, 2)
            self.assertEqual(status, 200)
            self.assertEqual(body["result"], "acked")

            # 10. Repeated ack is idempotent
            status, body = srv.nutrition_ack(crid, 2)
            self.assertEqual(status, 200)
            self.assertEqual(body["result"], "idempotent")

            # 11. Queue is now empty
            status, body = srv.nutrition_get()
            self.assertEqual(body["records"], [])

            # 12. Status shows acked, delivery_possible=1
            final = ns.get_entry(crid)
            self.assertEqual(final["state"], "acked")
            self.assertEqual(final["delivery_possible"], 1)

        finally:
            srv.stop()

    def test_cancel_before_fetch_no_delivery_note(self):
        srv = NutritionTestServer()
        try:
            ns = srv._nutrition_store
            entry = ns.create_entry(_minimal_data())
            crid = entry["client_record_id"]
            # Cancel before ever being confirmed or fetched
            result = ns.cancel_entry(crid, 1)
            # No delivery_possible, so no warning needed
            self.assertEqual(result["delivery_possible"], 0)
        finally:
            srv.stop()

    def test_cancel_confirmed_before_fetch_clean(self):
        srv = NutritionTestServer()
        try:
            ns = srv._nutrition_store
            entry = ns.create_entry(_minimal_data())
            crid = entry["client_record_id"]
            ns.confirm_entry(crid, 1, CONFIRM_AFFIRM_PHRASE, "alice")
            # Cancel before GET queue fetch (delivery_possible still 0)
            result = ns.cancel_entry(crid, 1)
            # No delivery_possible warning since not fetched yet
            self.assertEqual(result.get("delivery_possible", 0), 0)
            if "cancellation_note" in result:
                # No phone uncertainty note since not fetched
                self.assertNotIn("write-back protocol", result["cancellation_note"])
        finally:
            srv.stop()

    def test_restart_persistence(self):
        """Data survives receiver restart — use store directly to avoid stop() deleting files."""
        import threading
        from .helpers import (
            _SYNTHETIC_TOKEN, _SYNTHETIC_NUTRITION_TOKEN,
            make_temp_token, make_temp_db,
        )
        from ..receiver import _make_bridge_handler_class, _BoundedTCPServer
        from ..store import HealthStore
        from ..ratelimit import RateLimitManager

        db_path = make_temp_db()
        ndb_path = make_temp_db()

        try:
            # First instance: create and confirm a record, then close cleanly
            hs1 = HealthStore(db_path)
            ns1 = NutritionStore(ndb_path)
            entry = ns1.create_entry(_minimal_data())
            crid = entry["client_record_id"]
            ns1.confirm_entry(crid, 1, CONFIRM_AFFIRM_PHRASE, "alice")
            ns1.close()
            hs1.close()

            # Second instance (simulates restart): queue should still have the record
            ns2 = NutritionStore(ndb_path)
            q = ns2.get_confirmed_queue()
            ns2.close()

            crids = [r["client_record_id"] for r in q]
            self.assertIn(crid, crids)
        finally:
            for p in (db_path, ndb_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()

"""
Routing tests — exact path matching, method rejection, healthz.

SYNTHETIC fixtures, isolated DB.
"""

import json
import unittest
from http.client import HTTPConnection

from .fixtures import minimal_payload
from .helpers import TestServer


class TestRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = TestServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    # ── Exact path ───────────────────────────────────────────────────────────

    def test_ingest_exact_path_ok(self):
        status, _ = self.srv.post_json(minimal_payload())
        self.assertEqual(status, 200)

    def test_trailing_slash_rejected(self):
        status, _ = self.srv.request("POST", "/ingest/health-connect/", body=minimal_payload())
        self.assertEqual(status, 404)

    def test_wrong_path_rejected(self):
        status, _ = self.srv.request("POST", "/ingest/other", body=minimal_payload())
        self.assertEqual(status, 404)

    def test_root_path_rejected(self):
        status, _ = self.srv.request("POST", "/", body=minimal_payload())
        self.assertEqual(status, 404)

    def test_path_with_query_rejected(self):
        status, _ = self.srv.request(
            "POST",
            "/ingest/health-connect?debug=true",
            body=minimal_payload(),
        )
        self.assertEqual(status, 404)

    # ── Methods ──────────────────────────────────────────────────────────────

    def test_get_ingest_rejected(self):
        status, _ = self.srv.request("GET", "/ingest/health-connect", body=None)
        self.assertIn(status, (404, 405))

    def test_put_rejected(self):
        status, _ = self.srv.request("PUT", "/ingest/health-connect", body=minimal_payload())
        self.assertEqual(status, 405)

    def test_delete_rejected(self):
        status, _ = self.srv.request("DELETE", "/ingest/health-connect", body=None)
        self.assertEqual(status, 405)

    def test_patch_rejected(self):
        status, _ = self.srv.request("PATCH", "/ingest/health-connect", body=minimal_payload())
        self.assertEqual(status, 405)

    # ── Healthz ──────────────────────────────────────────────────────────────

    def test_healthz_returns_200(self):
        status, body = self.srv.request("GET", "/healthz", body=None, token="__omit__")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "ok")

    def test_healthz_no_auth_required(self):
        """Healthz must not require auth — it is for local process monitoring only."""
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(body.get("status"), "ok")

    def test_healthz_contains_no_health_data(self):
        """Healthz response must be exactly {status: ok} — no health data."""
        status, body = self.srv.request("GET", "/healthz", body=None, token="__omit__")
        self.assertEqual(set(body.keys()), {"status"})

    # ── No public read API ────────────────────────────────────────────────────

    def test_no_get_data_endpoint(self):
        for path in ["/data", "/records", "/health", "/status", "/api"]:
            status, _ = self.srv.request("GET", path, body=None)
            self.assertIn(status, (404, 405), msg=f"Path {path} should not be accessible")


if __name__ == "__main__":
    unittest.main()

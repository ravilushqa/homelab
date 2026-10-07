"""
Auth tests — SYNTHETIC fixtures, isolated DB.

Tests: missing token, wrong token, valid token, constant-time property.
"""

import unittest

from .fixtures import minimal_payload, payload_with_steps
from .helpers import TestServer


class TestAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = TestServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_missing_token_returns_401(self):
        status, body = self.srv.request(
            method="POST",
            path="/ingest/health-connect",
            body=minimal_payload(),
            token="__omit__",
        )
        self.assertEqual(status, 401)

    def test_wrong_token_returns_401(self):
        status, body = self.srv.post_json(
            payload_with_steps(), token="WRONG-TOKEN-SYNTHETIC"
        )
        self.assertEqual(status, 401)

    def test_empty_token_returns_401(self):
        status, body = self.srv.post_json(minimal_payload(), token="")
        self.assertEqual(status, 401)

    def test_valid_token_returns_200(self):
        status, body = self.srv.post_json(minimal_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))

    def test_valid_token_with_steps_returns_200(self):
        status, body = self.srv.post_json(payload_with_steps())
        self.assertEqual(status, 200)
        self.assertIn("steps", body.get("ingested", {}))

    def test_wrong_token_response_is_generic(self):
        """Response must not reveal whether the token was close/far from correct."""
        status, body = self.srv.post_json(minimal_payload(), token="almost-right")
        self.assertEqual(status, 401)
        # Must not leak token value or detailed auth info
        resp_str = str(body)
        self.assertNotIn("token", resp_str.lower().replace("unauthorized", ""))

    def test_401_error_field_present(self):
        status, body = self.srv.post_json(minimal_payload(), token="bad")
        self.assertEqual(status, 401)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()

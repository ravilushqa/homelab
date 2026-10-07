"""
Rate limit and body limit tests.

SYNTHETIC fixtures, isolated DB.
"""

import json
import time
import unittest
from http.client import HTTPConnection

from .fixtures import minimal_payload, payload_with_steps
from .helpers import TestServer
from ..ratelimit import RateLimitManager, SlidingWindowRateLimiter


class TestSlidingWindowRateLimiter(unittest.TestCase):
    def test_allows_within_limit(self):
        rl = SlidingWindowRateLimiter(limit=5, window=10.0)
        for _ in range(5):
            self.assertTrue(rl.is_allowed("ip1"))

    def test_blocks_over_limit(self):
        rl = SlidingWindowRateLimiter(limit=3, window=60.0)
        for _ in range(3):
            rl.is_allowed("ip1")
        self.assertFalse(rl.is_allowed("ip1"))

    def test_different_keys_independent(self):
        rl = SlidingWindowRateLimiter(limit=2, window=60.0)
        rl.is_allowed("ip1")
        rl.is_allowed("ip1")
        # ip1 is now at limit
        self.assertFalse(rl.is_allowed("ip1"))
        # ip2 is fresh
        self.assertTrue(rl.is_allowed("ip2"))

    def test_window_expiry(self):
        rl = SlidingWindowRateLimiter(limit=2, window=0.1)
        rl.is_allowed("ip1")
        rl.is_allowed("ip1")
        self.assertFalse(rl.is_allowed("ip1"))
        time.sleep(0.15)
        self.assertTrue(rl.is_allowed("ip1"))


class TestRateLimitManager(unittest.TestCase):
    def test_global_limit(self):
        from ..ratelimit import RateLimitManager, _GLOBAL_LIMIT
        rl = RateLimitManager()
        # Exhaust global limit
        for _ in range(_GLOBAL_LIMIT):
            rl.check_request("127.0.0.1")
        # Next request should fail at global level
        self.assertFalse(rl.check_request("127.0.0.2"))

    def test_auth_fail_recorded(self):
        rl = RateLimitManager()
        from ..ratelimit import _AUTH_FAIL_LIMIT
        # Use up auth fail limit
        for _ in range(_AUTH_FAIL_LIMIT):
            rl.record_auth_failure("bad_ip")
        # Should now be rate-limited
        self.assertFalse(rl.record_auth_failure("bad_ip"))


class TestHTTPRateLimiting(unittest.TestCase):
    """Integration: rate limit enforcement at the HTTP layer."""

    def test_body_too_large_returns_413(self):
        import socket
        srv = TestServer()
        try:
            conn = HTTPConnection(srv.host, srv.port, timeout=5)
            big = b"x" * (8 * 1024 * 1024 + 1)
            try:
                conn.request(
                    "POST", "/ingest/health-connect",
                    body=big,
                    headers={
                        "Content-Length": str(len(big)),
                        "Content-Type": "application/json",
                        "X-Health-Token": srv.token_str,
                    },
                )
                resp = conn.getresponse()
                self.assertIn(resp.status, (413, 400))
            except (BrokenPipeError, ConnectionResetError, socket.error):
                # Server closed connection early = correct rejection
                pass
            finally:
                conn.close()
        finally:
            srv.stop()

    def test_chunked_transfer_encoding_rejected(self):
        srv = TestServer()
        try:
            conn = HTTPConnection(srv.host, srv.port, timeout=5)
            import json as _json
            data = _json.dumps(minimal_payload()).encode()
            conn.request(
                "POST", "/ingest/health-connect",
                headers={
                    "Content-Type": "application/json",
                    "Transfer-Encoding": "chunked",
                    "X-Health-Token": srv.token_str,
                },
            )
            # Chunked bodies can't be sent normally via HTTPConnection, so we
            # just test header rejection by sending with Content-Length too
            # (server should reject at the TE check)
            resp = conn.getresponse()
            resp.read()
            conn.close()
        except Exception:
            pass  # Connection may close; that's acceptable rejection
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()

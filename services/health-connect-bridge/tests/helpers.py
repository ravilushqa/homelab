"""
Test helpers: in-process HTTP server and DB utilities.

SYNTHETIC — all test infrastructure; never touches production systems.
"""

import json
import os
import stat
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path

from ..receiver import _BridgeHandler, _BoundedTCPServer, RateLimitManager, _load_token
from ..store import HealthStore
from ..ratelimit import RateLimitManager

_SYNTHETIC_TOKEN = "synthetic-test-token-NEVER-USE-IN-PROD-abc123def456"


def make_temp_db() -> Path:
    """Create an isolated temporary SQLite DB path for tests."""
    fd, path = tempfile.mkstemp(suffix=".sqlite3", prefix="test_health_")
    os.close(fd)
    os.unlink(path)  # Let the store create it fresh with correct permissions
    return Path(path)


def make_temp_token(value: str = _SYNTHETIC_TOKEN) -> tuple[str, Path]:
    """Create a temporary token file with correct permissions. Returns (value, path)."""
    fd, path = tempfile.mkstemp(suffix=".token", prefix="test_token_")
    with os.fdopen(fd, "w") as f:
        f.write(value)
    os.chmod(path, 0o600)
    return value, Path(path)


class TestServer:
    """Spin up an in-process BridgeServer on a random localhost port for tests."""

    def __init__(self, token: str = None, db_path: Path = None) -> None:
        self.db_path = db_path or make_temp_db()
        self.token_str = token or _SYNTHETIC_TOKEN
        _, self.token_path = make_temp_token(self.token_str)

        self._store = HealthStore(self.db_path)
        _BridgeHandler._token = self.token_str.encode()
        _BridgeHandler._store = self._store
        _BridgeHandler._rate_limiter = RateLimitManager()

        self._httpd = _BoundedTCPServer(("127.0.0.1", 0), _BridgeHandler, max_workers=8)
        self.host = "127.0.0.1"
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._store.close()
        for p in (self.db_path, self.token_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def request(
        self,
        method: str = "POST",
        path: str = "/ingest/health-connect",
        body: dict = None,
        token: str = None,
        extra_headers: dict = None,
        raw_body: bytes = None,
    ) -> tuple[int, dict]:
        conn = HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token is None:
            headers["X-Health-Token"] = self.token_str
        elif token != "__omit__":
            headers["X-Health-Token"] = token

        if extra_headers:
            headers.update(extra_headers)

        if raw_body is not None:
            data = raw_body
        elif body is not None:
            data = json.dumps(body).encode()
        else:
            data = b""

        headers["Content-Length"] = str(len(data))
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        try:
            resp_body = json.loads(resp.read())
        except Exception:
            resp_body = {}
        conn.close()
        return status, resp_body

    def post_json(self, payload: dict, token: str = None) -> tuple[int, dict]:
        return self.request("POST", "/ingest/health-connect", body=payload, token=token)

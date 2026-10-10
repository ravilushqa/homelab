"""
Test helpers: in-process HTTP server and DB utilities.

SYNTHETIC — all test infrastructure; never touches production systems.

Each TestServer / NutritionTestServer creates an ISOLATED handler class via
_make_bridge_handler_class so class-level state does not leak between server
instances running in the same process.
"""

import json
import os
import stat
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path

from ..receiver import (
    _BridgeHandler,
    _BoundedTCPServer,
    RateLimitManager,
    _load_token,
    _make_bridge_handler_class,
)
from ..store import HealthStore
from ..nutrition_queue import NutritionStore

_SYNTHETIC_TOKEN = "synthetic-test-token-NEVER-USE-IN-PROD-abc123def456"
_SYNTHETIC_NUTRITION_TOKEN = "synthetic-nutrition-token-NEVER-USE-IN-PROD-xyz789"


def make_temp_db() -> Path:
    """Create an isolated temporary SQLite DB path for tests."""
    fd, path = tempfile.mkstemp(suffix=".sqlite3", prefix="test_health_")
    os.close(fd)
    os.unlink(path)
    return Path(path)


def make_temp_token(value: str = _SYNTHETIC_TOKEN) -> tuple:
    """Create a temporary token file (0600). Returns (value, path)."""
    fd, path = tempfile.mkstemp(suffix=".token", prefix="test_token_")
    with os.fdopen(fd, "w") as f:
        f.write(value)
    os.chmod(path, 0o600)
    return value, Path(path)


class TestServer:
    """
    In-process BridgeServer on a random localhost port.
    Uses _make_bridge_handler_class for per-instance isolation.
    """

    def __init__(self, token: str = None, db_path: Path = None) -> None:
        self.db_path = db_path or make_temp_db()
        self.token_str = token or _SYNTHETIC_TOKEN
        _, self.token_path = make_temp_token(self.token_str)

        self._store = HealthStore(self.db_path)
        HandlerClass = _make_bridge_handler_class(
            self.token_str.encode(),
            self._store,
            RateLimitManager(),
        )

        self._httpd = _BoundedTCPServer(("127.0.0.1", 0), HandlerClass, max_workers=8)
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
    ) -> tuple:
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

    def post_json(self, payload: dict, token: str = None) -> tuple:
        return self.request("POST", "/ingest/health-connect", body=payload, token=token)


class NutritionTestServer:
    """
    In-process server with both ingest and nutrition write-back enabled.
    Uses distinct synthetic tokens and distinct DB paths.
    Uses _make_bridge_handler_class for per-instance isolation.
    SYNTHETIC — never touches production systems.
    """

    def __init__(
        self,
        ingest_token: str = None,
        nutrition_token: str = None,
        db_path: Path = None,
        nutrition_db_path: Path = None,
    ) -> None:
        self.db_path = db_path or make_temp_db()
        self.nutrition_db_path = nutrition_db_path or make_temp_db()
        self.ingest_token_str = ingest_token or _SYNTHETIC_TOKEN
        self.nutrition_token_str = nutrition_token or _SYNTHETIC_NUTRITION_TOKEN

        _, self.ingest_token_path = make_temp_token(self.ingest_token_str)
        _, self.nutrition_token_path = make_temp_token(self.nutrition_token_str)

        self._store = HealthStore(self.db_path)
        self._nutrition_store = NutritionStore(self.nutrition_db_path)

        HandlerClass = _make_bridge_handler_class(
            self.ingest_token_str.encode(),
            self._store,
            RateLimitManager(),
            self.nutrition_token_str.encode(),
            self._nutrition_store,
            RateLimitManager(),
        )

        self._httpd = _BoundedTCPServer(("127.0.0.1", 0), HandlerClass, max_workers=8)
        self.host = "127.0.0.1"
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._store.close()
        self._nutrition_store.close()
        for p in (self.db_path, self.nutrition_db_path,
                  self.ingest_token_path, self.nutrition_token_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def nutrition_get(self, token: str = None) -> tuple:
        """GET /nutrition/queue with X-Nutrition-Token."""
        nt = token if token is not None else self.nutrition_token_str
        conn = HTTPConnection(self.host, self.port, timeout=10)
        headers = {"X-Nutrition-Token": nt}
        conn.request("GET", "/nutrition/queue", headers=headers)
        resp = conn.getresponse()
        status = resp.status
        try:
            body = json.loads(resp.read())
        except Exception:
            body = {}
        conn.close()
        return status, body

    def nutrition_ack(self, client_record_id: str, version: int, token: str = None) -> tuple:
        """POST /nutrition/ack."""
        nt = token if token is not None else self.nutrition_token_str
        payload = {"client_record_id": client_record_id, "client_record_version": version}
        data = json.dumps(payload).encode()
        conn = HTTPConnection(self.host, self.port, timeout=10)
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Nutrition-Token": nt,
        }
        conn.request("POST", "/nutrition/ack", body=data, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        try:
            body = json.loads(resp.read())
        except Exception:
            body = {}
        conn.close()
        return status, body

    def ingest(self, payload: dict, token: str = None) -> tuple:
        """POST /ingest/health-connect."""
        it = token if token is not None else self.ingest_token_str
        data = json.dumps(payload).encode()
        conn = HTTPConnection(self.host, self.port, timeout=10)
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Health-Token": it,
        }
        conn.request("POST", "/ingest/health-connect", body=data, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        try:
            body = json.loads(resp.read())
        except Exception:
            body = {}
        conn.close()
        return status, body

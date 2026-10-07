"""
Health Connect webhook receiver.

Security properties:
- Binds to 192.168.1.65:9121 (configurable; never 0.0.0.0 by default).
- Accepts POST /ingest/health-connect ONLY (exact path, no trailing slash,
  no query string).
- GET /healthz returns {"status":"ok"} — local health check, no auth.
- All other methods/paths → 404 or 405.
- Token from file: must be regular file, mode 0600, owner == process uid,
  content >= 32 chars.  Fails closed (RuntimeError) on any violation.
- Auth via X-Health-Token header; constant-time comparison.
- Duplicate Content-Length or duplicate X-Health-Token headers → 400.
- ANY Transfer-Encoding header → 400.
- Content-Length must be digit-only (no sign, no spaces).
- Body read must exactly match Content-Length bytes.
- JSON must not contain duplicate keys or non-finite floats.
- Schema validation (all-or-nothing) before writes.
- Rate limit: global cap → auth-failure lockout (peek) → per-IP normal.
  Auth-failure window checked BEFORE auth to block brute force.
- Concurrency: semaphore acquired in process_request BEFORE spawning thread;
  thread cannot start if semaphore is exhausted.
- Socket timeout set before handler runs (slow-header protection).
- Connection: close on every response.
- handle_error: no stack traces or client info logged.
- NO health data, tokens, headers, or path contents in any log output.
"""

import hmac
import json
import logging
import math
import os
import signal
import socket
import socketserver
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

from .ratelimit import RateLimitManager
from .store import HealthStore
from .validation import validate_payload

logger = logging.getLogger(__name__)

MAX_BODY = 8 * 1024 * 1024  # 8 MiB
MAX_CONCURRENCY = 8
SOCKET_TIMEOUT = 30.0        # seconds for header + body read
REQUEST_BACKLOG = 32

INGEST_PATH = "/ingest/health-connect"
HEALTHZ_PATH = "/healthz"


# ── Token loading ─────────────────────────────────────────────────────────────

def _load_token(token_path: Path) -> bytes:
    """
    Load the ingest token from a file.  Fails closed (raises RuntimeError)
    on ANY of:
      - path is a symlink
      - file does not exist
      - file is not a regular file
      - file not owned by the current process uid
      - file permissions are not exactly 0600
      - file content is empty or shorter than 32 characters
    """
    if token_path.is_symlink():
        raise RuntimeError(f"Token path must not be a symlink: {token_path}")
    if not token_path.exists():
        raise RuntimeError(f"Token file not found: {token_path}")
    if not token_path.is_file():
        raise RuntimeError(f"Token path is not a regular file: {token_path}")
    st = token_path.stat()
    if st.st_uid != os.getuid():
        raise RuntimeError(
            f"Token file not owned by process uid {os.getuid()}: {token_path}"
        )
    mode = st.st_mode & 0o777
    if mode != 0o600:
        raise RuntimeError(
            f"Token file permissions must be 0600, got {oct(mode)}: {token_path}"
        )
    raw = token_path.read_text().strip()
    if len(raw) < 32:
        raise RuntimeError(
            f"Token too short (minimum 32 chars, got {len(raw)}): {token_path}"
        )
    return raw.encode()


# ── JSON parsing helpers ──────────────────────────────────────────────────────

def _no_dup_keys(pairs: list) -> dict:
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"Duplicate JSON key")
        d[k] = v
    return d


def _check_no_nan_inf(obj) -> None:
    """Recursively reject non-finite floats anywhere in the parsed payload."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("Non-finite float in JSON payload")
    elif isinstance(obj, dict):
        for v in obj.values():
            _check_no_nan_inf(v)
    elif isinstance(obj, list):
        for item in obj:
            _check_no_nan_inf(item)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _BridgeHandler(BaseHTTPRequestHandler):
    # Class-level shared state (set by BridgeServer before serving)
    _token: bytes = b""
    _store: Optional[HealthStore] = None
    _rate_limiter: Optional[RateLimitManager] = None

    server_version = "health-connect-bridge/1.0"
    sys_version = ""  # suppress Python version from Server header

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # suppress default combined-log (never log client data)

    def log_request(self, code: int = "-", size: int = "-") -> None:
        pass

    def _peer_ip(self) -> str:
        return self.client_address[0]

    def _send_json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _reject(self, code: int, msg: str) -> None:
        self._send_json(code, {"error": msg})

    def do_GET(self) -> None:
        if self.path == HEALTHZ_PATH:
            self._send_json(200, {"status": "ok"})
            return
        self._reject(404, "not found")

    def do_POST(self) -> None:
        # Concurrency is already gated at process_request level.
        self._handle_post(self._peer_ip())

    def _handle_post(self, ip: str) -> None:
        rl = self.__class__._rate_limiter

        # Global + auth-failure-lockout + per-IP admission check
        if rl is not None and not rl.check_request(ip):
            self._reject(429, "rate limit exceeded")
            return

        # Exact path match (no trailing slash, no query string)
        if self.path != INGEST_PATH:
            self._reject(404, "not found")
            return

        # Reject ANY Transfer-Encoding header (including chunked)
        te_vals = self.headers.get_all("Transfer-Encoding") or []
        if te_vals:
            self._reject(400, "transfer-encoding not accepted")
            return

        # Exactly one Content-Length required (duplicate = potential smuggling)
        cl_vals = self.headers.get_all("Content-Length") or []
        if len(cl_vals) != 1:
            self._reject(411, "content-length required (exactly one)")
            return
        cl_str = cl_vals[0].strip()
        # Digit-only (no sign, no spaces, no leading zeros that hide length)
        if not cl_str or not all(c in "0123456789" for c in cl_str):
            self._reject(400, "invalid content-length")
            return
        content_length = int(cl_str)
        if content_length > MAX_BODY:
            self._reject(413, "request entity too large")
            return

        # Require application/json content-type
        ct = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ct != "application/json":
            self._reject(415, "content-type must be application/json")
            return

        # Exactly one X-Health-Token header
        token_vals = self.headers.get_all("X-Health-Token") or []
        if len(token_vals) > 1:
            self._reject(400, "duplicate auth header")
            return
        supplied = (token_vals[0] if token_vals else "").encode()
        expected = self.__class__._token

        # Constant-time auth comparison
        auth_ok = hmac.compare_digest(supplied, expected)
        if not auth_ok:
            if rl is not None:
                rl.record_auth_failure(ip)
            self._reject(401, "unauthorized")
            return

        # Read body — must match Content-Length exactly
        try:
            raw = self.rfile.read(content_length)
        except (OSError, socket.timeout):
            self._reject(400, "failed to read body")
            return
        if len(raw) != content_length:
            self._reject(400, "body length mismatch")
            return

        # Parse JSON — reject duplicate keys and non-finite floats
        try:
            payload = json.loads(raw, object_pairs_hook=_no_dup_keys)
        except (json.JSONDecodeError, ValueError):
            self._reject(400, "invalid json")
            return

        if not isinstance(payload, dict):
            self._reject(400, "payload must be a json object")
            return

        try:
            _check_no_nan_inf(payload)
        except ValueError:
            self._reject(400, "non-finite float in payload")
            return

        # Schema validation — all-or-nothing
        errors = validate_payload(payload)
        if errors:
            self._reject(400, "validation failed")
            return

        # Ingest
        received_at = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        store = self.__class__._store
        if store is None:
            self._reject(503, "service unavailable")
            return
        try:
            summary = store.ingest_payload(payload, received_at)
        except Exception:
            logger.error("Ingest error (payload details suppressed)")
            self._reject(500, "internal error")
            return

        logger.info("Ingest ok record_types=%d", len(summary))
        self._send_json(200, {"ok": True, "ingested": summary})

    def do_HEAD(self) -> None:
        self._reject(405, "method not allowed")

    def do_PUT(self) -> None:
        self._reject(405, "method not allowed")

    def do_DELETE(self) -> None:
        self._reject(405, "method not allowed")

    def do_PATCH(self) -> None:
        self._reject(405, "method not allowed")


# ── Bounded-concurrency TCP server ────────────────────────────────────────────

class _BoundedTCPServer(socketserver.TCPServer):
    """
    TCPServer that enforces a concurrency semaphore BEFORE spawning a thread.
    The semaphore is acquired in process_request (called in the accept loop)
    so that a full worker pool causes immediate connection rejection, not
    silent queueing or unlimited thread growth.

    A socket timeout is set on each accepted connection before the handler
    runs, providing protection against slow-header and slow-body attacks.
    """

    allow_reuse_address = True
    request_queue_size = REQUEST_BACKLOG

    def __init__(self, server_address, RequestHandlerClass, max_workers: int) -> None:
        self._worker_sem = threading.Semaphore(max_workers)
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request, client_address) -> None:
        if not self._worker_sem.acquire(blocking=False):
            # No workers available: drop the connection immediately
            try:
                request.close()
            except Exception:
                pass
            return
        # Set socket timeout before the handler starts reading headers/body
        try:
            request.settimeout(SOCKET_TIMEOUT)
        except Exception:
            pass
        t = threading.Thread(
            target=self._worker_thread,
            args=(request, client_address),
            daemon=True,
        )
        t.start()

    def _worker_thread(self, request, client_address) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self._worker_sem.release()

    def handle_error(self, request, client_address) -> None:
        # Do not log client_address or any request details (may contain health data)
        logger.debug("Connection error (details suppressed)")


# ── Server lifecycle ──────────────────────────────────────────────────────────

class BridgeServer:
    def __init__(self, host: str, port: int, token_path: Path, db_path: Path) -> None:
        self._host = host
        self._port = port
        self._token_path = token_path
        self._db_path = db_path
        self._server: Optional[_BoundedTCPServer] = None
        self._store: Optional[HealthStore] = None

    def start(self) -> None:
        token = _load_token(self._token_path)
        store = HealthStore(self._db_path)
        rl = RateLimitManager()

        _BridgeHandler._token = token
        _BridgeHandler._store = store
        _BridgeHandler._rate_limiter = rl

        self._store = store
        self._server = _BoundedTCPServer(
            (self._host, self._port), _BridgeHandler, MAX_CONCURRENCY
        )
        logger.info("Listening on %s:%d", self._host, self._port)

        def _stop(sig, frame):
            logger.info("Shutdown signal received")
            threading.Thread(target=self._server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self._server.serve_forever()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
        if self._store:
            self._store.close()


def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    _DEFAULT_BASE = (
        Path.home() / ".hermes/profiles/health/workspace/health-connect-bridge"
    )
    parser = argparse.ArgumentParser(description="Health Connect webhook receiver")
    parser.add_argument("--host", default="192.168.1.65")
    parser.add_argument("--port", type=int, default=9121)
    parser.add_argument(
        "--token-path",
        type=Path,
        default=_DEFAULT_BASE / "secrets/ingest-token",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=_DEFAULT_BASE / "data/health.sqlite3",
    )
    args = parser.parse_args()
    BridgeServer(
        host=args.host,
        port=args.port,
        token_path=args.token_path,
        db_path=args.db_path,
    ).start()


if __name__ == "__main__":
    main()

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

Nutrition write-back (optional — only active when nutrition_token_path and
nutrition_db_path are configured):
- GET  /nutrition/queue  — X-Nutrition-Token auth; returns confirmed unacked records.
- POST /nutrition/ack    — X-Nutrition-Token auth; acks a delivered record.
- Scoped auth: X-Health-Token cannot access nutrition routes;
  X-Nutrition-Token cannot access ingest route.
- Nutrition token and ingest token must not share bytes (rejected at startup).
- Nutrition DB and health DB must not alias each other (symlink/hardlink checked).
- Cache-Control: no-store on all nutrition responses.
- Ack body capped at MAX_ACK_BODY; GET queue body has no body (reject if present).
- Reject Transfer-Encoding and query strings on nutrition routes.
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
NUTRITION_QUEUE_PATH = "/nutrition/queue"
NUTRITION_ACK_PATH = "/nutrition/ack"

# Nutrition-specific limits
_MAX_ACK_BODY = 1024   # bytes — ack payload is tiny
_NUTRITION_GET_RATE_LIMIT = 10   # per IP per 60 s
_NUTRITION_GLOBAL_RATE_LIMIT = 30  # global per 60 s


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

def _make_bridge_handler_class(
    token: bytes,
    store: "HealthStore",
    rate_limiter: "RateLimitManager",
    nutrition_token: bytes = b"",
    nutrition_store=None,
    nutrition_rl=None,
) -> type:
    """
    Create an isolated handler class per server instance.

    Each BridgeServer creates its own subclass so that class-level state
    (auth tokens, stores) never leaks between concurrent server instances
    in the same process.
    """
    return type(
        "_BridgeHandlerInstance",
        (_BridgeHandler,),
        {
            "_token": token,
            "_store": store,
            "_rate_limiter": rate_limiter,
            "_nutrition_token": nutrition_token,
            "_nutrition_store": nutrition_store,
            "_nutrition_rl": nutrition_rl,
        },
    )


class _BridgeHandler(BaseHTTPRequestHandler):
    # Class-level state defaults.  Production use goes via _make_bridge_handler_class.
    # Test helpers may set these directly on the class for isolated test servers.
    _token: bytes = b""
    _store: Optional[HealthStore] = None
    _rate_limiter: Optional[RateLimitManager] = None

    # Nutrition write-back state (only active when not None/empty)
    _nutrition_token: bytes = b""
    _nutrition_store = None
    _nutrition_rl: Optional[RateLimitManager] = None

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

    def _send_json_no_cache(self, code: int, body: dict) -> None:
        """Like _send_json but adds Cache-Control: no-store (for nutrition endpoints)."""
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _reject_no_cache(self, code: int, msg: str) -> None:
        self._send_json_no_cache(code, {"error": msg})

    def do_GET(self) -> None:
        if self.path == HEALTHZ_PATH:
            self._send_json(200, {"status": "ok"})
            return
        if self.path == NUTRITION_QUEUE_PATH:
            self._handle_nutrition_queue(self._peer_ip())
            return
        self._reject(404, "not found")

    def do_POST(self) -> None:
        # Concurrency is already gated at process_request level.
        ip = self._peer_ip()
        if self.path == NUTRITION_ACK_PATH:
            self._handle_nutrition_ack(ip)
            return
        self._handle_post(ip)

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

        # Parse JSON — reject duplicate keys, non-finite floats, deep recursion
        try:
            payload = json.loads(raw, object_pairs_hook=_no_dup_keys)
        except (json.JSONDecodeError, ValueError):
            self._reject(400, "invalid json")
            return
        except RecursionError:
            self._reject(400, "json too deeply nested")
            return
        except OverflowError:
            self._reject(400, "json value out of range")
            return

        if not isinstance(payload, dict):
            self._reject(400, "payload must be a json object")
            return

        try:
            _check_no_nan_inf(payload)
        except (ValueError, RecursionError):
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

    # ── Nutrition handlers ────────────────────────────────────────────────────

    def _check_nutrition_auth(self) -> Optional[bytes]:
        """
        Validate X-Nutrition-Token for nutrition endpoints.

        Returns the nutrition token bytes if auth passes, None if request
        should be rejected (caller must have already sent the error response).

        Security: rejects query strings, duplicate headers, Transfer-Encoding.
        Scoped: X-Health-Token cannot access nutrition routes (different header).
        """
        # Reject query string on nutrition paths
        if "?" in self.path:
            self._reject_no_cache(404, "not found")
            return None

        # Reject Transfer-Encoding
        te_vals = self.headers.get_all("Transfer-Encoding") or []
        if te_vals:
            self._reject_no_cache(400, "transfer-encoding not accepted")
            return None

        # Scoped auth: ingest token (X-Health-Token) must NOT be present
        health_vals = self.headers.get_all("X-Health-Token") or []
        if health_vals:
            self._reject_no_cache(400, "wrong token header for nutrition endpoint")
            return None

        # Exactly one X-Nutrition-Token
        nt_vals = self.headers.get_all("X-Nutrition-Token") or []
        if len(nt_vals) > 1:
            self._reject_no_cache(400, "duplicate auth header")
            return None
        supplied = (nt_vals[0] if nt_vals else "").encode()
        expected = self.__class__._nutrition_token

        rl = self.__class__._nutrition_rl
        if not hmac.compare_digest(supplied, expected):
            if rl is not None:
                rl.record_auth_failure(self._peer_ip())
            self._reject_no_cache(401, "unauthorized")
            return None
        return expected

    def _handle_nutrition_queue(self, ip: str) -> None:
        """GET /nutrition/queue — return confirmed unacked records."""
        ns = self.__class__._nutrition_store
        if ns is None:
            # Nutrition not configured: fail closed
            self._reject(404, "not found")
            return

        rl = self.__class__._nutrition_rl
        if rl is not None and not rl.check_request(ip):
            self._reject_no_cache(429, "rate limit exceeded")
            return

        if self._check_nutrition_auth() is None:
            return

        # GET must not have a body; reject duplicate Content-Length headers too
        cl_vals = self.headers.get_all("Content-Length") or []
        if len(cl_vals) > 1:
            self._reject_no_cache(400, "duplicate content-length header")
            return
        if cl_vals:
            cl_str = cl_vals[0].strip()
            if cl_str and cl_str != "0":
                self._reject_no_cache(400, "body not accepted on GET")
                return

        try:
            records = ns.get_confirmed_queue()
        except Exception:
            logger.error("Nutrition queue fetch error (details suppressed)")
            self._reject_no_cache(500, "internal error")
            return

        self._send_json_no_cache(200, {"schema_version": 1, "records": records})

    def _handle_nutrition_ack(self, ip: str) -> None:
        """POST /nutrition/ack — acknowledge a delivered record."""
        from .nutrition_queue import (
            NotFound, VersionConflict, InvalidState,
        )
        ns = self.__class__._nutrition_store
        if ns is None:
            self._reject(404, "not found")
            return

        rl = self.__class__._nutrition_rl
        if rl is not None and not rl.check_request(ip):
            self._reject_no_cache(429, "rate limit exceeded")
            return

        if self._check_nutrition_auth() is None:
            return

        # Reject Transfer-Encoding (already checked in _check_nutrition_auth via path,
        # but double-check here since _check_nutrition_auth is path-agnostic)
        te_vals = self.headers.get_all("Transfer-Encoding") or []
        if te_vals:
            self._reject_no_cache(400, "transfer-encoding not accepted")
            return

        # Exactly one Content-Length required; cap at MAX_ACK_BODY
        cl_vals = self.headers.get_all("Content-Length") or []
        if len(cl_vals) != 1:
            self._reject_no_cache(411, "content-length required (exactly one)")
            return
        cl_str = cl_vals[0].strip()
        if not cl_str or not all(c in "0123456789" for c in cl_str):
            self._reject_no_cache(400, "invalid content-length")
            return
        content_length = int(cl_str)
        if content_length > _MAX_ACK_BODY:
            self._reject_no_cache(413, "ack body too large")
            return

        ct = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ct != "application/json":
            self._reject_no_cache(415, "content-type must be application/json")
            return

        try:
            raw = self.rfile.read(content_length)
        except (OSError, socket.timeout):
            self._reject_no_cache(400, "failed to read body")
            return
        if len(raw) != content_length:
            self._reject_no_cache(400, "body length mismatch")
            return

        try:
            payload = json.loads(raw, object_pairs_hook=_no_dup_keys)
        except (json.JSONDecodeError, ValueError):
            self._reject_no_cache(400, "invalid json")
            return
        except (RecursionError, OverflowError):
            self._reject_no_cache(400, "json too complex or value out of range")
            return

        if not isinstance(payload, dict):
            self._reject_no_cache(400, "payload must be a json object")
            return

        try:
            _check_no_nan_inf(payload)
        except (ValueError, RecursionError):
            self._reject_no_cache(400, "non-finite float in payload")
            return

        # Validate ack fields: exactly client_record_id (string) and client_record_version (int)
        expected_keys = {"client_record_id", "client_record_version"}
        if set(payload.keys()) != expected_keys:
            self._reject_no_cache(400, "ack must contain exactly client_record_id and client_record_version")
            return

        crid = payload.get("client_record_id")
        crv = payload.get("client_record_version")

        if not isinstance(crid, str) or not crid:
            self._reject_no_cache(400, "client_record_id must be a non-empty string")
            return
        if isinstance(crv, bool) or not isinstance(crv, int) or crv < 1:
            self._reject_no_cache(400, "client_record_version must be a positive integer")
            return
        # Guard against huge ints that signal malice
        if crv > 2 ** 53:
            self._reject_no_cache(400, "client_record_version out of range")
            return

        try:
            result = ns.ack_record(crid, crv)
        except NotFound:
            self._reject_no_cache(404, "not found")
            return
        except VersionConflict:
            self._reject_no_cache(409, "version conflict")
            return
        except InvalidState:
            self._reject_no_cache(409, "record not in confirmable state")
            return
        except Exception:
            logger.error("Nutrition ack error (details suppressed)")
            self._reject_no_cache(500, "internal error")
            return

        self._send_json_no_cache(200, {"ok": True, "result": result})

    # ── Standard method handlers ──────────────────────────────────────────────

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

def _check_no_path_alias(path_a: Path, path_b: Path) -> None:
    """
    Raise RuntimeError if path_a and path_b refer to the same inode
    (hardlink or same file) or if either is a symlink.
    Only checks existing paths; called after both exist.
    """
    for p in (path_a, path_b):
        if p.is_symlink():
            raise RuntimeError(f"DB path must not be a symlink: {p}")
    try:
        st_a = path_a.stat()
        st_b = path_b.stat()
        if (st_a.st_dev == st_b.st_dev) and (st_a.st_ino == st_b.st_ino):
            raise RuntimeError(
                f"Nutrition DB and health DB must not alias each other: "
                f"{path_a} and {path_b}"
            )
    except FileNotFoundError:
        pass  # one or both don't exist yet; alias check deferred


class BridgeServer:
    def __init__(
        self,
        host: str,
        port: int,
        token_path: Path,
        db_path: Path,
        nutrition_token_path: Optional[Path] = None,
        nutrition_db_path: Optional[Path] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._token_path = token_path
        self._db_path = db_path
        self._nutrition_token_path = nutrition_token_path
        self._nutrition_db_path = nutrition_db_path
        self._server: Optional[_BoundedTCPServer] = None
        self._store: Optional[HealthStore] = None
        self._nutrition_store = None

        # Fail closed if only one of the two nutrition args is provided
        has_nt = nutrition_token_path is not None
        has_nd = nutrition_db_path is not None
        if has_nt != has_nd:
            raise RuntimeError(
                "Both --nutrition-token-path and --nutrition-db-path must be "
                "provided together, or neither (partial nutrition config rejected)"
            )

    def start(self) -> None:
        # Alias check using resolved paths FIRST — before any token loading or
        # file creation.  Path.resolve() normalises ./x, ../x, and symlinks even
        # for absent files (strict=False default), so identical absent paths are
        # caught before HealthStore creates any DB file.
        if self._nutrition_db_path is not None:
            try:
                resolved_db = Path(self._db_path).resolve()
                resolved_ndb = Path(self._nutrition_db_path).resolve()
                if resolved_db == resolved_ndb:
                    raise RuntimeError(
                        "Nutrition DB and health DB resolve to the same path — "
                        "they must not alias each other"
                    )
            except RuntimeError:
                raise
            except (OSError, ValueError) as e:
                raise RuntimeError(
                    f"Cannot resolve DB paths for alias check: {e}"
                ) from e

        # Load and validate all tokens and path aliases BEFORE opening any database.
        # This ensures no DB is created or mutated if configuration is invalid.
        token = _load_token(self._token_path)

        ntoken: bytes = b""
        nstore = None
        nrl = None

        # Nutrition validation (optional — only if both paths provided)
        if self._nutrition_token_path is not None and self._nutrition_db_path is not None:
            from .nutrition_queue import NutritionStore
            # Inode/symlink alias check (complements the resolved-path check above)
            _check_no_path_alias(self._db_path, self._nutrition_db_path)
            ntoken = _load_token(self._nutrition_token_path)
            # Reject identical token bytes
            if hmac.compare_digest(token, ntoken):
                raise RuntimeError(
                    "Nutrition token and ingest token must not share bytes"
                )

        # All pre-flight checks passed; now open databases
        store = HealthStore(self._db_path)
        rl = RateLimitManager()

        if self._nutrition_token_path is not None and self._nutrition_db_path is not None:
            from .nutrition_queue import NutritionStore
            nstore = NutritionStore(self._nutrition_db_path)
            nrl = RateLimitManager()
            self._nutrition_store = nstore
            logger.info("Nutrition write-back enabled")

        # Per-server isolated handler class — no class-level state shared between instances
        HandlerClass = _make_bridge_handler_class(
            token, store, rl, ntoken, nstore, nrl
        )

        self._store = store
        self._server = _BoundedTCPServer(
            (self._host, self._port), HandlerClass, MAX_CONCURRENCY
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
        if self._nutrition_store:
            self._nutrition_store.close()


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
    parser.add_argument(
        "--nutrition-token-path",
        type=Path,
        default=None,
        help="Path to nutrition pairing token (enables nutrition write-back when set)",
    )
    parser.add_argument(
        "--nutrition-db-path",
        type=Path,
        default=None,
        help="Path to nutrition SQLite database (required when --nutrition-token-path is set)",
    )
    args = parser.parse_args()
    BridgeServer(
        host=args.host,
        port=args.port,
        token_path=args.token_path,
        db_path=args.db_path,
        nutrition_token_path=args.nutrition_token_path,
        nutrition_db_path=args.nutrition_db_path,
    ).start()


if __name__ == "__main__":
    main()

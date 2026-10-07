"""
Rate limiter for the HTTP receiver.

IP is always the TCP peer address — X-Forwarded-For is never read.

Rate limits:
  normal:    10 requests / 60 s per IP    (checked on every admission)
  auth_fail: 5 failures / 300 s per IP    (checked on admission to block brute force)
  global:    200 requests / 60 s total    (checked first; applies even to failed auth)

Auth-failure enforcement on admission:
  check_request() peeks at the auth-failure window for the requesting IP
  WITHOUT recording an event.  If the IP has already hit the auth-failure limit,
  the request is rejected with 429 before auth is attempted.  This prevents
  brute-force attempts from consuming server resources.

  record_auth_failure() is called AFTER an auth failure is detected to add
  the event to the failure window.

Bounded storage:
  cleanup_old_keys() must be called periodically to prevent unbounded memory
  growth from IPs that send one request and disappear.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Deque

_NORMAL_LIMIT = 10
_NORMAL_WINDOW = 60.0

_AUTH_FAIL_LIMIT = 5
_AUTH_FAIL_WINDOW = 300.0

_GLOBAL_LIMIT = 200
_GLOBAL_WINDOW = 60.0


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window rate limiter."""

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._lock = threading.Lock()
        self._windows: dict[str, Deque[float]] = defaultdict(deque)

    def _evict(self, dq: Deque[float], cutoff: float) -> None:
        while dq and dq[0] < cutoff:
            dq.popleft()

    def is_allowed(self, key: str) -> bool:
        """Check and record an event.  Returns True if allowed."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            dq = self._windows[key]
            self._evict(dq, cutoff)
            if len(dq) >= self._limit:
                return False
            dq.append(now)
            return True

    def is_blocked(self, key: str) -> bool:
        """
        Peek: returns True if the key is currently over limit.
        Does NOT record an event.
        """
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            dq = self._windows.get(key)
            if dq is None:
                return False
            self._evict(dq, cutoff)
            return len(dq) >= self._limit

    def record_only(self, key: str) -> None:
        """Record an event unconditionally (no limit check)."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            dq = self._windows[key]
            self._evict(dq, cutoff)
            dq.append(now)

    def cleanup_old_keys(self) -> None:
        """Remove keys with no events within the window.  Call periodically."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            stale = [
                k for k, dq in self._windows.items()
                if not dq or dq[-1] < cutoff
            ]
            for k in stale:
                del self._windows[k]

    @property
    def active_key_count(self) -> int:
        with self._lock:
            return len(self._windows)


class RateLimitManager:
    """Composed rate limit manager used by the HTTP handler."""

    def __init__(self) -> None:
        self._normal = SlidingWindowRateLimiter(_NORMAL_LIMIT, _NORMAL_WINDOW)
        self._auth_fail = SlidingWindowRateLimiter(_AUTH_FAIL_LIMIT, _AUTH_FAIL_WINDOW)
        self._global = SlidingWindowRateLimiter(_GLOBAL_LIMIT, _GLOBAL_WINDOW)
        self._global_key = "__global__"

    def check_request(self, ip: str) -> bool:
        """
        Admission check.  Returns True if the request should proceed.

        Checks in order:
          1. Global rate limit (counts the event)
          2. Auth-failure lockout (peek — does not record)
          3. Per-IP normal rate limit (counts the event)
        """
        if not self._global.is_allowed(self._global_key):
            return False
        # Reject IPs that have hit the auth-failure limit without recording
        if self._auth_fail.is_blocked(ip):
            return False
        return self._normal.is_allowed(ip)

    def record_auth_failure(self, ip: str) -> None:
        """Record an authentication failure for this IP."""
        self._auth_fail.record_only(ip)

    def cleanup(self) -> None:
        """Release memory for inactive IPs.  Call from a maintenance thread."""
        self._normal.cleanup_old_keys()
        self._auth_fail.cleanup_old_keys()
        self._global.cleanup_old_keys()

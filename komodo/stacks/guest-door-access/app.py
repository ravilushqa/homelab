import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LOG = logging.getLogger("guest-door-access")
MAX_BODY_BYTES = 8192


@dataclass(frozen=True)
class Config:
    ha_url: str
    ha_token: str
    access_token: str
    pin: str
    access_start: datetime | None
    access_end: datetime | None
    rate_limit_seconds: int
    building_entity: str
    building_domain: str
    building_service: str
    apartment_entity: str
    apartment_domain: str
    apartment_service: str


class RateLimiter:
    def __init__(self, interval_seconds: int):
        self.interval_seconds = max(0, interval_seconds)
        self.last_attempts: dict[tuple[str, str], float] = {}
        self.lock = threading.Lock()

    def allow(self, client_ip: str, action: str, now: float | None = None) -> bool:
        if self.interval_seconds == 0:
            return True
        current = time.time() if now is None else now
        key = (client_ip, action)
        with self.lock:
            previous = self.last_attempts.get(key)
            if previous is not None and current - previous < self.interval_seconds:
                return False
            self.last_attempts[key] = current
            return True


def parse_iso_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_config() -> Config:
    return Config(
        ha_url=os.environ.get("HA_URL", "").rstrip("/"),
        ha_token=os.environ.get("HA_TOKEN", ""),
        access_token=os.environ.get("ACCESS_TOKEN", ""),
        pin=os.environ.get("PIN", ""),
        access_start=parse_iso_datetime(os.environ.get("ACCESS_START", "")),
        access_end=parse_iso_datetime(os.environ.get("ACCESS_END", "")),
        rate_limit_seconds=int(os.environ.get("RATE_LIMIT_SECONDS", "10")),
        building_entity=os.environ.get("BUILDING_ENTITY", "button.comelit_default_open_door"),
        building_domain=os.environ.get("BUILDING_DOMAIN", "button"),
        building_service=os.environ.get("BUILDING_SERVICE", "press"),
        apartment_entity=os.environ.get("APARTMENT_ENTITY", "button.home_door_unlatch"),
        apartment_domain=os.environ.get("APARTMENT_DOMAIN", "button"),
        apartment_service=os.environ.get("APARTMENT_SERVICE", "press"),
    )


def access_window_open(config: Config, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if config.access_start and current < config.access_start:
        return False
    if config.access_end and current > config.access_end:
        return False
    return True


def html_page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Guest Door Access</title>
  <style>
    :root {
      color-scheme: light;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f6f7f2;
      color: #17211b;
    }
    * { box-sizing: border-box; }
    body {
      min-height: 100vh;
      margin: 0;
      display: grid;
      place-items: center;
      padding: 24px;
      background:
        linear-gradient(135deg, rgba(44, 104, 81, 0.12), rgba(217, 183, 83, 0.15)),
        #f6f7f2;
    }
    main {
      width: min(100%, 440px);
      padding: 28px;
      border: 1px solid #d9dece;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 18px 45px rgba(23, 33, 27, 0.12);
    }
    h1 {
      margin: 0 0 8px;
      font-size: 1.6rem;
      line-height: 1.15;
    }
    p {
      margin: 0 0 20px;
      color: #526057;
      line-height: 1.45;
    }
    label {
      display: block;
      margin-bottom: 16px;
      color: #334139;
      font-size: 0.95rem;
      font-weight: 650;
    }
    input {
      width: 100%;
      min-height: 48px;
      margin-top: 8px;
      padding: 10px 12px;
      border: 1px solid #b9c2b3;
      border-radius: 6px;
      font: inherit;
      font-size: 1.1rem;
    }
    .actions {
      display: grid;
      gap: 12px;
    }
    button {
      width: 100%;
      min-height: 56px;
      border: 0;
      border-radius: 6px;
      background: #23664f;
      color: #fff;
      font: inherit;
      font-size: 1.05rem;
      font-weight: 750;
      cursor: pointer;
    }
    button.secondary { background: #303d46; }
    button:disabled {
      cursor: wait;
      opacity: 0.72;
    }
    #status {
      min-height: 24px;
      margin-top: 18px;
      color: #334139;
      font-weight: 650;
    }
    #status.error { color: #a23628; }
    #status.ok { color: #23664f; }
  </style>
</head>
<body>
  <main>
    <h1>Guest Door Access</h1>
    <p>Use these controls only when you are at the entrance or apartment door.</p>
    <label>
      PIN
      <input id="pin" type="password" inputmode="numeric" autocomplete="one-time-code">
    </label>
    <div class="actions">
      <button data-action="building">Open building door</button>
      <button class="secondary" data-action="apartment">Open apartment door</button>
    </div>
    <div id="status" role="status" aria-live="polite"></div>
  </main>
  <script>
    function tokenFromHash() {
      const hash = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
      if (!hash) return "";
      if (hash.startsWith("token=") || hash.includes("&token=")) {
        const params = new URLSearchParams(hash);
        return params.get("token") || "";
      }
      return decodeURIComponent(hash);
    }

    const hashToken = tokenFromHash();
    if (hashToken) localStorage.setItem("guestDoorAccessToken", hashToken);
    const token = hashToken || localStorage.getItem("guestDoorAccessToken") || "";
    const status = document.querySelector("#status");
    const pin = document.querySelector("#pin");
    const buttons = [...document.querySelectorAll("button[data-action]")];

    async function openDoor(action) {
      status.className = "";
      if (!token) {
        status.className = "error";
        status.textContent = "Access token missing.";
        return;
      }
      status.textContent = "Sending request...";
      buttons.forEach((button) => button.disabled = true);
      try {
        const response = await fetch(`/api/open/${action}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token, pin: pin.value })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "Request failed");
        status.className = "ok";
        status.textContent = data.message || "Door request sent.";
      } catch (error) {
        status.className = "error";
        status.textContent = error.message;
      } finally {
        buttons.forEach((button) => button.disabled = false);
      }
    }

    buttons.forEach((button) => {
      button.addEventListener("click", () => openDoor(button.dataset.action));
    });
  </script>
</body>
</html>
"""


class GuestDoorHandler(BaseHTTPRequestHandler):
    server_version = "GuestDoorAccess/1.0"

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_text(HTTPStatus.OK, "ok\n")
            return

        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_html(HTTPStatus.OK, html_page())
            return

        self.log_rejection("page", "bad_token")
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        action_map = {
            "/api/open/building": (
                "building",
                self.server.config.building_domain,
                self.server.config.building_service,
                self.server.config.building_entity,
            ),
            "/api/open/apartment": (
                "apartment",
                self.server.config.apartment_domain,
                self.server.config.apartment_service,
                self.server.config.apartment_entity,
            ),
        }
        if path not in action_map:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        action, domain, service, entity = action_map[path]
        client_ip = self.client_ip()
        if not self.server.rate_limiter.allow(client_ip, action):
            self.log_rejection(action, "rate_limited")
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Please wait before trying again."})
            return

        try:
            payload = self.read_payload()
        except ValueError as exc:
            self.close_connection = True
            self.log_rejection(action, str(exc))
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request body."})
            return

        if payload.get("token") != self.server.config.access_token:
            self.log_rejection(action, "bad_token")
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Access token rejected."})
            return
        if payload.get("pin") != self.server.config.pin:
            self.log_rejection(action, "bad_pin")
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "PIN rejected."})
            return
        if not access_window_open(self.server.config):
            self.log_rejection(action, "outside_access_window")
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Access is not active right now."})
            return
        try:
            call_home_assistant(self.server.config, domain, service, entity)
        except Exception as exc:
            LOG.warning("action=%s result=rejected reason=ha_error entity=%s error=%s", action, entity, exc)
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": "Door request could not be sent."})
            return

        LOG.info("action=%s result=success entity=%s", action, entity)
        self.send_json(HTTPStatus.OK, {"message": "Door request sent."})

    def read_payload(self) -> dict[str, str]:
        length = parse_content_length(self.headers.get("Content-Length"), MAX_BODY_BYTES)
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return data if isinstance(data, dict) else {}
        if "application/x-www-form-urlencoded" in content_type:
            try:
                parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                return {}
            return {key: values[-1] for key, values in parsed.items()}
        return {}

    def send_html(self, status: HTTPStatus, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def send_text(self, status: HTTPStatus, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def send_json(self, status: HTTPStatus, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_rejection(self, action: str, reason: str) -> None:
        LOG.info("action=%s result=rejected reason=%s client_ip=%s", action, reason, self.client_ip())

    def client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "unknown"

    def log_message(self, format: str, *args: object) -> None:
        status = args[1] if len(args) > 1 else "-"
        size = args[2] if len(args) > 2 else "-"
        LOG.info("request client_ip=%s method=%s status=%s size=%s", self.client_ip(), self.command, status, size)


def parse_content_length(value: str | None, max_bytes: int = MAX_BODY_BYTES) -> int:
    if value is None or value == "":
        return 0
    try:
        length = int(value)
    except ValueError as exc:
        raise ValueError("bad_content_length") from exc
    if length < 0:
        raise ValueError("bad_content_length")
    if length > max_bytes:
        raise ValueError("body_too_large")
    return length


def call_home_assistant(config: Config, domain: str, service: str, entity: str) -> None:
    if not config.ha_url or not config.ha_token:
        raise RuntimeError("Home Assistant URL or token is not configured")
    url = f"{config.ha_url}/api/services/{domain}/{service}"
    body = json.dumps({"entity_id": entity}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {config.ha_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 400:
                raise RuntimeError(f"Home Assistant returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Home Assistant returned HTTP {exc.code}") from exc


def validate_config(config: Config) -> None:
    missing = []
    required = {
        "HA_URL": config.ha_url,
        "HA_TOKEN": config.ha_token,
        "ACCESS_TOKEN": config.access_token,
        "PIN": config.pin,
    }
    for name, value in required.items():
        if not value:
            missing.append(name)
    if missing:
        raise SystemExit(f"Missing required environment: {', '.join(missing)}")


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    validate_config(config)
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), GuestDoorHandler)
    server.config = config
    server.rate_limiter = RateLimiter(config.rate_limit_seconds)
    LOG.info("guest-door-access listening on port %s", port)
    server.serve_forever()


if __name__ == "__main__":
    main()

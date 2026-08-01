from __future__ import annotations

import http.server
import pathlib
import re
import urllib.error
import urllib.request


STATIC_ROOT = pathlib.Path("/frontend/public")
APP_JS_PATH = STATIC_ROOT / "demo" / "app.js"
INDEX_PATH = STATIC_ROOT / "demo" / "index.html"
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def patched_app_js() -> bytes:
    source = APP_JS_PATH.read_text(encoding="utf-8")
    if "isPreselectedSeriesAvailable" in source:
        return source.encode("utf-8")
    availability_anchor = (
        "  const storedSeries = getStoredDriverPrintSeries();\n"
        "  const isStoredSeriesAvailable = availableSeriesOptions.some("
    )
    availability_replacement = (
        "  const storedSeries = getStoredDriverPrintSeries();\n"
        "  const isPreselectedSeriesAvailable = availableSeriesOptions.some(\n"
        "    (item) => normalizeBlankSeries(item?.series).toLowerCase() === "
        "preselectedSeries.toLowerCase(),\n"
        "  );\n"
        "  const isStoredSeriesAvailable = availableSeriesOptions.some("
    )
    selection_anchor = (
        "      preselectedSeries ||\n"
        "      (isStoredSeriesAvailable ? storedSeries : \"\") ||"
    )
    selection_replacement = (
        "      (preselectedSeries && (isPreselectedSeriesAvailable || "
        "!availableSeriesOptions.length) ? preselectedSeries : \"\") ||\n"
        "      (isStoredSeriesAvailable ? storedSeries : \"\") ||"
    )
    if source.count(availability_anchor) != 1 or source.count(selection_anchor) != 1:
        raise RuntimeError("Cached app.js does not match the expected recovery source")
    source = source.replace(availability_anchor, availability_replacement, 1)
    source = source.replace(selection_anchor, selection_replacement, 1)
    return source.encode("utf-8")


def patched_index_html() -> bytes:
    source = INDEX_PATH.read_text(encoding="utf-8")
    source, replacements = re.subn(
        r'app\.js\?v=[^"\']+',
        "app.js?v=20260801-blank-series-selection",
        source,
        count=1,
    )
    if replacements != 1:
        raise RuntimeError("Cached index.html does not contain the app.js asset")
    return source.encode("utf-8")


PATCHED_APP_JS = patched_app_js()
PATCHED_INDEX_HTML = patched_index_html()


class RecoveryProxy(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_ROOT), **kwargs)

    def _redirect_root(self) -> None:
        self.send_response(http.HTTPStatus.FOUND)
        self.send_header("Location", "/demo/index.html")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _forward_api(self) -> None:
        if self.path == "/":
            self._redirect_root()
            return

        upstream = f"http://backend:8000{self.path}"
        body_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(body_length) if body_length else None
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
            and name.lower() not in {"host", "accept-encoding"}
        }
        request = urllib.request.Request(
            upstream,
            data=body,
            headers=headers,
            method=self.command,
        )

        try:
            response = urllib.request.urlopen(
                request,
                timeout=300,
            )
        except urllib.error.HTTPError as exc:
            response = exc
        except Exception as exc:
            payload = f"Recovery proxy error: {exc}".encode("utf-8")
            self.send_response(http.HTTPStatus.BAD_GATEWAY)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return

        with response:
            payload = b"" if self.command == "HEAD" else response.read()
            self.send_response(response.status)
            for name, value in response.headers.items():
                lower_name = name.lower()
                if lower_name in HOP_BY_HOP_HEADERS or lower_name == "content-type":
                    continue
                self.send_header(name, value)
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset()
            if charset and content_type.startswith("text/"):
                content_type = f"{content_type}; charset={charset}"
            self.send_header("Content-Type", content_type)
            content_length = response.headers.get("Content-Length")
            self.send_header(
                "Content-Length",
                content_length if self.command == "HEAD" and content_length else str(len(payload)),
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

    def _serve_static_or_api(self) -> None:
        request_path = self.path.partition("?")[0]
        if request_path == "/":
            self._redirect_root()
        elif request_path.startswith("/api/"):
            self._forward_api()
        elif request_path == "/demo/app.js":
            self._serve_bytes(PATCHED_APP_JS, "application/javascript; charset=utf-8")
        elif request_path == "/demo/index.html":
            self._serve_bytes(PATCHED_INDEX_HTML, "text/html; charset=utf-8")
        elif self.command == "HEAD":
            super().do_HEAD()
        else:
            super().do_GET()

    do_GET = _serve_static_or_api
    do_HEAD = _serve_static_or_api
    do_POST = _forward_api
    do_PUT = _forward_api
    do_PATCH = _forward_api
    do_DELETE = _forward_api


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 80), RecoveryProxy)
    server.serve_forever()

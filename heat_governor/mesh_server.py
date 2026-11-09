"""``mesh serve`` HTTP server: the dashboard page and its JSON API on one origin.

``MeshServer(reader, host="127.0.0.1", port=9480)`` wraps a stdlib
``ThreadingHTTPServer`` (the same pattern as ``state.StateServer``). ``reader`` is
any object with ``snapshot()`` and ``trend(name, since_s, max_points)`` returning
``{"source", "samples"}`` (normally a ``FleetReader``). ``start()`` serves on a daemon
thread, ``stop()`` shuts down, ``address`` is the bound ``(host, port)``; port 0 picks an
ephemeral port.

Routes (GET, and HEAD): ``/`` the page from ``heat_governor/web/index.html``;
``/vendor/<file>`` an allow-listed plain filename from ``web/vendor/``;
``/api/fleet``; ``/api/history?machine=<name>&range=15m|1h|6h|24h|7d`` (default 1h), which
answers ``{"machine", "range", "source": "records"|"buffer", "samples"}``.
Everything else is 404, other methods 405. The page and API share one origin, so
no CORS headers are sent. The default bind is loopback; only an explicit ``host``
changes it.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9480
RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}
MAX_POINTS = 300
_VENDOR_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_VENDOR_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
_NOT_FOUND = {"error": "not found"}
_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'"
)


def _web_file(name: str, vendor: bool = False) -> bytes | None:
    root = resources.files("heat_governor").joinpath("web")
    if vendor:
        root = root.joinpath("vendor")
    entry = root.joinpath(name)
    if not entry.is_file():
        return None
    return entry.read_bytes()


class MeshServer:
    def __init__(self, reader: object, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.reader = reader
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, code: int, doc: object) -> None:
                self._send(
                    code,
                    json.dumps(doc).encode(),
                    "application/json",
                    {"Cache-Control": "no-store"},
                )

            def _index(self) -> None:
                body = _web_file("index.html")
                if body is None:
                    return self._json(404, _NOT_FOUND)
                self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": _CSP})

            def _vendor(self, name: str) -> None:
                ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
                body = None
                if _VENDOR_NAME.match(name) and name not in (".", "..") and ext in _VENDOR_TYPES:
                    body = _web_file(name, vendor=True)
                if body is None:
                    return self._json(404, _NOT_FOUND)
                self._send(200, body, _VENDOR_TYPES[ext])

            def _route(self) -> None:
                parts = urlsplit(self.path)
                path = parts.path
                if path == "/":
                    return self._index()
                if path.startswith("/vendor/"):
                    return self._vendor(path[len("/vendor/") :])
                if path == "/api/fleet":
                    return self._json(200, owner.reader.snapshot())
                if path == "/api/history":
                    return self._history(parse_qs(parts.query))
                return self._json(404, _NOT_FOUND)

            def _history(self, query: dict) -> None:
                name = (query.get("machine") or [""])[0]
                rng = (query.get("range") or ["1h"])[0]
                if not name:
                    return self._json(400, {"error": "machine is required"})
                if rng not in RANGES:
                    return self._json(400, {"error": f"range must be one of {', '.join(RANGES)}"})
                try:
                    got = owner.reader.trend(name, since_s=RANGES[rng], max_points=MAX_POINTS)
                except KeyError:
                    return self._json(404, {"error": f"unknown machine: {name}"})
                self._json(
                    200,
                    {
                        "machine": name,
                        "range": rng,
                        "source": got["source"],
                        "samples": got["samples"],
                    },
                )

            def _guarded(self) -> None:
                try:
                    self._route()
                except Exception:  # noqa: BLE001 - never leak a traceback to the client
                    self._json(500, {"error": "internal error"})

            def _not_allowed(self) -> None:
                self._send(
                    405,
                    b'{"error":"method not allowed"}',
                    "application/json",
                    {"Allow": "GET, HEAD"},
                )

            do_GET = do_HEAD = _guarded  # noqa: N815 - http.server API
            do_POST = do_PUT = do_DELETE = do_PATCH = _not_allowed  # noqa: N815

            def log_message(self, *_args: object) -> None:
                return

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.address = self.httpd.server_address
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

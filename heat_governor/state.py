"""The published state: the contract every client reads.

The daemon rewrites one small JSON document about once a second (atomic
rename, so a reader never sees a half-written file) and, optionally, serves the
same document over HTTP. The schema is documented in
``docs/contracts/state.md``; bump ``SCHEMA`` on any incompatible change.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCHEMA = 1
ENV_STATE = "HEAT_GOVERNOR_STATE"


def default_state_path(environ: dict[str, str] | None = None) -> Path:
    """``$HEAT_GOVERNOR_STATE``, else ``$XDG_RUNTIME_DIR/heat-governor/state.json``,
    else ``~/.local/state/heat-governor/state.json``."""
    env = os.environ if environ is None else environ
    if env.get(ENV_STATE):
        return Path(env[ENV_STATE])
    if env.get("XDG_RUNTIME_DIR"):
        return Path(env["XDG_RUNTIME_DIR"]) / "heat-governor" / "state.json"
    return Path.home() / ".local" / "state" / "heat-governor" / "state.json"


def default_data_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "heat-governor"


def write_state(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    # created with the process umask (022 by default -> 0644): readable by a client
    # running as another user or in a container through a read-only bind mount
    tmp.write_text(json.dumps(doc, separators=(",", ":")))
    os.replace(tmp, path)


def read_state(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


class StateServer:
    """``GET /state`` (the document) and ``GET /healthz``. Read-only, no auth:
    the document carries temperatures and container names, nothing secret. Bind
    to 127.0.0.1 unless remote clients need it."""

    def __init__(self, host: str, port: int) -> None:
        self.doc: dict = {"schema": SCHEMA, "level": "unknown"}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if self.path.split("?")[0] in ("/state", "/"):
                    body = json.dumps(owner.doc).encode()
                    code = 200
                elif self.path == "/healthz":
                    body, code = b'{"ok":true}', 200
                else:
                    body, code = b'{"error":"not found"}', 404
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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

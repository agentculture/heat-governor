"""The unix-socket Docker client against a stub Engine API server."""

from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from heat_governor.docker import Docker, DockerError

INFO = {
    "Id": "abc123",
    "Name": "/job",
    "State": {"Status": "running"},
    "Config": {"Labels": {"heat-governor.role": "batch"}},
    "HostConfig": {"NanoCpus": 4000000000},
}


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def api(tmp_path: Path):
    calls: list[tuple[str, str, object]] = []

    class H(BaseHTTPRequestHandler):
        def address_string(self) -> str:  # unix sockets have no peer address
            return "unix"

        def _send(self, code: int, body: object = None) -> None:
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            calls.append(("GET", self.path, None))
            if self.path.startswith("/containers/json"):
                self._send(200, [{"Id": "abc123"}])
            elif self.path == "/containers/abc123/json":
                self._send(200, INFO)
            else:
                self._send(404, {"message": "No such container"})

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            calls.append(("POST", self.path, body))
            self._send(204)

        def log_message(self, *_a: object) -> None:
            return

    sock = str(tmp_path / "d.sock")
    srv = _Server(sock, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Docker(sock, timeout=5), calls
    srv.shutdown()
    srv.server_close()


def test_inspect_and_labelled(api) -> None:
    d, _ = api
    c = d.inspect("abc123")
    assert (c.name, c.state, c.role, c.nano_cpus) == ("job", "running", "batch", 4000000000)
    assert [x.id for x in d.labelled()] == ["abc123"]


def test_actions_hit_the_engine_api(api) -> None:
    d, calls = api
    d.pause("abc123")
    d.unpause("abc123")
    d.set_nano_cpus("abc123", 2000000000)
    posts = [(p, b) for m, p, b in calls if m == "POST"]
    assert posts == [
        ("/containers/abc123/pause", None),
        ("/containers/abc123/unpause", None),
        ("/containers/abc123/update", {"NanoCpus": 2000000000}),
    ]


def test_http_error_raises(api) -> None:
    d, _ = api
    with pytest.raises(DockerError, match="404"):
        d.inspect("nope")


def test_missing_socket_raises(tmp_path: Path) -> None:
    docker = Docker(str(tmp_path / "none.sock"))
    with pytest.raises(DockerError):
        docker.inspect("x")

"""LocalTransport tests against a real loopback http.server on an ephemeral port."""

from __future__ import annotations

import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from heat_governor.state import StateServer
from heat_governor.transport import LocalTransport, Unreachable

DOC = {"schema": 1, "host": "box", "level": "cool", "at": 1.0, "admit": True}


@pytest.fixture
def server():
    srv = StateServer("127.0.0.1", 0)
    srv.doc = dict(DOC)
    srv.start()
    yield srv
    srv.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_fetch_reads_state(server):
    t = LocalTransport(timeout_s=2.0, port=server.address[1])
    assert t.fetch() == DOC
    server.doc = {**DOC, "at": 2.0}
    assert t.fetch()["at"] == 2.0  # every fetch is a fresh read


def test_nothing_listening_is_unreachable():
    t = LocalTransport(timeout_s=1.0, port=_free_port())
    start = time.monotonic()
    with pytest.raises(Unreachable) as err:
        t.fetch()
    assert "local daemon unreachable" in err.value.message
    assert err.value.code == 2
    assert time.monotonic() - start < 1.5


class _Handler(BaseHTTPRequestHandler):
    body = b""
    status = 200
    delay = 0.0

    def do_GET(self) -> None:  # noqa: N802
        time.sleep(self.delay)
        try:
            self.send_response(self.status)
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
        except OSError:
            pass  # the client already gave up (the timeout test): nothing to report

    def log_message(self, *_a: object) -> None:
        return


def _serve(body: bytes, status: int = 200, delay: float = 0.0):
    handler = type("H", (_Handler,), {"body": body, "status": status, "delay": delay})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.mark.parametrize(
    "body,status",
    [(b"", 200), (b"not json", 200), (b"[1, 2]", 200), (b'{"error":"x"}', 404)],
)
def test_bad_responses_are_unreachable(body, status):
    httpd = _serve(body, status)
    try:
        with pytest.raises(Unreachable):
            LocalTransport(timeout_s=1.0, port=httpd.server_address[1]).fetch()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_oversized_document_is_refused():
    httpd = _serve(b'{"pad":"' + b"x" * (1 << 20) + b'"}')
    try:
        with pytest.raises(Unreachable, match="too large"):
            LocalTransport(timeout_s=2.0, port=httpd.server_address[1]).fetch()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_slow_daemon_times_out():
    httpd = _serve(b"{}", delay=2.0)
    try:
        start = time.monotonic()
        with pytest.raises(Unreachable):
            LocalTransport(timeout_s=0.5, port=httpd.server_address[1]).fetch()
        assert time.monotonic() - start < 1.5
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_close_and_context_manager(server):
    with LocalTransport(timeout_s=1.0, port=server.address[1]) as t:
        assert t.fetch()["host"] == "box"
    t.close()  # idempotent


# -- records history (d2) ---------------------------------------------------------


def test_local_history_reads_this_machines_records(tmp_path, monkeypatch):
    import json as _json

    rd = tmp_path / "heat-governor" / "records"
    rd.mkdir(parents=True)
    t0 = 1_794_000_000.0
    day = time.strftime("%Y%m%d", time.gmtime(t0))
    (rd / f"{day}.jsonl").write_text(
        _json.dumps({"t": t0 + 5, "temp_c": 48.5, "level": "cool", "system": {}}) + "\n"
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    out = LocalTransport(timeout_s=1.0).history(t0, t0 + 60, 5)
    assert [s["temp_c"] for s in out["samples"]] == [48.5]
    assert out["files"] == [f"{day}.jsonl"]


def test_local_history_refuses_bad_arguments():
    with pytest.raises(Unreachable):
        LocalTransport(timeout_s=1.0).history(float("nan"), 1.0, 5)


def test_local_history_failure_is_unreachable(monkeypatch):
    import heat_governor.transport as transport

    monkeypatch.setattr(transport, "PYTHON", "/nonexistent/python3")
    with pytest.raises(Unreachable):
        LocalTransport(timeout_s=1.0).history(1.0, 100.0, 5)

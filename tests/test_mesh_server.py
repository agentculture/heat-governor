"""mesh_server: same-origin page + JSON API over a fake reader, on an ephemeral port."""

from __future__ import annotations

import inspect
import json
import urllib.error
import urllib.request

import pytest

from heat_governor import mesh_server
from heat_governor.mesh_server import DEFAULT_HOST, DEFAULT_PORT, MeshServer


class FakeReader:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def snapshot(self) -> dict:
        return {"schema": 1, "reader": {}, "machines": [{"name": "spark-a"}]}

    def history(self, name: str, since_s: int = 86400, max_points: int = 300) -> list[dict]:
        self.calls.append((name, since_s, max_points))
        if name != "spark-a":
            raise KeyError(name)
        return [{"t": 1, "temp": 50.0}]


@pytest.fixture
def server():
    reader = FakeReader()
    srv = MeshServer(reader, port=0)
    srv.start()
    srv.reader = reader  # type: ignore[attr-defined]
    yield srv
    srv.stop()


def get(srv, path: str):
    url = f"http://{srv.address[0]}:{srv.address[1]}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # nosec B310
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_defaults_are_loopback():
    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_PORT == 9480
    assert inspect.signature(MeshServer).parameters["host"].default == "127.0.0.1"
    assert mesh_server.MeshServer(FakeReader(), port=0).address[0] == "127.0.0.1"


def test_index_page(server):
    code, h, body = get(server, "/")
    assert code == 200
    assert h["Content-Type"] == "text/html; charset=utf-8"
    assert "default-src 'self'" in h["Content-Security-Policy"]
    assert b"Heat Governor" in body
    assert "Access-Control-Allow-Origin" not in h


def test_fleet(server):
    code, h, body = get(server, "/api/fleet")
    assert code == 200
    assert h["Content-Type"] == "application/json"
    assert h["Cache-Control"] == "no-store"
    assert json.loads(body)["machines"][0]["name"] == "spark-a"


def test_history(server):
    code, h, body = get(server, "/api/history?machine=spark-a&range=6h")
    assert code == 200
    assert h["Cache-Control"] == "no-store"
    doc = json.loads(body)
    assert doc == {"machine": "spark-a", "range": "6h", "samples": [{"t": 1, "temp": 50.0}]}
    assert server.reader.calls[-1] == ("spark-a", 21600, 300)
    get(server, "/api/history?machine=spark-a")
    assert server.reader.calls[-1] == ("spark-a", 3600, 300)


@pytest.mark.parametrize(
    "path,code",
    [
        ("/api/history", 400),
        ("/api/history?machine=spark-a&range=2d", 400),
        ("/api/history?machine=nope", 404),
        ("/nope", 404),
        ("/api/other", 404),
        ("/vendor/", 404),
        ("/vendor/missing.js", 404),
        ("/vendor/../index.html", 404),
        ("/vendor/%2e%2e/index.html", 404),
        ("/vendor/..%2findex.html", 404),
        ("/vendor/%2e%2e%2findex.html", 404),
        ("/vendor/sub/README.txt", 404),
        ("/vendor/README.txt/", 404),
    ],
)
def test_errors(server, path, code):
    status, h, body = get(server, path)
    assert status == code
    if path.startswith("/api/"):
        assert "error" in json.loads(body)


def test_vendor_readme(server):
    code, h, body = get(server, "/vendor/README.txt")
    assert code == 200
    assert h["Content-Type"].startswith("text/plain")
    assert b"vendored" in body.lower()


def test_post_not_allowed(server):
    req = urllib.request.Request(
        f"http://{server.address[0]}:{server.address[1]}/api/fleet", data=b"x", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)  # nosec B310
    assert e.value.code in (405, 501)


def test_head(server):
    req = urllib.request.Request(f"http://{server.address[0]}:{server.address[1]}/", method="HEAD")
    with urllib.request.urlopen(req, timeout=5) as r:  # nosec B310
        assert r.status == 200
        assert r.read() == b""

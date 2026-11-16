"""mesh_server: same-origin page + JSON API over a fake reader, on an ephemeral port."""

from __future__ import annotations

import http.client
import inspect
import json
import os
import secrets
import threading
import urllib.error
import urllib.request

import pytest

from heat_governor import boundaries_service as svc
from heat_governor import mesh_server
from heat_governor.cli import main
from heat_governor.cli._commands import mesh as mesh_cmd
from heat_governor.fleet import Fleet, Machine, load_fleet
from heat_governor.fleet_reader import FleetReader
from heat_governor.mesh_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    MeshServer,
    TokenFileError,
    load_tokens,
)
from tests.test_boundaries_service import HOG, BoxTransport, _version


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

    def trend(self, name: str, since_s: int = 86400, max_points: int = 300) -> dict:
        return {"source": "records", "samples": self.history(name, since_s, max_points)}


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
    assert doc == {
        "machine": "spark-a",
        "range": "6h",
        "source": "records",
        "samples": [{"t": 1, "temp": 50.0}],
    }
    assert server.reader.calls[-1] == ("spark-a", 21600, 300)
    get(server, "/api/history?machine=spark-a")
    assert server.reader.calls[-1] == ("spark-a", 3600, 300)


def test_history_7d_range(server):
    code, _, body = get(server, "/api/history?machine=spark-a&range=7d")
    assert code == 200
    assert json.loads(body)["range"] == "7d"
    assert server.reader.calls[-1] == ("spark-a", 604800, 300)
    assert mesh_server.RANGES["7d"] == 604800


def test_history_source_is_passed_through(server):
    server.reader.trend = lambda name, since_s, max_points: {"source": "buffer", "samples": []}
    _, _, body = get(server, "/api/history?machine=spark-a&range=1h")
    assert json.loads(body)["source"] == "buffer"


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


class BlockingReader(FakeReader):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)

    def trend(self, name: str, since_s: int = 86400, max_points: int = 300) -> dict:
        self.entered.release()
        self.release.wait(10)
        return super().trend(name, since_s, max_points)


def test_history_concurrency_limit():
    reader = BlockingReader()
    srv = MeshServer(reader, port=0)
    srv.start()
    try:
        results: list = []
        threads = [
            threading.Thread(
                target=lambda: results.append(get(srv, "/api/history?machine=spark-a"))
            )
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for _ in range(4):
            assert reader.entered.acquire(timeout=5)
        code, headers, body = get(srv, "/api/history?machine=spark-a")
        assert code == 503
        assert headers.get("Retry-After")
        assert json.loads(body) == {"error": "busy, retry shortly"}
        reader.release.set()
        for t in threads:
            t.join(10)
        assert sorted(r[0] for r in results) == [200] * 4
        assert get(srv, "/api/history?machine=spark-a")[0] == 200
    finally:
        reader.release.set()
        srv.stop()


def test_unexpected_error_is_logged_and_sanitized(server, capsys):
    def boom() -> dict:
        raise RuntimeError("secret-detail-xyz")

    server.reader.snapshot = boom
    code, _h, body = get(server, "/api/fleet")
    assert code == 500
    assert json.loads(body) == {"error": "internal error"}
    assert b"secret-detail-xyz" not in body
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "secret-detail-xyz" in err


# -- boundaries: token-guarded list, preview and apply ----------------------------------------

# built at run time so scripts/scan-secrets.py never sees a literal token
TOKEN = "t" * 40
OTHER = secrets.token_urlsafe(32)


class Box:
    """A mesh server with writes enabled over one fake box ``a``."""

    def __init__(self, tmp_path, tokens=None, **kw):
        self.box = BoxTransport()
        fleet = Fleet(machines=(Machine("a", "ssh", "a.example"),), poll_s=10.0, timeout_s=5.0)
        self.reader = FleetReader(fleet, transport_factory=lambda m: self.box)
        self.reader.poll_once("a")
        self.reader.trend = FakeReader().trend  # type: ignore[method-assign]
        self.audit = tmp_path / "state" / "audit.jsonl"
        service = svc.BoundariesService(
            self.reader.snapshot, self.reader.transport, audit_path=self.audit
        )
        toks = {"ops-laptop": TOKEN, "spare": OTHER} if tokens is None else tokens
        self.srv = MeshServer(self.reader, port=0, tokens=toks, boundaries=service, **kw)
        self.srv.start()
        self.host = f"127.0.0.1:{self.srv.address[1]}"

    def request(self, method, path, body=None, headers=None, token=TOKEN, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.address[1], timeout=10)
        hdrs = {"Host": self.host}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers or {})
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in hdrs.items():
                if v is not None:
                    conn.putheader(k, v)
            if data is not None and "Content-Length" not in hdrs:
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            resp = conn.getresponse()
            out = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, out
        finally:
            conn.close()

    def stop(self):
        self.srv.stop()


@pytest.fixture
def wbox(tmp_path):
    b = Box(tmp_path)
    yield b
    b.stop()


@pytest.fixture
def rbox(tmp_path):
    """The same fake box, served with no token file (read-only)."""
    b = Box(tmp_path, tokens={})
    yield b
    b.stop()


def etag(version: str) -> str:
    return f'"{version}"'


ADD = {"operation": "add", "rule": HOG}
URLS = [
    ("GET", "/api/boundaries/a", None),
    ("POST", "/api/boundaries/a/preview", {"rule": HOG}),
    ("POST", "/api/boundaries/a", ADD),
]


# no token file


@pytest.mark.parametrize("method,path,body", URLS)
def test_no_token_file_refuses_every_list_preview_and_write_with_403(rbox, method, path, body):
    code, h, out = rbox.request(method, path, body, headers={"If-Match": etag("missing")})
    assert code == 403
    assert json.loads(out)["outcome"] == "writes_disabled"
    assert h["cache-control"] == "no-store"
    assert rbox.box.calls == ["fetch"]  # nothing reached the box


def test_no_token_file_probe_says_writes_disabled(rbox):
    code, h, out = rbox.request("GET", "/api/boundaries", token=None)
    assert code == 200
    assert json.loads(out) == {"writes_enabled": False}
    assert h["cache-control"] == "no-store"


def test_probe_says_writes_enabled_without_a_token(wbox):
    code, _h, out = wbox.request("GET", "/api/boundaries", token=None)
    assert code == 200
    assert json.loads(out) == {"writes_enabled": True}


def test_writes_disabled_without_a_service_or_tokens():
    srv = MeshServer(FakeReader(), port=0)
    assert srv.writes_enabled is False


# authentication


@pytest.mark.parametrize("method,path,body", URLS)
@pytest.mark.parametrize("token", [None, "wrong-" + "x" * 40, TOKEN + "x", TOKEN[:-1], ""])
def test_missing_or_wrong_token_is_401_and_reaches_nothing(wbox, method, path, body, token):
    code, h, out = wbox.request(method, path, body, token=token, headers={"If-Match": '"m"'})
    assert code == 401
    assert h["www-authenticate"].startswith("Bearer")
    assert json.loads(out)["outcome"] == "unauthorized"
    assert TOKEN.encode() not in out
    assert wbox.box.calls == ["fetch"]
    assert not wbox.audit.exists()


@pytest.mark.parametrize(
    "query", ["?token={t}", "?access_token={t}", "?authorization=Bearer%20{t}", "?bearer={t}"]
)
def test_token_in_query_string_is_ignored(wbox, query):
    path = "/api/boundaries/a" + query.format(t=TOKEN)
    code, _h, out = wbox.request("GET", path, token=None)
    assert code == 401
    assert TOKEN.encode() not in out


def test_token_in_cookie_or_body_is_ignored(wbox):
    code, _h, _ = wbox.request("GET", "/api/boundaries/a", token=None, headers={"Cookie": TOKEN})
    assert code == 401
    body = {"rule": HOG, "token": TOKEN}
    code, _h, _ = wbox.request("POST", "/api/boundaries/a/preview", body, token=None)
    assert code == 401


@pytest.mark.parametrize(
    "value", ["Basic {t}", "Bearer  {t}", "Bearer {t} extra", "{t}", "Token {t}", "Bearer"]
)
def test_malformed_authorization_is_401(wbox, value):
    hdr = {"Authorization": value.format(t=TOKEN)}
    code, _h, _ = wbox.request("GET", "/api/boundaries/a", token=None, headers=hdr)
    assert code == 401


def test_any_configured_token_works_and_scheme_is_case_insensitive(wbox):
    assert wbox.request("GET", "/api/boundaries/a", token=OTHER)[0] == 200
    hdr = {"Authorization": f"bearer {TOKEN}"}
    assert wbox.request("GET", "/api/boundaries/a", token=None, headers=hdr)[0] == 200


def test_tokens_are_compared_with_compare_digest_against_every_token(monkeypatch):
    calls = []
    real = mesh_server.hmac.compare_digest

    def spy(a, b):
        calls.append(b)
        return real(a, b)

    monkeypatch.setattr(mesh_server.hmac, "compare_digest", spy)
    tokens = {"one": "a" * 32, "two": "b" * 32, "three": "c" * 32}
    assert mesh_server.match_token(tokens, "a" * 32) == "one"
    assert len(calls) == 3  # no short-circuit on the first match
    calls.clear()
    assert mesh_server.match_token(tokens, "z" * 32) is None
    assert len(calls) == 3


def test_failed_auth_is_rate_limited_with_429(tmp_path):
    now = [1000.0]
    b = Box(tmp_path, clock=lambda: now[0])
    try:
        limit = mesh_server.AUTH_FAILURES_MAX
        for _ in range(limit):
            assert b.request("GET", "/api/boundaries/a", token="nope")[0] == 401
        code, h, out = b.request("GET", "/api/boundaries/a", token="nope")
        assert code == 429
        assert int(h["retry-after"]) >= 1
        assert json.loads(out)["outcome"] == "rate_limited"
        # even the right token, from that address, waits until the window passes
        assert b.request("GET", "/api/boundaries/a")[0] == 429
        now[0] += mesh_server.AUTH_WINDOW_S + 1
        assert b.request("GET", "/api/boundaries/a")[0] == 200
    finally:
        b.stop()


def test_rate_limit_is_per_client_address():
    now = [0.0]
    lim = mesh_server.AuthLimiter(max_failures=2, window_s=60, clock=lambda: now[0])
    lim.fail("100.64.0.9")
    lim.fail("100.64.0.9")
    assert lim.blocked("100.64.0.9") >= 1
    assert lim.blocked("100.64.0.1") == 0
    now[0] = 61.0
    assert lim.blocked("100.64.0.9") == 0


def test_rate_limit_remembers_a_bounded_number_of_addresses():
    lim = mesh_server.AuthLimiter(max_failures=1, window_s=60, clock=lambda: 0.0)
    for n in range(lim.MAX_CLIENTS + 50):
        lim.fail(f"client-{n}")
    assert len(lim._fails) == lim.MAX_CLIENTS


# list, preview, apply through the fake box


def test_list_returns_rules_version_and_etag(wbox):
    wbox.box.rules = [dict(HOG)]
    code, h, out = wbox.request("GET", "/api/boundaries/a")
    assert code == 200
    doc = json.loads(out)
    assert doc["outcome"] == "ok"
    assert doc["version"] == _version(wbox.box.rules)
    assert doc["rules"][0]["editable"] is True
    assert h["etag"] == etag(doc["version"])
    assert h["cache-control"] == "no-store"


def test_list_unknown_machine_is_404(wbox):
    code, _h, out = wbox.request("GET", "/api/boundaries/nope")
    assert code == 404
    assert json.loads(out)["outcome"] == svc.UNKNOWN_MACHINE


@pytest.mark.parametrize(
    "path", ["/api/boundaries/a%2fb", "/api/boundaries/..", "/api/boundaries/"]
)
def test_bad_machine_segment_is_404(wbox, path):
    code, _h, out = wbox.request("GET", path)
    assert code == 404
    assert "error" in json.loads(out) or "outcome" in json.loads(out)


def test_preview_runs_on_the_box(wbox):
    code, _h, out = wbox.request("POST", "/api/boundaries/a/preview", {"rule": HOG})
    assert code == 200
    assert json.loads(out)["outcome"] == "ok"
    assert wbox.box.calls[-1] == "preview"


def test_preview_with_extra_keys_is_400(wbox):
    body = {"rule": HOG, "allow_kill": True}
    code, _h, out = wbox.request("POST", "/api/boundaries/a/preview", body)
    assert code == 400
    assert json.loads(out)["outcome"] == "invalid"


def test_apply_saves_and_writes_audit_with_label(wbox):
    code, h, out = wbox.request(
        "POST", "/api/boundaries/a", ADD, headers={"If-Match": etag("missing")}
    )
    assert code == 200
    doc = json.loads(out)
    assert doc["outcome"] == "saved"
    assert wbox.box.rules[0]["name"] == "hog"
    assert h["etag"] == etag(doc["version"])
    lines = [json.loads(x) for x in wbox.audit.read_text().splitlines()]
    assert [x["outcome"] for x in lines] == ["attempt", "saved"]
    assert {x["label"] for x in lines} == {"ops-laptop"}
    assert TOKEN not in wbox.audit.read_text()


def test_stale_if_match_is_409_with_the_current_rules(wbox):
    wbox.box.rules = [dict(HOG, max_mib=1024)]
    current = _version(wbox.box.rules)
    rule = dict(HOG, max_mib=8192)
    body = {"operation": "set", "rule": rule}
    code, _h, out = wbox.request(
        "POST", "/api/boundaries/a", body, headers={"If-Match": etag("missing")}
    )
    assert code == 409
    doc = json.loads(out)
    assert doc["outcome"] == "conflict"
    assert doc["version"] == current
    assert doc["rules"] == [dict(HOG, max_mib=1024)]
    assert wbox.box.rules == [dict(HOG, max_mib=1024)]  # nothing written


def test_apply_without_if_match_is_428(wbox):
    code, _h, out = wbox.request("POST", "/api/boundaries/a", ADD)
    assert code == 428
    assert json.loads(out)["outcome"] == "precondition_required"
    assert "apply" not in wbox.box.calls


@pytest.mark.parametrize("value", ["*", 'W/"missing"', '"a", "b"', '"', '"has space"', ""])
def test_unusable_if_match_is_400_or_428(wbox, value):
    code, _h, _ = wbox.request("POST", "/api/boundaries/a", ADD, headers={"If-Match": value})
    assert code in (400, 428)
    assert "apply" not in wbox.box.calls


def test_body_base_version_must_agree_with_if_match(wbox):
    body = dict(ADD, base_version="other")
    code, _h, out = wbox.request(
        "POST", "/api/boundaries/a", body, headers={"If-Match": '"missing"'}
    )
    assert code == 400
    assert json.loads(out)["outcome"] == "invalid"
    assert "apply" not in wbox.box.calls
    body = dict(ADD, base_version="missing")
    code, _h, _ = wbox.request("POST", "/api/boundaries/a", body, headers={"If-Match": '"missing"'})
    assert code == 200


def test_bare_if_match_is_accepted(wbox):
    code, _h, _ = wbox.request("POST", "/api/boundaries/a", ADD, headers={"If-Match": "missing"})
    assert code == 200


def test_invalid_request_is_400_with_problems(wbox):
    body = {"operation": "zap", "rule": {"name": ""}}
    code, _h, out = wbox.request("POST", "/api/boundaries/a", body, headers={"If-Match": '"m"'})
    assert code == 400
    doc = json.loads(out)
    assert doc["outcome"] == "invalid"
    assert doc["problems"]


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'"x"', b"\xff\xfe", b"NaN"])
def test_bad_json_body_is_400(wbox, raw):
    code, _h, out = wbox.request(
        "POST", "/api/boundaries/a", raw=raw, headers={"If-Match": '"m"', "Content-Type": JT}
    )
    assert code == 400
    assert json.loads(out)["outcome"] == "invalid"


JT = "application/json"


def test_would_kill_and_old_version_map_to_4xx(wbox):
    wbox.box.state = dict(wbox.box.state, version="0.17.1")
    wbox.reader.poll_once("a")
    code, _h, out = wbox.request("GET", "/api/boundaries/a")
    assert code == 422
    assert json.loads(out)["outcome"] == svc.VERSION_TOO_OLD


def test_service_exception_is_500_without_detail(wbox, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("service-detail-xyz")

    wbox.srv.boundaries.list = boom
    code, _h, out = wbox.request("GET", "/api/boundaries/a")
    assert code == 500
    assert json.loads(out) == {"error": "internal error"}
    assert b"service-detail-xyz" not in out


# Host, Origin, Fetch metadata, Content-Type, size


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:{port}",
        "127.0.0.1",
        "127.0.0.1:1",
        "127.0.0.1.evil.example:{port}",
        "127.0.0.1:{port}.evil.example",
        "user@127.0.0.1:{port}",
        "127.0.0.2:{port}",
        "0.0.0.0:{port}",
        "",
    ],
)
@pytest.mark.parametrize("method,path,body", URLS)
def test_foreign_host_is_refused(wbox, host, method, path, body):
    port = wbox.srv.address[1]
    hdr = {"Host": host.format(port=port), "If-Match": '"missing"'}
    code, _h, out = wbox.request(method, path, body, headers=hdr)
    assert code == 403
    assert json.loads(out)["outcome"] == "host_not_allowed"
    assert wbox.box.calls == ["fetch"]


def test_missing_host_is_refused(wbox):
    code, _h, out = wbox.request("GET", "/api/boundaries/a", headers={"Host": None})
    assert code == 403
    assert json.loads(out)["outcome"] == "host_not_allowed"


def test_localhost_and_configured_names_are_allowed(tmp_path):
    b = Box(tmp_path, server_names=("EliteMini.tail.example",))
    try:
        port = b.srv.address[1]
        for host in (f"localhost:{port}", f"elitemini.tail.example:{port}", f"LOCALHOST:{port}"):
            assert b.request("GET", "/api/boundaries/a", headers={"Host": host})[0] == 200
        assert b.request("GET", "/api/boundaries/a", headers={"Host": "elitemini"})[0] == 403
    finally:
        b.stop()


def test_allowed_hosts_for_a_non_loopback_bind():
    hosts = mesh_server.allowed_hosts("100.95.106.3", 9480, ("elitemini",))
    assert hosts == {"100.95.106.3:9480", "elitemini:9480"}
    assert mesh_server.allowed_hosts("::1", 80, ()) == {
        "[::1]:80",
        "[::1]",
        "localhost:80",
        "localhost",
    }


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
def test_wildcard_bind_with_tokens_is_refused(host):
    reader = FakeReader()
    with pytest.raises(ValueError, match="specific"):
        MeshServer(reader, host=host, port=0, tokens={"x": TOKEN})


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "null", "http://127.0.0.1:1", "https://127.0.0.1:{port}"]
)
@pytest.mark.parametrize("method,path,body", URLS)
def test_foreign_origin_is_refused(wbox, origin, method, path, body):
    hdr = {"Origin": origin.format(port=wbox.srv.address[1]), "If-Match": '"missing"'}
    code, _h, out = wbox.request(method, path, body, headers=hdr)
    assert code == 403
    assert json.loads(out)["outcome"] == "origin_not_allowed"
    assert wbox.box.calls == ["fetch"]


def test_own_origin_and_no_origin_are_accepted(wbox):
    own = {"Origin": f"http://{wbox.host}", "If-Match": '"missing"'}
    assert wbox.request("POST", "/api/boundaries/a/preview", {"rule": HOG}, headers=own)[0] == 200
    assert wbox.request("POST", "/api/boundaries/a/preview", {"rule": HOG})[0] == 200


@pytest.mark.parametrize(
    "site,code", [("cross-site", 403), ("same-site", 403), ("same-origin", 200)]
)
def test_fetch_metadata_cross_site_is_refused(wbox, site, code):
    hdr = {"Sec-Fetch-Site": site}
    assert wbox.request("POST", "/api/boundaries/a/preview", {"rule": HOG}, headers=hdr)[0] == code


@pytest.mark.parametrize(
    "ctype",
    [
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "application/json-patch+json",
        "application/jsonx",
        None,
        "application/json; charset=latin-1",
    ],
)
def test_wrong_content_type_is_415(wbox, ctype):
    hdr = {"Content-Type": ctype, "If-Match": '"missing"'}
    code, _h, out = wbox.request("POST", "/api/boundaries/a", ADD, headers=hdr)
    assert code == 415
    assert json.loads(out)["outcome"] == "unsupported_media_type"
    assert wbox.box.calls == ["fetch"]


def test_json_with_utf8_charset_is_accepted(wbox):
    hdr = {"Content-Type": "application/json; charset=utf-8"}
    assert wbox.request("POST", "/api/boundaries/a/preview", {"rule": HOG}, headers=hdr)[0] == 200


def test_body_over_the_cap_is_413_before_reading(wbox):
    big = mesh_server.MAX_BODY_BYTES + 1
    hdr = {"Content-Length": str(big), "If-Match": '"missing"', "Content-Type": JT}
    # headers only: the server must answer from Content-Length without waiting for the body
    code, _h, out = wbox.request("POST", "/api/boundaries/a", headers=hdr, raw=b"")
    assert code == 413
    assert json.loads(out)["outcome"] == "too_large"
    assert wbox.box.calls == ["fetch"]


def test_body_over_the_cap_is_413_even_without_a_token(wbox):
    hdr = {"Content-Length": str(10**9), "Content-Type": JT}
    code, _h, _ = wbox.request(
        "POST", "/api/boundaries/a/preview", headers=hdr, raw=b"", token=None
    )
    assert code == 413


@pytest.mark.parametrize(
    "hdr,code",
    [
        ({"Content-Length": None}, 411),
        ({"Content-Length": "abc"}, 400),
        ({"Content-Length": "-1"}, 400),
        ({"Transfer-Encoding": "chunked"}, 400),
    ],
)
def test_missing_bad_or_chunked_length_is_refused(wbox, hdr, code):
    hdr = {"Content-Type": JT, **hdr}
    got, _h, _ = wbox.request("POST", "/api/boundaries/a/preview", headers=hdr, raw=b"")
    assert got == code
    assert wbox.box.calls == ["fetch"]


def test_short_body_times_out_without_reaching_the_box(wbox, monkeypatch):
    monkeypatch.setattr(mesh_server, "BODY_READ_TIMEOUT_S", 0.3)
    hdr = {"Content-Length": "100", "Content-Type": JT}
    code, _h, _ = wbox.request("POST", "/api/boundaries/a/preview", headers=hdr, raw=b"{}")
    assert code == 400
    assert wbox.box.calls == ["fetch"]


# methods


@pytest.mark.parametrize(
    "method,path,allow",
    [
        ("PUT", "/api/boundaries/a", "GET, POST"),
        ("DELETE", "/api/boundaries/a", "GET, POST"),
        ("HEAD", "/api/boundaries/a", "GET, POST"),
        ("GET", "/api/boundaries/a/preview", "POST"),
        ("POST", "/api/boundaries", "GET, HEAD"),
        ("PATCH", "/api/boundaries/a/preview", "POST"),
    ],
)
def test_wrong_method_on_boundaries_routes_is_405(wbox, method, path, allow):
    code, h, _ = wbox.request(method, path)
    assert code == 405
    assert h["allow"] == allow


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_writes_to_existing_routes_stay_405(wbox, method):
    code, h, out = wbox.request(method, "/api/fleet", raw=b"x")
    assert code == 405
    assert h["allow"] == "GET, HEAD"
    assert out == b'{"error":"method not allowed"}'


# concurrency


class SlowBox(BoxTransport):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)

    def boundaries(self, verb, body, *, config_path):
        if verb == "preview":
            self.entered.release()
            self.release.wait(10)
        return super().boundaries(verb, body, config_path=config_path)


def test_a_slow_write_does_not_block_other_requests_and_extra_ones_get_503(tmp_path):
    b = Box(tmp_path)
    slow = SlowBox()
    b.box = slow
    b.reader._boxes["a"].transport = slow
    b.reader.poll_once("a")
    try:
        results: list = []
        n = mesh_server.MAX_BOUNDARIES_CONCURRENCY
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    b.request("POST", "/api/boundaries/a/preview", {"rule": HOG})
                )
            )
            for _ in range(n)
        ]
        for t in threads:
            t.start()
        for _ in range(n):
            assert slow.entered.acquire(timeout=5)
        assert get(b.srv, "/api/fleet")[0] == 200  # reads still answer
        code, h, out = b.request("POST", "/api/boundaries/a/preview", {"rule": HOG})
        assert code == 503
        assert h["retry-after"]
        assert h["cache-control"] == "no-store"
        slow.release.set()
        for t in threads:
            t.join(10)
        assert sorted(r[0] for r in results) == [200] * n
    finally:
        slow.release.set()
        b.stop()


# logging


def test_nothing_logs_a_token(wbox, capsys):
    wbox.request("GET", f"/api/boundaries/a?token={TOKEN}")
    wbox.request("GET", "/api/boundaries/a", token="wrong")
    wbox.request("POST", "/api/boundaries/a", ADD, headers={"If-Match": '"missing"'})
    wbox.request("GET", "/api/boundaries/a", headers={"Host": "evil"})
    cap = capsys.readouterr()
    assert TOKEN not in cap.out
    assert TOKEN not in cap.err
    assert OTHER not in cap.err


def _raw(srv, method, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.address[1], timeout=5)
    try:
        conn.request(method, path)
        r = conn.getresponse()
        heads = [(k, v) for k, v in r.getheaders() if k != "Date"]
        return r.status, heads, r.read()
    finally:
        conn.close()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/vendor/README.txt",
        "/vendor/missing.js",
        "/api/fleet",
        "/api/history?machine=spark-a&range=6h",
        "/api/history?machine=nope",
        "/api/history",
        "/nope",
        "/api/other",
        "/api/boundariesx",
    ],
)
def test_existing_routes_are_byte_for_byte_the_same_with_writes_enabled(method, path):
    reader = FakeReader()
    plain = MeshServer(reader, port=0)
    armed = MeshServer(reader, port=0, tokens={"ops": TOKEN}, boundaries=object())
    plain.start()
    armed.start()
    try:
        assert armed.writes_enabled
        assert _raw(armed, method, path) == _raw(plain, method, path)
    finally:
        plain.stop()
        armed.stop()


def test_csp_is_unchanged():
    assert mesh_server._CSP == (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'"
    )


# the token file


def write_tokens(tmp_path, text, mode=0o600):
    p = tmp_path / "tokens"
    p.write_text(text)
    os.chmod(p, mode)
    return p


def test_load_tokens_reads_label_lines_comments_and_blanks(tmp_path):
    p = write_tokens(tmp_path, f"# ops\n\nops-laptop = {TOKEN}\n  spare.2_x={OTHER}  \n")
    assert load_tokens(p) == {"ops-laptop": TOKEN, "spare.2_x": OTHER}


@pytest.mark.parametrize(
    "text,needle",
    [
        ("", "no tokens"),
        ("# only a comment\n", "no tokens"),
        ("{t}\n", "line 1"),
        ("bad label = {t}\n", "label"),
        ("a = short\n", "32"),
        ("a = {t}\na = {o}\n", "duplicate"),
        ("a = {t}\nb = {t}\n", "same token"),
        ("a = {t} x\n", "whitespace"),
        ("= {t}\n", "label"),
    ],
)
def test_load_tokens_refuses_malformed_files_without_printing_a_token(tmp_path, text, needle):
    p = write_tokens(tmp_path, text.format(t=TOKEN, o=OTHER))
    with pytest.raises(TokenFileError) as e:
        load_tokens(p)
    assert needle in str(e.value)
    assert TOKEN not in str(e.value)
    assert OTHER not in str(e.value)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o602, 0o620])
def test_load_tokens_refuses_a_file_open_to_group_or_others(tmp_path, mode):
    p = write_tokens(tmp_path, f"a = {TOKEN}\n", mode)
    with pytest.raises(TokenFileError, match="group or others"):
        load_tokens(p)


def test_load_tokens_refuses_missing_unreadable_and_non_files(tmp_path):
    with pytest.raises(TokenFileError, match="cannot read"):
        load_tokens(tmp_path / "none")
    with pytest.raises(TokenFileError, match="regular file"):
        load_tokens(tmp_path)
    p = write_tokens(tmp_path, "a = \xff" + "x" * 40)
    p.write_bytes(b"a = \xff" + b"x" * 40)
    with pytest.raises(TokenFileError, match="UTF-8|ASCII"):
        load_tokens(p)


def _fleet_file(tmp_path):
    f = tmp_path / "fleet.toml"
    f.write_text('[[machines]]\nname = "a"\ntransport = "local"\n')
    return f


@pytest.fixture
def fake_transports(monkeypatch):
    monkeypatch.setattr(mesh_cmd, "transport_factory", lambda m: BoxTransport())


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_mesh_serve_exits_2_for_a_group_or_other_readable_token_file(
    tmp_path, capsys, fake_transports, mode
):
    p = write_tokens(tmp_path, f"a = {TOKEN}\n", mode)
    argv = ["mesh", "serve", "--fleet", str(_fleet_file(tmp_path)), "--listen", "127.0.0.1:0"]
    rc = main([*argv, "--token-file", str(p)])
    cap = capsys.readouterr()
    assert rc == 2
    assert "group or others" in cap.err
    assert TOKEN not in cap.err
    assert TOKEN not in cap.out


def test_mesh_serve_exits_2_for_a_malformed_token_file(tmp_path, capsys, fake_transports):
    p = write_tokens(tmp_path, f"{TOKEN}\n")
    argv = ["mesh", "serve", "--fleet", str(_fleet_file(tmp_path)), "--listen", "127.0.0.1:0"]
    rc = main([*argv, "--token-file", str(p), "--json"])
    cap = capsys.readouterr()
    assert rc == 2
    assert TOKEN not in cap.err
    assert TOKEN not in cap.out


def test_mesh_serve_with_token_file_enables_writes(tmp_path, capsys, fake_transports, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    seen = {}

    def fake_wait():
        srv_url = json.loads(capsys.readouterr().out)["serving"]
        port = int(srv_url.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/boundaries")
        seen["probe"] = json.loads(conn.getresponse().read())
        conn.close()

    monkeypatch.setattr(mesh_cmd, "_wait_for_signal", fake_wait)
    p = write_tokens(tmp_path, f"ops = {TOKEN}\n")
    argv = ["mesh", "serve", "--fleet", str(_fleet_file(tmp_path)), "--listen", "127.0.0.1:0"]
    rc = main([*argv, "--token-file", str(p), "--json"])
    assert rc == 0
    assert seen["probe"] == {"writes_enabled": True}
    assert (tmp_path / "xdg" / "heat-governor" / svc.AUDIT_FILE).exists()
    cap = capsys.readouterr()
    assert TOKEN not in cap.out + cap.err


def test_mesh_serve_refuses_a_wildcard_bind_with_a_token_file(tmp_path, capsys, fake_transports):
    p = write_tokens(tmp_path, f"ops = {TOKEN}\n")
    argv = ["mesh", "serve", "--fleet", str(_fleet_file(tmp_path)), "--listen", "0.0.0.0:0"]
    rc = main([*argv, "--token-file", str(p)])
    assert rc == 1
    assert "specific" in capsys.readouterr().err


def test_start_mesh_without_token_file_is_read_only(tmp_path, fake_transports):
    reader, server = mesh_cmd.start_mesh(load_fleet(_fleet_file(tmp_path)), "127.0.0.1:0")
    try:
        assert server.writes_enabled is False
        assert server.boundaries is None
    finally:
        mesh_cmd.stop_mesh(reader, server)


def test_bad_server_name_is_exit_1(tmp_path, capsys, fake_transports):
    p = write_tokens(tmp_path, f"ops = {TOKEN}\n")
    argv = ["mesh", "serve", "--fleet", str(_fleet_file(tmp_path)), "--listen", "127.0.0.1:0"]
    rc = main([*argv, "--token-file", str(p), "--server-name", "evil:80"])
    assert rc == 1
    assert "server-name" in capsys.readouterr().err


def test_start_mesh_with_tokens_serves_boundaries_through_the_readers_transport(
    tmp_path, fake_transports
):
    fleet = load_fleet(_fleet_file(tmp_path))
    audit = tmp_path / "audit" / "a.jsonl"
    reader, server = mesh_cmd.start_mesh(
        fleet, "127.0.0.1:0", tokens={"ops": TOKEN}, audit_path=audit
    )
    try:
        reader.poll_once("a")
        assert server.writes_enabled
        assert oct(audit.stat().st_mode & 0o777) == "0o600"
        conn = http.client.HTTPConnection("127.0.0.1", server.address[1], timeout=5)
        conn.request("GET", "/api/boundaries/a", headers={"Authorization": f"Bearer {TOKEN}"})
        resp = conn.getresponse()
        doc = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert doc["outcome"] == "ok"
        assert reader.transport("a").calls[-1] == "list"
    finally:
        mesh_cmd.stop_mesh(reader, server)

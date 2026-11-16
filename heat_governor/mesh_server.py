"""``mesh serve`` HTTP server: the dashboard page and its JSON API on one origin.

``MeshServer(reader, host="127.0.0.1", port=9480, *, tokens=None, boundaries=None,
server_names=(), clock=time.monotonic)`` wraps a stdlib ``ThreadingHTTPServer`` (the same
pattern as ``state.StateServer``; one thread per request, so a slow request never blocks the
others). ``reader`` is any object with ``snapshot()`` and ``trend(name, since_s, max_points)``
returning ``{"source", "samples"}`` (normally a ``FleetReader``). ``start()`` serves on a daemon
thread, ``stop()`` shuts down, ``address`` is the bound ``(host, port)``; port 0 picks an
ephemeral port.

Read routes (GET, and HEAD), unauthenticated: ``/`` the page from
``heat_governor/web/index.html``; ``/vendor/<file>`` an allow-listed plain filename from
``web/vendor/``; ``/api/fleet``; ``/api/history?machine=<name>&range=15m|1h|6h|24h|7d`` (default
1h), which answers ``{"machine", "range", "source": "records"|"buffer", "samples"}``.
``/api/history`` allows at most 4 concurrent reads and answers 503 with ``Retry-After`` beyond
that. Any other path is 404; POST, PUT, DELETE and PATCH on them are 405. The page and API share
one origin, so no CORS headers are sent. The default bind is loopback; only an explicit ``host``
changes it, and binding elsewhere with ``--listen`` exposes the read routes to anyone who can
reach that address.

Boundaries routes (the only write path; see :class:`~heat_governor.boundaries_service.
BoundariesService`, which owns request validation, the version gate and the audit log):

* ``GET|HEAD /api/boundaries`` -- no token: ``{"writes_enabled": bool}``.
* ``GET /api/boundaries/MACHINE`` -- the machine's rules (source, editable), version, file error.
* ``POST /api/boundaries/MACHINE/preview`` -- body ``{"rule": {...}}``.
* ``POST /api/boundaries/MACHINE`` -- apply ``{"operation", "rule", "allow_kill"}``; the base
  version comes from ``If-Match`` (authoritative; a body ``base_version`` must agree).

Writes are enabled only when ``tokens`` (label -> token, from ``mesh serve --token-file``) is
non-empty; otherwise every boundaries route but the probe answers 403 and the server is as
read-only as before. Checks, in order: route and method (404 / 405), writes enabled (403), the
``Host`` header is exactly one of :func:`allowed_hosts` (403), an ``Origin`` header, when sent,
is ``http://`` plus that Host and ``Sec-Fetch-Site``, when sent, is ``same-origin`` or ``none``
(403; a request without either is a non-browser client and is allowed), for POST a JSON
``Content-Type`` (415), a ``Content-Length`` (411) of at most :data:`MAX_BODY_BYTES` (413, from
the header, before reading) and no ``Transfer-Encoding`` (400), the failed-auth rate limit
(429), then ``Authorization: Bearer TOKEN`` compared with every token by
``hmac.compare_digest`` (401). The token is read only from that header, never from the query
string, a cookie or the body, and nothing logs it. Every boundaries response is JSON with
``Cache-Control: no-store`` and an ``outcome`` (refusals add ``error``); its status comes from
:func:`~heat_governor.boundaries_service.http_status`. At most
:data:`MAX_BOUNDARIES_CONCURRENCY` boundaries calls run at once (503 beyond that).
"""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import stat
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

JSON_TYPE = "application/json"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9480
RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}
MAX_POINTS = 300
MAX_HISTORY_CONCURRENCY = 4
_VENDOR_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_VENDOR_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
_NOT_FOUND = {"error": "not found"}

# -- boundaries: limits and formats
MAX_BODY_BYTES = 16 * 1024  # a rule is well under 1 KiB
BODY_READ_TIMEOUT_S = 10.0
DRAIN_TIMEOUT_S = 1.0
MAX_BOUNDARIES_CONCURRENCY = 4
AUTH_FAILURES_MAX = 10  # failed authentications per window, per client address
AUTH_WINDOW_S = 60.0
TOKEN_MIN_LEN = 32
TOKEN_FILE_MAX_BYTES = 64 * 1024
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})  # nosec B104 - refused, not bound
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{%d,512}$" % TOKEN_MIN_LEN)
_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")  # fleet.py's machine names
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_ETAG_RE = re.compile(r'^"([\x21\x23-\x7e]{1,256})"$')
_BARE_TAG_RE = re.compile(r"^[\x21\x23-\x2b\x2d-\x7e]{1,256}$")
_LENGTH_RE = re.compile(r"^[0-9]{1,12}$")
_PROBE, _LIST, _PREVIEW = "probe", "list", "preview"
_ALLOW = {_PROBE: ("GET", "HEAD"), _LIST: ("GET", "POST"), _PREVIEW: ("POST",)}
_PREFIX = "/api/boundaries"
_WRITE_METHODS = ("POST", "PUT", "DELETE", "PATCH")
_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'"
)


# -- the token file -------------------------------------------------------------------------


class TokenFileError(ValueError):
    """The token file cannot be used; the message names the problem, never a token."""


def _read_token_file(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise TokenFileError(f"cannot read token file {path}: {exc.strerror or exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TokenFileError(f"token file {path} is not a regular file")
        if st.st_mode & 0o077:
            raise TokenFileError(
                f"token file {path} is open to group or others (mode "
                f"{stat.S_IMODE(st.st_mode):o}); run chmod 600 on it"
            )
        if st.st_uid != os.getuid():
            raise TokenFileError(f"token file {path} is not owned by the user running mesh serve")
        raw = os.read(fd, TOKEN_FILE_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > TOKEN_FILE_MAX_BYTES:
        raise TokenFileError(f"token file {path} is over {TOKEN_FILE_MAX_BYTES} bytes")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TokenFileError(f"token file {path} is not ASCII (UTF-8 tokens are refused)") from exc


def _token_line(path: Path, n: int, line: str) -> tuple[str, str]:
    label, sep, token = line.partition("=")
    label, token = label.strip(), token.strip()
    where = f"token file {path} line {n}"
    if not sep:
        raise TokenFileError(f"{where} is not 'label = token'")
    if not _LABEL_RE.match(label):
        raise TokenFileError(f"{where}: the label must be letters, digits, '-', '_' or '.'")
    if any(c.isspace() for c in token):
        raise TokenFileError(f"{where} ({label}): the token contains whitespace")
    if not _TOKEN_RE.match(token):
        raise TokenFileError(
            f"{where} ({label}): the token must be {TOKEN_MIN_LEN} to 512 printable characters"
        )
    return label, token


def load_tokens(path: Path | str) -> dict[str, str]:
    """``{label: token}`` from lines of ``label = token`` (``#`` comments and blank lines
    allowed). :class:`TokenFileError` for a missing, unreadable, non-regular, foreign-owned,
    group- or other-accessible, oversized, non-ASCII, empty or malformed file, a duplicate label
    or a token used twice. Read once, at ``mesh serve`` start: a restart rotates tokens."""
    path = Path(path)
    tokens: dict[str, str] = {}
    for n, line in enumerate(_read_token_file(path).splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        label, token = _token_line(path, n, line)
        if label in tokens:
            raise TokenFileError(f"token file {path} line {n}: duplicate label {label!r}")
        if token in tokens.values():
            raise TokenFileError(f"token file {path} line {n} ({label}): the same token twice")
        tokens[label] = token
    if not tokens:
        raise TokenFileError(f"token file {path} has no tokens (expected 'label = token' lines)")
    return tokens


def match_token(tokens: dict[str, str], given: str) -> str | None:
    """The label of the token equal to ``given``, else None. Every configured token is compared
    with ``hmac.compare_digest``, with no early exit on a match or a mismatch."""
    found = None
    raw = given.encode("ascii", "replace")
    for label, token in tokens.items():
        if hmac.compare_digest(raw, token.encode("ascii")) and found is None:
            found = label
    return found


def _bearer(values: list[str] | None) -> str | None:
    """The token of exactly one ``Authorization: Bearer TOKEN`` header, else None."""
    if not values or len(values) != 1:
        return None
    scheme, sep, token = values[0].strip().partition(" ")
    if scheme.lower() != "bearer" or not sep or not _TOKEN_RE.match(token):
        return None
    return token


def _host_form(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def allowed_hosts(host: str, port: int, names: tuple[str, ...] | list[str] = ()) -> set[str]:
    """The ``Host`` header values a boundaries request may carry, lowercased: the bound address
    and every configured name, each as ``NAME:PORT`` (and bare ``NAME`` when the port is 80),
    plus ``localhost`` for a loopback bind. Nothing else: a DNS-rebinding page's own name, a
    suffix or prefix trick, userinfo or another port never matches."""
    hosts = {host.lower(), *(n.lower() for n in names)}
    if host == "::1" or host.startswith("127."):
        hosts.add("localhost")
    out = set()
    for h in hosts:
        out.add(f"{_host_form(h)}:{port}")
        if port == 80:
            out.add(_host_form(h))
    return out


class AuthLimiter:
    """At most ``max_failures`` failed authentications per ``window_s`` per client address;
    past that, that address gets 429 on every authenticated route (even with the right token)
    until its window slides. Other addresses are not affected, so one peer sending bad tokens
    cannot lock the operator out. Tokens are long random strings, so this is defence in depth,
    not the guard itself."""

    MAX_CLIENTS = 1024  # addresses remembered; the oldest is dropped past this

    def __init__(
        self,
        max_failures: int = AUTH_FAILURES_MAX,
        window_s: float = AUTH_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._clock = clock
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, client: str = "") -> int:
        """Seconds ``client`` must wait (at least 1) when over the limit, else 0."""
        with self._lock:
            fails = self._fails.get(client)
            if not fails:
                return 0
            now = self._clock()
            while fails and fails[0] <= now - self.window_s:
                fails.popleft()
            if not fails:
                del self._fails[client]
                return 0
            if len(fails) < self.max_failures:
                return 0
            return max(1, math.ceil(fails[0] + self.window_s - now))

    def fail(self, client: str = "") -> None:
        with self._lock:
            if client not in self._fails and len(self._fails) >= self.MAX_CLIENTS:
                del self._fails[next(iter(self._fails))]
            self._fails.setdefault(client, deque(maxlen=self.max_failures)).append(self._clock())


class _Refusal(Exception):
    def __init__(self, code: int, outcome: str, error: str, headers: dict | None = None):
        super().__init__(outcome)
        self.code, self.outcome, self.error = code, outcome, error
        self.headers = headers or {}


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def _web_file(name: str, vendor: bool = False) -> bytes | None:
    root = resources.files("heat_governor").joinpath("web")
    if vendor:
        root = root.joinpath("vendor")
    entry = root.joinpath(name)
    if not entry.is_file():
        return None
    return entry.read_bytes()


def _is_boundaries(path: str) -> bool:
    return path == _PREFIX or path.startswith(_PREFIX + "/")


def _boundaries_route(path: str) -> tuple[str | None, str]:
    """``(kind, machine)`` for a boundaries path, ``(None, "")`` when it names no route. The
    machine segment is matched raw (not percent-decoded) against the fleet's name pattern."""
    if path == _PREFIX:
        return _PROBE, ""
    parts = path[len(_PREFIX) + 1 :].split("/")
    if not _MACHINE_RE.match(parts[0]):
        return None, ""
    if len(parts) == 1:
        return _LIST, parts[0]
    if len(parts) == 2 and parts[1] == "preview":
        return _PREVIEW, parts[0]
    return None, ""


class _Handler(BaseHTTPRequestHandler):
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
            JSON_TYPE,
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
            return self._json(200, self.server.mesh.reader.snapshot())
        if path == "/api/history":
            return self._history(parse_qs(parts.query))
        if _is_boundaries(path):
            return self._boundaries(path)
        return self._json(404, _NOT_FOUND)

    def _busy(self) -> None:
        self._send(
            503,
            b'{"error": "busy, retry shortly"}',
            JSON_TYPE,
            {"Cache-Control": "no-store", "Retry-After": "1"},
        )

    def _history(self, query: dict) -> None:
        name = (query.get("machine") or [""])[0]
        rng = (query.get("range") or ["1h"])[0]
        if not name:
            return self._json(400, {"error": "machine is required"})
        if rng not in RANGES:
            return self._json(400, {"error": f"range must be one of {', '.join(RANGES)}"})
        sem = self.server.mesh.history_slots
        if not sem.acquire(blocking=False):
            return self._busy()
        try:
            got = self.server.mesh.reader.trend(name, since_s=RANGES[rng], max_points=MAX_POINTS)
        except KeyError:
            return self._json(404, {"error": f"unknown machine: {name}"})
        finally:
            sem.release()
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
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"error": "internal error"})

    def _not_allowed(self) -> None:
        self._send(
            405,
            b'{"error":"method not allowed"}',
            JSON_TYPE,
            {"Allow": "GET, HEAD"},
        )

    # -- boundaries --------------------------------------------------------------------------

    def _bjson(self, code: int, doc: object, extra: dict | None = None) -> None:
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        self._send(code, json.dumps(doc).encode(), JSON_TYPE, {**headers, **(extra or {})})

    def _refuse(self, r: _Refusal) -> None:
        self._drain()
        self._bjson(r.code, {"outcome": r.outcome, "error": r.error}, r.headers)

    def _drain(self) -> None:
        """Read and drop a refused request's unread body when it is small and well framed, so
        closing the socket does not reset the connection before the client reads the answer.
        An over-cap, unframed or chunked body is never read."""
        if getattr(self, "_body_taken", False) or self.command not in _WRITE_METHODS:
            return
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
            return
        value = lengths[0].strip()
        if not _LENGTH_RE.match(value) or int(value) > MAX_BODY_BYTES:
            return
        self._body_taken = True
        try:
            self.connection.settimeout(DRAIN_TIMEOUT_S)
            self.rfile.read(int(value))
        except OSError:
            pass  # a client that stops sending only loses its own answer

    def _boundaries(self, path: str) -> None:
        kind, machine = _boundaries_route(path)
        if kind is None:
            return self._refuse(_Refusal(404, "not_found", "not found"))
        if self.command not in _ALLOW[kind]:
            allow = {"Allow": ", ".join(_ALLOW[kind])}
            return self._refuse(_Refusal(405, "method_not_allowed", "method not allowed", allow))
        try:
            self._boundaries_checked(kind, machine)
        except _Refusal as r:
            self._refuse(r)

    def _boundaries_checked(self, kind: str, machine: str) -> None:
        mesh = self.server.mesh
        if kind == _PROBE:
            if mesh.writes_enabled:
                self._check_host_origin()
            return self._bjson(200, {"writes_enabled": mesh.writes_enabled})
        if not mesh.writes_enabled:
            msg = "boundary writes are not enabled on this server (start it with --token-file)"
            raise _Refusal(403, "writes_disabled", msg)
        self._check_host_origin()
        length = self._check_body_headers() if self.command == "POST" else 0
        label = self._authenticate()
        if kind == _PREVIEW:
            return self._preview(machine, length)
        if self.command == "POST":
            return self._apply(machine, length, label)
        return self._answer(self._call(mesh.boundaries.list, machine))

    def _check_host_origin(self) -> None:
        mesh = self.server.mesh
        hosts = self.headers.get_all("Host") or []
        host = hosts[0].strip().lower() if len(hosts) == 1 else ""
        if host not in mesh.hosts:
            raise _Refusal(403, "host_not_allowed", "the Host header is not this server's address")
        origins = self.headers.get_all("Origin") or []
        if len(origins) > 1 or (origins and origins[0].strip().lower() != f"http://{host}"):
            raise _Refusal(403, "origin_not_allowed", "cross-origin requests are refused")
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site.strip().lower() not in ("same-origin", "none"):
            raise _Refusal(403, "origin_not_allowed", "cross-site requests are refused")

    def _check_body_headers(self) -> int:
        media, _, params = (self.headers.get("Content-Type") or "").partition(";")
        charset = params.strip().lower().replace(" ", "")
        if media.strip().lower() != JSON_TYPE or charset not in ("", "charset=utf-8"):
            raise _Refusal(415, "unsupported_media_type", "Content-Type must be application/json")
        if self.headers.get("Transfer-Encoding") is not None:
            raise _Refusal(400, "bad_length", "chunked bodies are refused; send Content-Length")
        lengths = self.headers.get_all("Content-Length") or []
        if not lengths:
            raise _Refusal(411, "length_required", "Content-Length is required")
        if len(lengths) != 1 or not _LENGTH_RE.match(lengths[0].strip()):
            raise _Refusal(400, "bad_length", "Content-Length must be one non-negative integer")
        length = int(lengths[0].strip())
        if length > MAX_BODY_BYTES:
            raise _Refusal(413, "too_large", f"the body is over {MAX_BODY_BYTES} bytes")
        return length

    def _authenticate(self) -> str:
        mesh = self.server.mesh
        client = str(self.client_address[0])
        wait = mesh.limiter.blocked(client)
        if wait:
            msg = "too many failed authentications; retry later"
            raise _Refusal(429, "rate_limited", msg, {"Retry-After": str(wait)})
        given = _bearer(self.headers.get_all("Authorization"))
        label = match_token(mesh.tokens, given) if given else None
        if label is None:
            mesh.limiter.fail(client)
            auth = {"WWW-Authenticate": 'Bearer realm="heat-governor"'}
            raise _Refusal(401, "unauthorized", "a valid bearer token is required", auth)
        return label

    def _read_json(self, length: int) -> dict:
        self.close_connection = True
        self._body_taken = True
        try:
            self.connection.settimeout(BODY_READ_TIMEOUT_S)
            raw = self.rfile.read(length)
        except OSError as exc:  # includes the socket timeout
            raise _Refusal(400, "invalid", "the request body did not arrive") from exc
        if len(raw) != length:
            raise _Refusal(400, "invalid", "the request body is shorter than Content-Length")
        try:
            doc = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
        except (ValueError, RecursionError) as exc:  # UnicodeDecodeError is a ValueError
            raise _Refusal(400, "invalid", "the body is not valid JSON") from exc
        if not isinstance(doc, dict):
            raise _Refusal(400, "invalid", "the body must be a JSON object")
        return doc

    def _if_match(self) -> str:
        values = self.headers.get_all("If-Match") or []
        if not values:
            msg = "If-Match with the version from GET /api/boundaries/MACHINE is required"
            raise _Refusal(428, "precondition_required", msg)
        value = values[0].strip() if len(values) == 1 else ""
        m = _ETAG_RE.match(value)
        if m:
            return m.group(1)
        if _BARE_TAG_RE.match(value) and value != "*":
            return value
        raise _Refusal(400, "invalid", 'If-Match must be one version, as "VERSION"')

    def _preview(self, machine: str, length: int) -> None:
        doc = self._read_json(length)
        extra = sorted(set(doc) - {"rule"})
        if extra:
            problems = [f"unknown preview key {k!r}" for k in extra]
            return self._answer({"outcome": "invalid", "problems": problems})
        self._answer(self._call(self.server.mesh.boundaries.preview, machine, doc.get("rule")))

    def _apply(self, machine: str, length: int, label: str) -> None:
        version = self._if_match()
        doc = self._read_json(length)
        if "base_version" in doc and doc["base_version"] != version:
            problems = ["base_version in the body disagrees with If-Match"]
            return self._answer({"outcome": "invalid", "problems": problems})
        request = {**doc, "base_version": version}
        self._answer(self._call(self.server.mesh.boundaries.apply, machine, request, label))

    def _call(self, fn: Callable[..., dict], *args: Any) -> dict:
        slots = self.server.mesh.boundaries_slots
        if not slots.acquire(blocking=False):
            raise _Refusal(503, "busy", "busy, retry shortly", {"Retry-After": "1"})
        try:
            return fn(*args)
        finally:
            slots.release()

    def _answer(self, result: dict) -> None:
        from heat_governor.boundaries_service import http_status

        version = result.get("version")
        extra = {}
        if isinstance(version, str) and _BARE_TAG_RE.match(version):
            extra["ETag"] = f'"{version}"'
        self._bjson(http_status(result), result, extra)

    def _write_method(self) -> None:
        if _is_boundaries(urlsplit(self.path).path):
            return self._guarded()
        return self._not_allowed()

    do_GET = do_HEAD = _guarded  # noqa: N815 - http.server API
    do_POST = do_PUT = do_DELETE = do_PATCH = _write_method  # noqa: N815

    def log_message(self, *_args: object) -> None:
        return


class MeshServer:
    def __init__(
        self,
        reader: Any,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        tokens: dict[str, str] | None = None,
        boundaries: Any = None,
        server_names: tuple[str, ...] | list[str] = (),
        clock: Callable[[], float] = time.monotonic,
    ):
        """``tokens`` (label -> token) enables the boundaries routes; ``boundaries`` is the
        service they call (default: a ``BoundariesService`` over ``reader.snapshot`` and
        ``reader.transport``). ``ValueError`` when tokens are given with a wildcard ``host``:
        the Host check needs the one address the server answers on."""
        self.reader = reader
        self.tokens = dict(tokens or {})
        if self.tokens and host in WILDCARD_HOSTS:
            raise ValueError("boundary writes need a specific listen address, not a wildcard")
        if self.tokens and boundaries is None:
            from heat_governor.boundaries_service import BoundariesService

            boundaries = BoundariesService(reader.snapshot, reader.transport)
        self.boundaries = boundaries if self.tokens else None
        self.writes_enabled = self.boundaries is not None
        self.limiter = AuthLimiter(clock=clock)
        self.history_slots = threading.BoundedSemaphore(MAX_HISTORY_CONCURRENCY)
        self.boundaries_slots = threading.BoundedSemaphore(MAX_BOUNDARIES_CONCURRENCY)
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.mesh = self  # type: ignore[attr-defined]
        self.address = self.httpd.server_address
        bound = str(self.address[0])
        names = (*server_names, host) if host not in WILDCARD_HOSTS else tuple(server_names)
        self.hosts = allowed_hosts(bound, int(self.address[1]), names)
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

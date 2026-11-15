"""Ship events, the black box and the postmortem to S3-compatible storage (stdlib only).

Three pieces:

* :func:`resolve_credentials` -- ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` first,
  then the named profile (``[ship] profile``, default ``default``) of
  ``~/.aws/credentials``. A world-readable credentials file logs a warning.
* :class:`S3Client` -- path-style PUT / GET / ListObjectsV2 over ``urllib.request``,
  signed with SigV4 (:mod:`heat_governor.sigv4`), the real payload hash always.
  Plain ``http://`` is accepted only for loopback / 100.64.0.0/10 hosts.
* :class:`Shipper` -- a background thread. :meth:`Shipper.submit` writes the object
  to the spool directory and wakes the thread through a bounded queue; it never
  touches the network, so a stalled server can't block the caller. The thread
  uploads spooled objects (oldest first), ships the black box every
  ``blackbox_every_s`` (``blackbox_low_memory_s`` while memory is low) and backs off
  on failure. A spooled object is deleted only after its PUT succeeded, so a crash
  between submit and upload still uploads on the next start (at-least-once).

Object keys (obligation o3)::

    <prefix>/<machine>/<boot_id>/events/<utc>-<kind>.json
    <prefix>/<machine>/<boot_id>/blackbox.jsonl.gz
    <prefix>/<machine>/<boot_id>/postmortem.json

``machine`` comes from configuration or the caller's fleet name, never the hostname.
Nothing here logs or raises with the secret key, an Authorization header or a
signed URL.
"""

from __future__ import annotations

import configparser
import gzip
import hashlib
import html
import json
import os
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode, urlsplit

from heat_governor import recorder, sigv4
from heat_governor.config import endpoint_problem

Log = Callable[[str], None]

DEFAULT_PROFILE = "default"
SPOOL_SUFFIX = ".spool"
SPOOL_VERSION = 1
HOUR_S = 3600.0
# 4xx codes that are the operator's to fix: retried slowly, logged once an hour.
CONFIG_CODES = {"NoSuchBucket", "InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied"}
ALWAYS_RETRY_CODES = {"RequestTimeTooSkewed", "RequestTimeout", "SlowDown", "Timeout", "Connection"}


def _quiet(_msg: str) -> None:
    pass


# --- credentials -------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    source: str = "explicit"

    def __str__(self) -> str:
        return f"Credentials({self.access_key}, from {self.source})"


def resolve_credentials(
    profile: str | None = None,
    *,
    environ: dict | None = None,
    home: Path | str | None = None,
    log: Log | None = None,
) -> Credentials | None:
    """Env vars first, then ``profile`` (default ``default``) of the shared credentials
    file (``AWS_SHARED_CREDENTIALS_FILE`` or ``<home>/.aws/credentials``). None if neither."""
    env = os.environ if environ is None else environ
    log = log or _quiet
    ak, sk = env.get("AWS_ACCESS_KEY_ID"), env.get("AWS_SECRET_ACCESS_KEY")
    if ak and sk:
        return Credentials(ak, sk, env.get("AWS_SESSION_TOKEN") or None, "env")
    return _profile_credentials(_credentials_path(env, home), profile or DEFAULT_PROFILE, log)


def _credentials_path(env, home: Path | str | None) -> Path:
    """``AWS_SHARED_CREDENTIALS_FILE``, else ``<home>/.aws/credentials``."""
    if env.get("AWS_SHARED_CREDENTIALS_FILE"):
        return Path(env["AWS_SHARED_CREDENTIALS_FILE"])
    base = Path(home) if home is not None else Path(env.get("HOME") or Path.home())
    return base / ".aws" / "credentials"


def _profile_credentials(path: Path, name: str, log: Log) -> Credentials | None:
    """Profile ``name`` of the shared credentials file at ``path``, or None."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & 0o004:
        log(f"ship: warning: {path} is world-readable; chmod 600 it")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        log(f"ship: cannot parse {path}")
        return None
    if not parser.has_section(name):
        log(f"ship: no [{name}] profile in {path}")
        return None
    sec = parser[name]
    ak, sk = sec.get("aws_access_key_id"), sec.get("aws_secret_access_key")
    if not ak or not sk:
        log(f"ship: profile [{name}] in {path} lacks an access key id or secret")
        return None
    return Credentials(
        ak.strip(), sk.strip(), (sec.get("aws_session_token") or None), f"profile:{name}"
    )


# --- S3 client ---------------------------------------------------------------


class ShipError(Exception):
    """An S3 request failed. ``status`` is the HTTP status (None for a timeout or a
    connection error); ``code`` the S3 error code. Never carries credentials."""

    def __init__(self, status: int | None, code: str, message: str = "") -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status or '-'} {code}" + (f": {message}" if message else ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would resend the signed headers to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_CODE_RE = re.compile(r"<Code>([^<]{1,100})</Code>")
_KEY_RE = re.compile(r"<Key>([^<]*)</Key>")
_TOKEN_RE = re.compile(r"<NextContinuationToken>([^<]*)</NextContinuationToken>")
_TRUNC_RE = re.compile(r"<IsTruncated>\s*true\s*</IsTruncated>", re.I)


class S3Client:
    def __init__(
        self,
        endpoint: str,
        region: str,
        bucket: str,
        credentials: Credentials,
        *,
        timeout: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        why = endpoint_problem(endpoint)
        if why:
            raise ValueError(f"ship endpoint {why}")
        if not bucket:
            raise ValueError("ship bucket is required")
        parts = urlsplit(endpoint)
        self.scheme = parts.scheme
        self.netloc = parts.netloc
        self.base_path = parts.path.rstrip("/")
        self.region = region
        self.bucket = bucket
        self.timeout = timeout
        self._creds = credentials
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._skew = timedelta(0)  # server clock minus ours, learnt from RequestTimeTooSkewed
        handlers: list = [_NoRedirect()]
        if self.scheme == "http":  # NOSONAR python:S5332 -- loopback/tailnet only
            handlers.append(urllib.request.ProxyHandler({}))  # loopback/tailnet: no proxy
        self._opener = urllib.request.build_opener(*handlers)

    def __repr__(self) -> str:
        return f"S3Client({self.scheme}://{self.netloc}, bucket={self.bucket})"

    def _url(self, key: str = "", query: dict | None = None) -> str:
        path = f"{self.base_path}/{quote(self.bucket, safe='')}"
        if key:
            path += "/" + quote(key, safe="/-_.~")
        qs = ("?" + urlencode(sorted(query.items()))) if query else ""
        return f"{self.scheme}://{self.netloc}{path}{qs}"

    def _request(
        self, method: str, url: str, body: bytes = b"", headers: dict | None = None
    ) -> bytes:
        signed = self._sign(method, url, body, headers)
        # the scheme is http(s) only: endpoint_problem() was checked in __init__
        req = urllib.request.Request(  # nosec B310
            url, data=body if method == "PUT" else None, headers=signed, method=method
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:  # nosec B310
                return resp.read()
        except urllib.error.HTTPError as err:
            raise self._http_error(err) from None
        except (socket.timeout, TimeoutError):
            raise ShipError(None, "Timeout", f"no answer in {self.timeout:g} s") from None
        except urllib.error.URLError as err:
            if isinstance(err.reason, (socket.timeout, TimeoutError)):
                raise ShipError(None, "Timeout", f"no answer in {self.timeout:g} s") from None
            raise ShipError(None, "Connection", type(err.reason).__name__) from None
        except (OSError, ValueError) as err:
            raise ShipError(None, "Connection", type(err).__name__) from None

    def _sign(self, method: str, url: str, body: bytes, headers: dict | None) -> dict:
        """The SigV4-signed headers (plus the session token, when there is one)."""
        hdrs = dict(headers or {})
        if self._creds.session_token:
            hdrs["x-amz-security-token"] = self._creds.session_token
        try:
            return sigv4.sign(
                method,
                url,
                hdrs,
                hashlib.sha256(body).hexdigest(),
                self._creds.access_key,
                self._creds.secret_key,
                self.region,
                now=self._now() + self._skew,
            )
        except sigv4.SigV4Error as err:
            raise ShipError(None, "InvalidRequest", str(err)) from None

    def _http_error(self, err: urllib.error.HTTPError) -> ShipError:
        """The ShipError for an HTTP error answer; learns the clock skew on the way."""
        try:
            text = err.read(4096).decode("utf-8", "replace")
        except Exception:  # pylint: disable=broad-except
            text = ""
        m = _CODE_RE.search(text)
        code = m.group(1) if m else f"HTTP{err.code}"
        if code == "RequestTimeTooSkewed":
            self._learn_skew(err.headers.get("Date") if err.headers else None)
        return ShipError(err.code, code)

    def _learn_skew(self, date_header: str | None) -> None:
        if not date_header:
            return
        try:
            server = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            return
        if server.tzinfo is None:
            server = server.replace(tzinfo=timezone.utc)
        self._skew = server - self._now()

    def put(self, key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
        self._request("PUT", self._url(key), body, {"Content-Type": content_type})

    def get(self, key: str) -> bytes:
        return self._request("GET", self._url(key))

    def list(self, prefix: str = "") -> list[str]:
        """Every key under ``prefix`` (ListObjectsV2, following continuation tokens)."""
        keys: list[str] = []
        cont: str | None = None  # the continuation token of the next page
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if cont:
                query["continuation-token"] = cont
            text = self._request("GET", self._url(query=query)).decode("utf-8", "replace")
            keys.extend(html.unescape(k) for k in _KEY_RE.findall(text))
            m = _TOKEN_RE.search(text)
            if not (_TRUNC_RE.search(text) and m):
                return keys
            cont = html.unescape(m.group(1))


# --- key layout --------------------------------------------------------------

_SEGMENT_BAD = re.compile(r"[^A-Za-z0-9_.:=+@-]")
_KIND_BAD = re.compile(r"[^A-Za-z0-9_.]")


def _segment(value: str) -> str:
    out = _SEGMENT_BAD.sub("_", str(value).strip()).strip(".") or "_"
    return out


def utc_stamp(t: float) -> str:
    """Sortable UTC millisecond stamp: ``20261114T173418.123Z``."""
    ms = int(round(t * 1000))
    dt = datetime.fromtimestamp(ms // 1000, timezone.utc)
    return f"{dt:%Y%m%dT%H%M%S}.{ms % 1000:03d}Z"


def object_root(prefix: str, machine: str, boot_id: str) -> str:
    head = "/".join(_segment(p) for p in str(prefix).strip("/").split("/") if p)
    tail = f"{_segment(machine)}/{_segment(boot_id)}"
    return f"{head}/{tail}" if head else tail


def event_key(prefix: str, machine: str, boot_id: str, kind: str, t: float, seq: int = 0) -> str:
    """``seq`` > 0 disambiguates two events in the same millisecond: ``<utc>-<seq>-<kind>``."""
    kind = _KIND_BAD.sub("_", kind) or "event"
    stamp = utc_stamp(t) + (f"-{seq}" if seq else "")
    return f"{object_root(prefix, machine, boot_id)}/events/{stamp}-{kind}.json"


def blackbox_key(prefix: str, machine: str, boot_id: str) -> str:
    return f"{object_root(prefix, machine, boot_id)}/blackbox.jsonl.gz"


def postmortem_key(prefix: str, machine: str, boot_id: str) -> str:
    return f"{object_root(prefix, machine, boot_id)}/postmortem.json"


# --- spool -------------------------------------------------------------------


def _spool_name(key: str) -> str:
    # one file per key: a newer black box replaces the older spooled one
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + SPOOL_SUFFIX


def write_spool(spool_dir: Path, key: str, body: bytes, content_type: str) -> Path:
    """Atomically write ``<sha256(key)[:32]>.spool``: a JSON header line, then the body."""
    spool_dir.mkdir(parents=True, exist_ok=True)
    path = spool_dir / _spool_name(key)
    header = json.dumps(
        {"v": SPOOL_VERSION, "key": key, "content_type": content_type, "t": time.time()}
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(header.encode("utf-8") + b"\n" + body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_spool(path: Path) -> tuple[str, str, bytes] | None:
    try:
        raw = path.read_bytes()
        head, body = raw.split(b"\n", 1)
        meta = json.loads(head)
        return str(meta["key"]), str(meta.get("content_type") or "application/octet-stream"), body
    except (OSError, ValueError, KeyError, TypeError):
        return None


# --- shipper -----------------------------------------------------------------


@dataclass(frozen=True)
class ShipperTuning:
    """The shipper's cadence, backoff, queue and spool knobs, and its clocks.

    ``log_clock`` (default: ``mono``) times the once-an-hour config-error log line."""

    blackbox_every_s: float = 60.0
    blackbox_minutes: float = 10.0
    blackbox_low_memory_s: float = 10.0
    queue_size: int = 256
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 300.0
    config_retry_s: float = 300.0
    boot_window_s: float = 600.0
    max_spool_files: int = 5000
    wall: Callable[[], float] = time.time
    mono: Callable[[], float] = time.monotonic
    log_clock: Callable[[], float] | None = None


class Shipper:
    def __init__(
        self,
        client: S3Client,
        *,
        machine: str,
        boot_id: str,
        spool_dir: Path | str,
        prefix: str = "fleet",
        log: Log | None = None,
        records_dir: Path | str | None = None,
        low_memory: Callable[[], bool] | None = None,
        tuning: ShipperTuning | None = None,
    ) -> None:
        if not machine or not str(machine).strip():
            raise ValueError("ship needs a machine name (config machine or the fleet name)")
        tune = tuning or ShipperTuning()
        self.tuning = tune
        self.client = client
        self.machine = str(machine).strip()
        self.boot_id = boot_id
        self.prefix = prefix
        self.spool_dir = Path(spool_dir)
        self.records_dir = Path(records_dir) if records_dir is not None else None
        self.blackbox_every_s = tune.blackbox_every_s
        self.blackbox_minutes = tune.blackbox_minutes
        self.blackbox_low_memory_s = tune.blackbox_low_memory_s
        self.low_memory = low_memory or (lambda: False)
        self.backoff_initial_s = tune.backoff_initial_s
        self.backoff_max_s = tune.backoff_max_s
        self.config_retry_s = tune.config_retry_s
        self.boot_window_s = tune.boot_window_s
        self.max_spool_files = tune.max_spool_files
        self._log = log or _quiet
        self._wall = tune.wall
        self._mono = tune.mono
        self._log_clock = tune.log_clock or tune.mono
        self._started_at = tune.mono()
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, tune.queue_size))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._key_lock = threading.Lock()
        self._last_stamp = ""
        self._seq = 0
        self._memory: list[tuple[str, str, bytes]] = []  # spool-write failed: hold in RAM
        self._mem_lock = threading.Lock()
        self._backoff = 0.0
        self._retry_at = 0.0
        self._failing = False
        self._logged_at: dict[str, float] = {}
        self._next_blackbox = 0.0

    @classmethod
    def from_rule(
        cls,
        rule,
        *,
        default_machine: str,
        boot_id: str,
        spool_dir: Path | str,
        records_dir: Path | str | None = None,
        log: Log | None = None,
        environ: dict | None = None,
        home: Path | str | None = None,
        timeout: float = 10.0,
        **kw,
    ) -> "Shipper":
        """Build from a :class:`~heat_governor.config.ShipRule`. ``spool_dir`` is the
        default used when the rule leaves ``spool_dir`` unset. The rule's black-box
        cadence overrides those of a ``tuning`` passed in ``kw``. Raises ValueError when
        the endpoint, bucket or credentials are missing or refused."""
        if not rule.endpoint or not rule.bucket:
            raise ValueError("ship.endpoint and ship.bucket are required")
        creds = resolve_credentials(rule.profile, environ=environ, home=home, log=log)
        if creds is None:
            raise ValueError("no ship credentials (AWS_* env vars or the credentials profile)")
        client = S3Client(rule.endpoint, rule.region, rule.bucket, creds, timeout=timeout)
        tuning = replace(
            kw.pop("tuning", None) or ShipperTuning(),
            blackbox_every_s=rule.blackbox_every_s,
            blackbox_minutes=rule.blackbox_minutes,
        )
        return cls(
            client,
            machine=rule.machine or default_machine,
            boot_id=boot_id,
            spool_dir=Path(rule.spool_dir) if rule.spool_dir else Path(spool_dir),
            prefix=rule.prefix,
            log=log,
            records_dir=records_dir,
            tuning=tuning,
            **kw,
        )

    # -- caller side (never touches the network) -------------------------
    def submit(self, kind: str, record: dict, *, t: float | None = None) -> str | None:
        """Queue one event; returns its object key. Never blocks on the network."""
        t = self._wall() if t is None else t
        with self._key_lock:
            stamp = utc_stamp(t)
            self._seq = self._seq + 1 if stamp == self._last_stamp else 0
            self._last_stamp = stamp
            key = event_key(self.prefix, self.machine, self.boot_id, kind, t, self._seq)
        return self._enqueue(key, self._json(record), "application/json")

    def submit_postmortem(self, record: dict, *, boot_id: str | None = None) -> str | None:
        """Queue the postmortem, keyed under the boot that crashed (what ``ship fetch`` reads).

        The daemon submits from the NEW boot, so the key uses ``boot_id`` when given, else
        ``record["previous_boot"]`` when it is a non-empty string, else the current boot."""
        prev = record.get("previous_boot")
        target = boot_id or (prev if isinstance(prev, str) and prev else None) or self.boot_id
        key = postmortem_key(self.prefix, self.machine, target)
        return self._enqueue(key, self._json(record), "application/json")

    @staticmethod
    def _json(record: dict) -> bytes:
        return json.dumps(record, sort_keys=True, default=str).encode("utf-8")

    def _enqueue(self, key: str, body: bytes, content_type: str) -> str | None:
        try:
            write_spool(self.spool_dir, key, body, content_type)
        except OSError as err:
            self._log(f"ship: cannot spool {key} ({type(err).__name__}); holding in memory")
            with self._mem_lock:
                if len(self._memory) >= self.max_spool_files:
                    self._log("ship: in-memory backlog full; dropping the oldest object")
                    self._memory.pop(0)
                self._memory.append((key, content_type, body))
        try:
            self._queue.put_nowait(key)
        except queue.Full:
            pass  # the object is spooled; the thread drains the whole spool on its next wake
        return key

    def pending(self) -> int:
        with self._mem_lock:
            held = len(self._memory)
        return len(self._spool_paths()) + held

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="heat-governor-ship", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # -- worker ----------------------------------------------------------
    def _blackbox_interval(self) -> float:
        try:
            low = bool(self.low_memory())
        except Exception:  # pylint: disable=broad-except
            low = False
        return (
            min(self.blackbox_every_s, self.blackbox_low_memory_s) if low else self.blackbox_every_s
        )

    def _run(self) -> None:
        self._next_blackbox = self._mono()  # first black box right away
        while not self._stop.is_set():
            now = self._mono()
            self._blackbox_if_due(now)
            self._drain_if_due(now)
            try:
                item = self._queue.get(timeout=max(0.01, self._wait_s()))
            except queue.Empty:
                continue
            if item is None:
                break
            if self.records_dir is not None:  # low memory may have shortened the cadence
                self._next_blackbox = min(
                    self._next_blackbox, self._mono() + self._blackbox_interval()
                )

    def _blackbox_if_due(self, now: float) -> None:
        if self.records_dir is not None:
            due = self._next_blackbox
            if now >= due:
                self._spool_blackbox()
                self._next_blackbox = now + self._blackbox_interval()

    def _drain_if_due(self, now: float) -> None:
        if now >= self._retry_at:
            try:
                self.drain()
            except Exception as err:  # pylint: disable=broad-except
                # never let the worker die; the type only (no message: no secrets)
                self._log(f"ship: unexpected {type(err).__name__} while uploading")
                self._retry_at = self._mono() + self.config_retry_s

    def _wait_s(self) -> float:
        """How long the worker may sleep: until the next black box or the next retry."""
        wait = 3600.0
        if self.records_dir is not None:
            wait = min(wait, max(0.0, self._next_blackbox - self._mono()))
        if self._retry_at > self._mono() and self.pending():
            wait = min(wait, self._retry_at - self._mono())
        return wait

    def blackbox_body(self) -> bytes:
        rows = recorder.tail(self.records_dir, self.blackbox_minutes, clock=self._wall)
        text = "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows)
        return gzip.compress(text.encode("utf-8"), mtime=0)

    def _spool_blackbox(self) -> str | None:
        if self.records_dir is None:
            return None
        try:
            body = self.blackbox_body()
        except OSError as err:
            self._log(f"ship: cannot read records for the black box ({type(err).__name__})")
            return None
        key = blackbox_key(self.prefix, self.machine, self.boot_id)
        return self._enqueue(key, body, "application/gzip")

    def ship_blackbox(self) -> str | None:
        """Spool the black box now; the worker uploads it (or :meth:`drain` does)."""
        return self._spool_blackbox()

    def _spool_paths(self) -> list[Path]:
        try:
            entries = [p for p in self.spool_dir.iterdir() if p.name.endswith(SPOOL_SUFFIX)]
        except OSError:
            return []
        out = []
        for p in entries:
            try:
                out.append((p.stat().st_mtime_ns, p.name, p))
            except OSError:
                continue
        return [p for _, _, p in sorted(out)]

    def _trim_spool(self, paths: list[Path]) -> list[Path]:
        extra = len(paths) - self.max_spool_files
        if extra <= 0:
            return paths
        self._log(f"ship: spool over {self.max_spool_files} objects; dropping the {extra} oldest")
        for p in paths[:extra]:
            try:
                p.unlink()
            except OSError:
                pass
        return paths[extra:]

    def drain(self) -> int:
        """Upload what is held in memory and spooled, oldest first; stop at the first
        failure (backing off). Returns how many objects were uploaded."""
        done, ok = self._drain_memory()
        if not ok:
            return done
        for path in self._trim_spool(self._spool_paths()):
            if self._stop.is_set():
                break
            sent = self._drain_spooled(path)
            if sent is None:
                continue
            if not sent:
                return done
            done += 1
        if done and self._failing:
            self._log(f"ship: uploads recovered ({done} sent)")
        if done:
            self._failing = False
        return done

    def _drain_memory(self) -> tuple[int, bool]:
        """Upload the objects held in memory: (how many sent, whether all of them were)."""
        done = 0
        with self._mem_lock:
            held = list(self._memory)
        for item in held:
            if not self._upload(*item):
                return done, False
            with self._mem_lock:
                if item in self._memory:
                    self._memory.remove(item)
            done += 1
        return done, True

    def _drain_spooled(self, path: Path) -> bool | None:
        """Upload one spool file: True sent, False failed (stop draining), None skipped
        (already gone, or unreadable and dropped)."""
        try:
            before = path.stat()
        except OSError:
            return None  # already uploaded
        parsed = read_spool(path)
        if parsed is None:
            self._log(f"ship: dropping unreadable spool file {path.name}")
            self._unlink(path)
            return None
        if not self._upload(*parsed):
            return False
        try:
            after = path.stat()
            if (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns):
                path.unlink()  # unchanged since read: a newer version would have a new inode
        except OSError:
            pass
        return True

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def classify(self, err: ShipError) -> str:
        """``retry`` (transient: exponential backoff) or ``config`` (the operator's to fix:
        slow retry, logged once an hour)."""
        if err.code == "InvalidRequest" and err.status is None:
            return "config"  # could not even sign (e.g. an empty key): the operator's to fix
        if err.status is None or err.code in ALWAYS_RETRY_CODES:
            return "retry"
        if err.status >= 500 or err.status in (408, 429):
            return "retry"
        if err.status == 403 and self._mono() - self._started_at < self.boot_window_s:
            return "retry"  # clock / IAM not settled yet just after start
        return "config"

    def _upload(self, key: str, content_type: str, body: bytes) -> bool:
        try:
            self.client.put(key, body, content_type)
        except ShipError as err:
            self._failed(err)
            return False
        self._backoff = 0.0
        self._retry_at = 0.0
        return True

    def _failed(self, err: ShipError) -> None:
        kind = self.classify(err)
        now = self._mono()
        if kind == "retry":
            self._backoff = (
                self.backoff_initial_s
                if self._backoff <= 0
                else min(self._backoff * 2, self.backoff_max_s)
            )
            self._retry_at = now + self._backoff
            if not self._failing:
                self._log(f"ship: upload failed ({err}); spooled, retrying with backoff")
        else:
            self._retry_at = now + self.config_retry_s
            stamp = self._log_clock()
            last = self._logged_at.get(err.code)
            if last is None or stamp - last >= HOUR_S:
                self._logged_at[err.code] = stamp
                self._log(
                    f"ship: upload refused ({err}); check ship.endpoint/bucket/credentials. "
                    f"Spool kept; retrying every {self.config_retry_s:g} s, "
                    "this message at most once an hour"
                )
        self._failing = True

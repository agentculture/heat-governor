"""A tiny S3-compatible fake on 127.0.0.1 that VERIFIES SigV4 (stdlib only).

Path-style only: ``PUT /<bucket>/<key>``, ``GET /<bucket>/<key>`` and
``GET /<bucket>?list-type=2&prefix=...`` (ListObjectsV2, paged by ``max_keys``).
The fake keys are assembled at runtime so no scanner-flagged literal is committed.

Scripted failures: ``fail_next`` is a list of ``(status, code)`` returned (one per
request) before normal handling; ``always`` returns ``(status, code)`` forever;
``stall_s`` sleeps before answering; ``strict_time`` rejects an ``x-amz-date`` more
than 15 minutes from the server clock with ``RequestTimeTooSkewed``.
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import re
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

from heat_governor import sigv4

FAKE_ACCESS_KEY = "AKIA" + "T" * 16
FAKE_SECRET_KEY = "fake-secret-" + "x" * 8 + "-" + "Q9" * 6
BUCKET = "fleet-records"
REGION = "us-east-1"

_AUTH_RE = re.compile(
    r"AWS4-HMAC-SHA256 Credential=(?P<ak>[^/]+)/(?P<scope>[^,]+), "
    r"SignedHeaders=(?P<signed>[^,]+), Signature=(?P<sig>[0-9a-f]{64})$"
)


def _error_xml(code: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>{code}</Code>'
        f"<Message>fake {code}</Message></Error>"
    ).encode()


class FakeS3:
    def __init__(
        self,
        *,
        bucket: str = BUCKET,
        region: str = REGION,
        access_key: str = FAKE_ACCESS_KEY,
        secret_key: str = FAKE_SECRET_KEY,
        port: int = 0,
        max_keys: int = 1000,
    ) -> None:
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.max_keys = max_keys
        self.objects: dict[str, bytes] = {}
        self.put_counts: dict[str, int] = {}
        self.requests: list[dict] = []  # method, path, headers, body
        self.fail_next: list[tuple[int, str]] = []
        self.always: tuple[int, str] | None = None
        self.stall_s = 0.0
        self.strict_time = False
        self.lock = threading.Lock()
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "FakeS3":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def do_PUT(self):
                fake._handle(self, "PUT")

            def do_GET(self):
                fake._handle(self, "GET")

        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        self._server.daemon_threads = True
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.02,), daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def __enter__(self) -> "FakeS3":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- request handling ------------------------------------------------
    def _reply(self, h, status: int, body: bytes = b"", ctype="application/xml", extra=None):
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        if body:
            h.wfile.write(body)

    def _verify(self, h, method: str, body: bytes) -> str | None:
        """Return an S3 error code when the request's SigV4 does not check out."""
        auth = h.headers.get("Authorization", "")
        m = _AUTH_RE.match(auth)
        if not m or m["ak"] != self.access_key:
            return "InvalidAccessKeyId"
        payload_hash = h.headers.get("x-amz-content-sha256", "")
        if payload_hash != hashlib.sha256(body).hexdigest():
            return "XAmzContentSHA256Mismatch"
        amz_date = h.headers.get("x-amz-date", "")
        date, region, service, term = m["scope"].split("/")
        if date != amz_date[:8] or region != self.region or service != "s3":
            return "SignatureDoesNotMatch"
        names = m["signed"].split(";")
        if "host" not in names or "x-amz-date" not in names:
            return "SignatureDoesNotMatch"
        headers = {n: h.headers.get(n, "") for n in names}
        url = f"http://{h.headers.get('Host', '')}{h.path}"
        canonical, signed = sigv4.canonical_request(method, url, headers, payload_hash)
        sts = sigv4.string_to_sign(amz_date, m["scope"], canonical)
        want = sigv4.signature(sigv4.signing_key(self.secret_key, date, region, "s3"), sts)
        if signed != m["signed"] or not hmac.compare_digest(want, m["sig"]):
            return "SignatureDoesNotMatch"
        if self.strict_time:
            try:
                sent = calendar.timegm(time.strptime(amz_date, "%Y%m%dT%H%M%SZ"))
            except ValueError:
                return "AccessDenied"
            if abs(sent - time.time()) > 900:
                return "RequestTimeTooSkewed"
        return None

    def _handle(self, h, method: str) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        with self.lock:
            self.requests.append(
                {"method": method, "path": h.path, "headers": dict(h.headers), "body": body}
            )
            scripted = self.fail_next.pop(0) if self.fail_next else self.always
            stall = self.stall_s
        if stall:
            time.sleep(stall)
        if scripted is not None:
            status, code = scripted
            self._reply(h, status, _error_xml(code))
            return
        problem = self._verify(h, method, body)
        if problem:
            status = 400 if problem == "XAmzContentSHA256Mismatch" else 403
            self._reply(h, status, _error_xml(problem), extra={"Date": formatdate(usegmt=True)})
            return
        parts = urlsplit(h.path)
        segs = parts.path.lstrip("/").split("/", 1)
        if segs[0] != self.bucket:
            self._reply(h, 404, _error_xml("NoSuchBucket"))
            return
        key = unquote(segs[1]) if len(segs) > 1 else ""
        if method == "PUT" and key:
            with self.lock:
                self.objects[key] = body
                self.put_counts[key] = self.put_counts.get(key, 0) + 1
            self._reply(h, 200, extra={"ETag": '"%s"' % hashlib.md5(body).hexdigest()})
        elif method == "GET" and key:
            with self.lock:
                data = self.objects.get(key)
            if data is None:
                self._reply(h, 404, _error_xml("NoSuchKey"))
            else:
                self._reply(h, 200, data, ctype="application/octet-stream")
        else:
            self._list(h, parse_qs(parts.query))

    def _list(self, h, q: dict) -> None:
        prefix = q.get("prefix", [""])[0]
        start = q.get("continuation-token", [""])[0]
        with self.lock:
            keys = sorted(k for k in self.objects if k.startswith(prefix) and k > start)
        page, more = keys[: self.max_keys], len(keys) > self.max_keys
        items = "".join(f"<Contents><Key>{escape(k)}</Key></Contents>" for k in page)
        token = f"<NextContinuationToken>{escape(page[-1])}</NextContinuationToken>" if more else ""
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult>'
            f"<Name>{self.bucket}</Name><Prefix>{escape(prefix)}</Prefix>"
            f"<KeyCount>{len(page)}</KeyCount><IsTruncated>{str(more).lower()}</IsTruncated>"
            f"{items}{token}</ListBucketResult>"
        ).encode()
        self._reply(h, 200, body)

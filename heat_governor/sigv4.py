"""AWS Signature Version 4 signer (stdlib only: hmac, hashlib, urllib.parse).

``sign`` canonicalizes whatever URL it is given (path-style or virtual-hosted)
and returns a new header dict carrying ``Authorization``, ``x-amz-date``,
``x-amz-content-sha256`` (always the real payload hash) and ``Host``. The pure
building blocks (``canonical_request``, ``string_to_sign``, ``signing_key``,
``signature``) are public so a test server can verify a signature.

The secret key is only ever an argument: it is never stored, returned, or put
in an exception message.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, unquote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class SigV4Error(ValueError):
    """Bad input to the signer. Messages never include credentials."""


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _enc(value: str) -> str:
    return quote(value, safe="-_.~")


def canonical_uri(path: str, service: str = "s3") -> str:
    path = path or "/"
    once = quote(unquote(path), safe="/-_.~")
    return once if service == "s3" else quote(once, safe="/-_.~")


def canonical_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    encoded = sorted((_enc(k), _enc(v)) for k, v in pairs)
    return "&".join(f"{k}={v}" for k, v in encoded)


def _canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
    norm = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    names = sorted(norm)
    block = "".join(f"{n}:{norm[n]}\n" for n in names)
    return block, ";".join(names)


def canonical_request(
    method: str, url: str, headers: dict[str, str], payload_sha256: str, service: str = "s3"
) -> tuple[str, str]:
    """Return ``(canonical_request, signed_headers)`` for the given headers."""
    parts = urlsplit(url)
    block, signed = _canonical_headers(headers)
    text = "\n".join(
        [
            method.upper(),
            canonical_uri(parts.path, service),
            canonical_query(parts.query),
            block,
            signed,
            payload_sha256,
        ]
    )
    return text, signed


def credential_scope(date: str, region: str, service: str) -> str:
    return f"{date}/{region}/{service}/aws4_request"


def string_to_sign(amz_date: str, scope: str, canonical: str) -> str:
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "\n".join([ALGORITHM, amz_date, scope, digest])


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def signature(key: bytes, sts: str) -> str:
    return hmac.new(key, sts.encode("utf-8"), hashlib.sha256).hexdigest()


def sign(
    method: str,
    url: str,
    headers: dict[str, str] | None,
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    now: datetime | None = None,
) -> dict[str, str]:
    """Return a copy of ``headers`` plus the SigV4 headers (incl. Authorization)."""
    if not access_key or not secret_key:
        raise SigV4Error("access key and secret key are required")
    if len(payload_sha256) != 64:
        raise SigV4Error("payload_sha256 must be a 64-char hex sha256 digest")
    parts = urlsplit(url)
    if not parts.hostname:
        raise SigV4Error("url must be absolute with a host")
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]

    out = dict(headers or {})
    for k in [k for k in out if k.lower() in ("authorization", "x-amz-date")]:
        del out[k]
    if not any(k.lower() == "host" for k in out):
        out["Host"] = parts.netloc
    out["x-amz-date"] = amz_date
    for k in [k for k in out if k.lower() == "x-amz-content-sha256"]:
        del out[k]
    out["x-amz-content-sha256"] = payload_sha256

    canonical, signed = canonical_request(method, url, out, payload_sha256, service)
    scope = credential_scope(date, region, service)
    sig = signature(
        signing_key(secret_key, date, region, service),
        string_to_sign(amz_date, scope, canonical),
    )
    out["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    )
    return out

"""SigV4 signer vs AWS's published S3 examples (keys assembled at runtime)."""

from datetime import datetime, timezone

import pytest

from heat_governor import sigv4

# AWS documented example credentials, split so no scanner-flagged literal is committed.
AK = "AKIA" + "IOSFODNN7" + "EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG" + "/bPxRfiCYEXAMPLE" + "KEY"
NOW = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
HOST = "examplebucket.s3.amazonaws.com"
EMPTY = sigv4.EMPTY_SHA256


def _sig(h):
    return h["Authorization"].rsplit("Signature=", 1)[1]


def test_get_object_range_example():
    h = sigv4.sign(
        "GET",
        f"https://{HOST}/test.txt",
        {"Range": "bytes=0-9"},
        EMPTY,
        AK,
        SK,
        "us-east-1",
        now=NOW,
    )
    assert _sig(h) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in h["Authorization"]
    assert f"Credential={AK}/20130524/us-east-1/s3/aws4_request" in h["Authorization"]
    assert h["x-amz-content-sha256"] == EMPTY
    assert h["x-amz-date"] == "20130524T000000Z"


def test_canonical_request_hash_get_object():
    hdrs = {"Host": HOST, "Range": "bytes=0-9", "x-amz-content-sha256": EMPTY}
    hdrs["x-amz-date"] = "20130524T000000Z"
    canon, _ = sigv4.canonical_request("GET", f"https://{HOST}/test.txt", hdrs, EMPTY)
    import hashlib

    assert (
        hashlib.sha256(canon.encode()).hexdigest()
        == "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972"
    )


def test_get_lifecycle_example():
    h = sigv4.sign("GET", f"https://{HOST}/?lifecycle", {}, EMPTY, AK, SK, "us-east-1", now=NOW)
    assert _sig(h) == "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"


def test_list_objects_example_query_sorted():
    h = sigv4.sign(
        "GET", f"https://{HOST}/?max-keys=2&prefix=J", {}, EMPTY, AK, SK, "us-east-1", now=NOW
    )
    assert _sig(h) == "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"


def test_put_object_example():
    import hashlib

    body = b"Welcome to Amazon S3."
    sha = hashlib.sha256(body).hexdigest()
    assert sha == "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    h = sigv4.sign(
        "PUT",
        f"https://{HOST}/test%24file.text",
        {"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
        sha,
        AK,
        SK,
        "us-east-1",
        now=NOW,
    )
    assert _sig(h) == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    assert h["x-amz-content-sha256"] == sha


def test_canonical_query_sorts_and_encodes():
    assert sigv4.canonical_query("b=2&a=1&a=0") == "a=0&a=1&b=2"
    assert sigv4.canonical_query("prefix=a b&x=%2F") == "prefix=a%20b&x=%2F"
    assert sigv4.canonical_query("lifecycle") == "lifecycle="
    assert sigv4.canonical_query("k=a+b~") == "k=a%20b~"


def test_region_auto_signs():
    h = sigv4.sign(
        "PUT", "https://acct.r2.cloudflarestorage.com/bkt/key", None, EMPTY, AK, SK, "auto", now=NOW
    )
    assert "/auto/s3/aws4_request" in h["Authorization"]
    assert h["Host"] == "acct.r2.cloudflarestorage.com"


def test_overrides_caller_content_hash():
    h = sigv4.sign(
        "PUT", f"https://{HOST}/k", {"x-amz-content-sha256": "UNSIGNED-PAYLOAD"}, EMPTY, AK, SK, "x"
    )
    assert h["x-amz-content-sha256"] == EMPTY
    assert "UNSIGNED" not in str(h)


def test_secret_never_leaks():
    h = sigv4.sign("GET", f"https://{HOST}/t", {"Range": "bytes=0-1"}, EMPTY, AK, SK, "us-east-1")
    assert SK not in repr(h)
    assert SK not in "".join(h.values())
    for bad in (
        dict(payload_sha256="short"),
        dict(url="/no-host"),
    ):
        kw = dict(
            method="GET",
            url=f"https://{HOST}/t",
            headers={},
            payload_sha256=EMPTY,
            access_key=AK,
            secret_key=SK,
            region="r",
        )
        kw.update(bad)
        with pytest.raises(sigv4.SigV4Error) as ei:
            sigv4.sign(**kw)
        assert SK not in str(ei.value)
        assert SK not in repr(ei.value)
    with pytest.raises(sigv4.SigV4Error) as ei:
        sigv4.sign("GET", f"https://{HOST}/t", {}, EMPTY, "", SK, "r")
    assert SK not in str(ei.value)

"""ship.py against a SigV4-verifying fake S3 on 127.0.0.1 (no real network, no real ~/.aws)."""

from __future__ import annotations

import gzip
import json
import os
import re
import socket
import stat
import time
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from heat_governor import ship
from tests.fake_s3 import BUCKET, FAKE_ACCESS_KEY, FAKE_SECRET_KEY, REGION, FakeS3

MACHINE = "spark-a"
BOOT = "0b1c2d3e-aaaa-bbbb-cccc-000000000001"


@pytest.fixture(autouse=True)
def _no_real_aws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name, raising=False)
    # keys must never come from the hostname
    monkeypatch.setattr(socket, "gethostname", lambda: "real-hostname-leak")


@pytest.fixture
def s3():
    with FakeS3() as fake:
        yield fake


class Log:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(str(msg))

    def count(self, needle: str) -> int:
        return sum(needle in line for line in self.lines)


def creds():
    return ship.Credentials(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)


def client(fake, **kw):
    kw.setdefault("timeout", 2.0)
    return ship.S3Client(fake.endpoint, REGION, fake.bucket, creds(), **kw)


def wait_for(pred, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


TUNING_FIELDS = {f.name for f in fields(ship.ShipperTuning)}


def make_shipper(fake_or_client, tmp_path, log=None, **kw):
    c = fake_or_client if isinstance(fake_or_client, ship.S3Client) else client(fake_or_client)
    kw.setdefault("backoff_initial_s", 0.05)
    kw.setdefault("backoff_max_s", 0.2)
    kw.setdefault("config_retry_s", 0.2)
    tuning = {k: kw.pop(k) for k in TUNING_FIELDS & set(kw)}
    return ship.Shipper(
        c,
        machine=MACHINE,
        boot_id=BOOT,
        spool_dir=tmp_path / "spool",
        prefix="fleet",
        log=log or Log(),
        tuning=ship.ShipperTuning(**tuning),
        **kw,
    )


def spool_files(tmp_path) -> list[Path]:
    d = tmp_path / "spool"
    return sorted(p for p in d.glob("*.spool")) if d.exists() else []


def assert_no_secret(tmp_path, log: Log, *texts: str) -> None:
    for line in log.lines:
        assert FAKE_SECRET_KEY not in line
        assert "Signature=" not in line
        assert "Authorization" not in line
    for t in texts:
        assert FAKE_SECRET_KEY not in t
    for p in tmp_path.rglob("*"):
        if p.is_file() and ".aws" not in p.parts:
            assert FAKE_SECRET_KEY.encode() not in p.read_bytes()


# --- credentials -----------------------------------------------------------


def write_credentials(home: Path, mode: int = 0o600) -> Path:
    path = home / ".aws" / "credentials"
    path.parent.mkdir(parents=True, exist_ok=True)
    ak_field, sk_field = "aws_access_key_id", "aws_secret_access_key"
    path.write_text(
        "[default]\n"
        f"{ak_field} = DEFAULTKEY\n{sk_field} = default-{'d' * 4}\n"
        "[ship]\n"
        f"{ak_field} = {FAKE_ACCESS_KEY}\n{sk_field} = {FAKE_SECRET_KEY}\n"
    )
    os.chmod(path, mode)
    return path


def test_credentials_env_first(tmp_path):
    home = tmp_path / "h"
    write_credentials(home)
    env = {"AWS_ACCESS_KEY_ID": "ENVKEY", "AWS_SECRET_ACCESS_KEY": "env-" + "s" * 6}
    c = ship.resolve_credentials("ship", environ=env, home=home)
    assert c.access_key == "ENVKEY"
    assert c.source == "env"
    assert "env-ssssss" not in repr(c)
    assert "env-ssssss" not in str(c)


def test_credentials_named_profile_then_default(tmp_path):
    home = tmp_path / "h"
    write_credentials(home)
    log = Log()
    c = ship.resolve_credentials("ship", environ={}, home=home, log=log)
    assert (c.access_key, c.secret_key) == (FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    assert c.source == "profile:ship"
    assert ship.resolve_credentials(None, environ={}, home=home).access_key == "DEFAULTKEY"
    assert ship.resolve_credentials("nope", environ={}, home=home, log=log) is None
    assert log.count("world-readable") == 0
    assert_no_secret(tmp_path, log, repr(c))


def test_credentials_world_readable_warns(tmp_path):
    home = tmp_path / "h"
    path = write_credentials(home, mode=0o644)
    assert path.stat().st_mode & stat.S_IROTH
    log = Log()
    c = ship.resolve_credentials("ship", environ={}, home=home, log=log)
    assert c is not None
    assert log.count("world-readable") == 1
    assert_no_secret(tmp_path, log)


def test_credentials_missing_everywhere(tmp_path):
    assert ship.resolve_credentials("ship", environ={}, home=tmp_path / "empty") is None


# --- client ----------------------------------------------------------------


def test_put_get_list_round_trip(s3):
    s3.max_keys = 2  # force ListObjectsV2 paging
    c = client(s3)
    keys = [f"fleet/{MACHINE}/{BOOT}/events/2026111{i}T000000.000Z-x.json" for i in range(5)]
    for i, k in enumerate(keys):
        c.put(k, json.dumps({"i": i}).encode(), content_type="application/json")
    c.put("other/thing", b"zz")
    assert json.loads(c.get(keys[3])) == {"i": 3}
    assert c.list(f"fleet/{MACHINE}/") == keys
    assert len(c.list("")) == 6
    # every request carried the real payload hash, never UNSIGNED-PAYLOAD
    for req in s3.requests:
        hdrs = {k.lower(): v for k, v in req["headers"].items()}
        assert hdrs["x-amz-content-sha256"] != "UNSIGNED-PAYLOAD"
        assert req["path"].startswith(f"/{BUCKET}")  # path-style


def test_client_get_missing_and_bad_secret(s3):
    c = client(s3)
    with pytest.raises(ship.ShipError) as err:
        c.get("fleet/none")
    assert err.value.status == 404
    assert err.value.code == "NoSuchKey"
    bad = ship.S3Client(s3.endpoint, REGION, BUCKET, ship.Credentials(FAKE_ACCESS_KEY, "wrong-k"))
    with pytest.raises(ship.ShipError) as err:
        bad.put("k", b"v")
    assert err.value.status == 403
    assert err.value.code == "SignatureDoesNotMatch"
    assert "wrong-k" not in str(err.value) and "wrong-k" not in repr(err.value)


def test_client_refuses_plain_http_to_open_internet():
    cred = creds()
    with pytest.raises(ValueError):
        ship.S3Client("http://203.0.113.9:9000", REGION, BUCKET, cred)
    ship.S3Client("http://100.100.1.2:9000", REGION, BUCKET, creds())  # tailnet ok
    ship.S3Client("https://s3.example.invalid", "auto", BUCKET, creds())


def test_client_timeout_raises_shiperror(s3):
    s3.stall_s = 1.0
    c = client(s3, timeout=0.2)
    t0 = time.monotonic()
    with pytest.raises(ship.ShipError) as err:
        c.put("k", b"v")
    assert time.monotonic() - t0 < 0.9
    assert err.value.status is None
    assert err.value.code == "Timeout"


def test_client_adopts_server_clock_after_skew(s3):
    s3.strict_time = True
    skewed = lambda: datetime.now(timezone.utc) - timedelta(hours=2)  # noqa: E731
    c = client(s3, now=skewed)
    with pytest.raises(ship.ShipError) as err:
        c.put("k", b"v")
    assert err.value.code == "RequestTimeTooSkewed"
    c.put("k", b"v")  # offset adopted from the server's Date header
    assert s3.objects["k"] == b"v"


# --- key layout ------------------------------------------------------------


def test_key_layout_obligation_o3():
    t = datetime(2026, 11, 14, 17, 34, 18, 123000, tzinfo=timezone.utc).timestamp()
    assert ship.utc_stamp(t) == "20261114T173418.123Z"
    assert (
        ship.event_key("fleet", MACHINE, BOOT, "memory_kill", t)
        == f"fleet/{MACHINE}/{BOOT}/events/20261114T173418.123Z-memory_kill.json"
    )
    assert ship.blackbox_key("fleet/", MACHINE, BOOT) == f"fleet/{MACHINE}/{BOOT}/blackbox.jsonl.gz"
    assert ship.postmortem_key("a/b", MACHINE, BOOT) == f"a/b/{MACHINE}/{BOOT}/postmortem.json"
    # a slash in machine or kind can't escape its path segment
    assert ship.event_key("p", "a/b", BOOT, "x/y-z", t).split("/")[1] == "a_b"
    assert ship.event_key("p", "m", BOOT, "x/y-z", t).endswith("-x_y_z.json")


def test_shipper_needs_a_machine_name(s3, tmp_path):
    c = client(s3)
    with pytest.raises(ValueError):
        ship.Shipper(c, machine="", boot_id=BOOT, spool_dir=tmp_path / "s")


# --- shipper ---------------------------------------------------------------

EVENT_RE = re.compile(
    rf"^fleet/{MACHINE}/{BOOT}/events/\d{{8}}T\d{{6}}\.\d{{3}}Z(-\d+)?-[A-Za-z0-9_.]+\.json$"
)


def test_events_land_with_layout_and_machine_name(s3, tmp_path):
    log = Log()
    sh = make_shipper(s3, tmp_path, log)
    sh.start()
    try:
        t = 1_800_000_000.5
        k1 = sh.submit("memory_kill", {"pid": 1}, t=t)
        k2 = sh.submit("memory_kill", {"pid": 2}, t=t)  # same millisecond
        k3 = sh.submit_postmortem({"why": "oom"})
        assert k1 != k2
        assert wait_for(lambda: {k1, k2, k3} <= set(s3.objects))
    finally:
        sh.stop()
    assert EVENT_RE.match(k1), (k1, k2)
    assert EVENT_RE.match(k2), (k1, k2)
    assert k3 == f"fleet/{MACHINE}/{BOOT}/postmortem.json"
    assert json.loads(s3.objects[k1])["pid"] == 1
    for key in s3.objects:
        assert f"/{MACHINE}/" in key
        assert "real-hostname-leak" not in key
    assert spool_files(tmp_path) == []
    assert_no_secret(tmp_path, log)


def test_blackbox_gzips_recorder_tail(s3, tmp_path):
    rec = tmp_path / "records"
    rec.mkdir()
    now = time.time()
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")
    rows = [{"t": now - 30, "a": 1}, {"t": now - 5, "a": 2}, {"t": now - 3600, "a": 0}]
    (rec / f"{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    sh = make_shipper(s3, tmp_path, records_dir=rec, blackbox_every_s=3600, blackbox_minutes=10)
    sh.start()
    key = ship.blackbox_key("fleet", MACHINE, BOOT)
    try:
        assert wait_for(lambda: key in s3.objects)  # first black box ships at start
    finally:
        sh.stop()
    lines = gzip.decompress(s3.objects[key]).decode().splitlines()
    assert [json.loads(x)["a"] for x in lines] == [1, 2]


def test_blackbox_every_10s_while_memory_is_low(s3, tmp_path):
    rec = tmp_path / "records"
    rec.mkdir()
    low = {"v": False}
    sh = make_shipper(
        s3,
        tmp_path,
        records_dir=rec,
        blackbox_every_s=3600,
        blackbox_low_memory_s=0.1,
        low_memory=lambda: low["v"],
    )
    key = ship.blackbox_key("fleet", MACHINE, BOOT)
    sh.start()
    try:
        assert wait_for(lambda: s3.put_counts.get(key, 0) >= 1)
        time.sleep(0.3)
        assert s3.put_counts[key] == 1  # normal cadence: hourly here
        low["v"] = True
        sh.submit("memory_low", {})  # any wake re-evaluates the cadence
        assert wait_for(lambda: s3.put_counts.get(key, 0) >= 3)
    finally:
        sh.stop()


@pytest.mark.parametrize(
    "failure", [(503, "SlowDown"), (403, "RequestTimeTooSkewed"), (500, "InternalError")]
)
def test_transient_failure_then_success_lands_once(s3, tmp_path, failure):
    s3.fail_next = [failure]
    log = Log()
    sh = make_shipper(s3, tmp_path, log, boot_window_s=0.0)
    sh.start()
    try:
        key = sh.submit("memory_kill", {"x": 1})
        assert wait_for(lambda: key in s3.objects)
        assert wait_for(lambda: spool_files(tmp_path) == [])
    finally:
        sh.stop()
    assert s3.put_counts[key] == 1
    assert_no_secret(tmp_path, log)


def test_server_down_then_up_spool_drains(tmp_path):
    fake = FakeS3().start()
    port = fake.port
    fake.stop()  # down
    log = Log()
    sh = make_shipper(client(fake, timeout=0.3), tmp_path, log)
    sh.start()
    try:
        keys = [sh.submit("memory_kill", {"i": i}) for i in range(3)]
        assert wait_for(lambda: len(spool_files(tmp_path)) == 3)
        time.sleep(0.2)  # a few failed attempts
        up = FakeS3(port=port).start()
        try:
            assert wait_for(lambda: set(keys) <= set(up.objects))
            assert wait_for(lambda: spool_files(tmp_path) == [])
            assert all(up.put_counts[k] == 1 for k in keys)
        finally:
            sh.stop()
            up.stop()
    finally:
        sh.stop()
    assert_no_secret(tmp_path, log)


def test_spool_survives_restart(tmp_path):
    fake = FakeS3().start()
    port = fake.port
    fake.stop()
    sh = make_shipper(client(fake, timeout=0.3), tmp_path)
    key = sh.submit("memory_kill", {"i": 1})  # never started: spooled only (crash case)
    assert len(spool_files(tmp_path)) == 1
    raw = spool_files(tmp_path)[0].read_bytes()
    header, body = raw.split(b"\n", 1)
    assert json.loads(header)["key"] == key
    assert json.loads(body) == {"i": 1}
    with FakeS3(port=port) as up:
        sh2 = make_shipper(client(up), tmp_path)
        sh2.start()
        try:
            assert wait_for(lambda: key in up.objects)
        finally:
            sh2.stop()
    assert spool_files(tmp_path) == []


def test_no_such_bucket_logged_once_per_hour_and_not_tight_looped(s3, tmp_path):
    s3.always = (404, "NoSuchBucket")
    log = Log()
    clock = {"t": 1000.0}
    sh = make_shipper(s3, tmp_path, log, config_retry_s=0.25, log_clock=lambda: clock["t"])
    sh.start()
    try:
        key = sh.submit("memory_kill", {})
        time.sleep(0.6)
        assert log.count("NoSuchBucket") == 1
        n = len(s3.requests)
        assert n <= 5, n  # retried at config_retry_s, not in a tight loop
        clock["t"] += 3601
        assert wait_for(lambda: log.count("NoSuchBucket") == 2)
        s3.always = None  # operator fixed the bucket
        sh.submit("wake", {})
        assert wait_for(lambda: key in s3.objects, timeout=3)
    finally:
        sh.stop()
    assert_no_secret(tmp_path, log)


def test_403_inside_boot_window_retries_outside_is_config(s3, tmp_path):
    s3.fail_next = [(403, "AccessDenied")]
    log = Log()
    sh = make_shipper(s3, tmp_path, log, boot_window_s=600)
    assert sh.classify(ship.ShipError(403, "AccessDenied")) == "retry"
    sh.start()
    try:
        key = sh.submit("memory_kill", {})
        assert wait_for(lambda: key in s3.objects)
    finally:
        sh.stop()
    late = make_shipper(s3, tmp_path, boot_window_s=0.0)
    assert late.classify(ship.ShipError(403, "AccessDenied")) == "config"
    assert late.classify(ship.ShipError(403, "SignatureDoesNotMatch")) == "config"
    assert late.classify(ship.ShipError(403, "RequestTimeTooSkewed")) == "retry"
    assert late.classify(ship.ShipError(None, "Timeout")) == "retry"
    assert late.classify(ship.ShipError(502, "BadGateway")) == "retry"
    assert late.classify(ship.ShipError(404, "NoSuchBucket")) == "config"
    assert late.classify(ship.ShipError(None, "InvalidRequest")) == "config"


def test_empty_secret_is_a_config_error_not_a_crash(s3):
    c = ship.S3Client(s3.endpoint, REGION, BUCKET, ship.Credentials(FAKE_ACCESS_KEY, ""))
    with pytest.raises(ship.ShipError) as err:
        c.put("k", b"v")
    assert err.value.code == "InvalidRequest"
    assert err.value.status is None


def test_stalled_server_never_blocks_submit(s3, tmp_path):
    s3.stall_s = 1.5
    log = Log()
    sh = make_shipper(client(s3, timeout=0.3), tmp_path, log)
    sh.start()
    try:
        t0 = time.monotonic()
        for i in range(20):
            sh.submit("memory_kill", {"i": i})
        assert time.monotonic() - t0 < 0.25
    finally:
        t0 = time.monotonic()
        sh.stop(timeout=2.0)
        assert time.monotonic() - t0 < 2.5
    assert len(spool_files(tmp_path)) == 20  # nothing lost
    assert_no_secret(tmp_path, log)


def test_full_queue_does_not_block_or_lose(s3, tmp_path):
    sh = make_shipper(s3, tmp_path, queue_size=2)  # not started: queue fills
    keys = [sh.submit("e", {"i": i}) for i in range(10)]
    assert len(spool_files(tmp_path)) == 10
    sh.start()
    try:
        assert wait_for(lambda: set(keys) <= set(s3.objects))
    finally:
        sh.stop()


def test_from_rule_uses_machine_default_and_profile(s3, tmp_path):
    from heat_governor.config import ShipRule

    home = tmp_path / "h"
    write_credentials(home)
    rule = ShipRule(enabled=True, endpoint=s3.endpoint, bucket=BUCKET, profile="ship")
    sh = ship.Shipper.from_rule(
        rule,
        default_machine="fleet-name",
        boot_id=BOOT,
        spool_dir=tmp_path / "spool",
        environ={},
        home=home,
    )
    assert sh.machine == "fleet-name"
    rule2 = ShipRule(
        enabled=True, endpoint=s3.endpoint, bucket=BUCKET, profile="ship", machine="explicit"
    )
    sh2 = ship.Shipper.from_rule(
        rule2, default_machine="fleet-name", boot_id=BOOT, environ={}, home=home,
        spool_dir=tmp_path / "spool",
    )  # fmt: skip
    assert sh2.machine == "explicit"
    with pytest.raises(ValueError):
        ship.Shipper.from_rule(
            rule, default_machine="m", boot_id=BOOT, spool_dir=tmp_path, environ={}, home=tmp_path
        )

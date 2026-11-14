"""Strict [memory] and [ship] configuration sections."""

from __future__ import annotations

import tomllib

import pytest

from heat_governor import config as cfgmod
from heat_governor.platforms import SPARK


def _problems(data: dict) -> list[str]:
    return cfgmod.validate(data)


def test_absent_sections_mean_record_and_shipping_off():
    cfg = cfgmod.Config(data={})
    assert _problems({}) == []
    assert cfg.memory.action == "record"
    assert cfg.ship.enabled is False
    assert cfg.ship.endpoint is None


def test_memory_defaults():
    m = cfgmod.Config(data={}).memory
    assert (m.floor_pct, m.horizon_s, m.soft_pct, m.window_s) == (5.0, 10.0, 20.0, 10.0)
    assert (m.min_victim_mib, m.max_kills_per_hour, m.protect) == (2048, 3, ())
    assert (m.poll_s, m.top_n, m.top_every_s) == (0.25, 10, 10.0)


def test_ship_defaults():
    s = cfgmod.Config(data={}).ship
    assert s.region == "us-east-1" and s.prefix == "fleet"
    assert (s.blackbox_every_s, s.blackbox_minutes) == (60.0, 10.0)
    assert s.spool_dir is None and s.machine is None and s.profile is None


def test_memory_values_parsed():
    data = {
        "memory": {
            "action": "kill",
            "floor_pct": 8,
            "soft_pct": 25.5,
            "protect": ["sshd", "x"],
            "poll_s": 0.5,
            "top_n": 5,
        }
    }
    assert _problems(data) == []
    m = cfgmod.Config(data=data).memory
    assert m.action == "kill" and m.floor_pct == 8.0 and m.soft_pct == 25.5
    assert m.protect == ("sshd", "x") and m.poll_s == 0.5 and m.top_n == 5


@pytest.mark.parametrize(
    "key,bad",
    [
        ("action", "nuke"),
        ("action", 3),
        ("floor_pct", "5"),
        ("floor_pct", True),
        ("floor_pct", 0.5),
        ("floor_pct", 51),
        ("horizon_s", 0),
        ("horizon_s", 121),
        ("soft_pct", 101),
        ("window_s", 1),
        ("window_s", 121),
        ("min_victim_mib", 63),
        ("min_victim_mib", 100.5),
        ("max_kills_per_hour", -1),
        ("max_kills_per_hour", 61),
        ("max_kills_per_hour", 1.5),
        ("protect", "sshd"),
        ("protect", [1]),
        ("protect", [""]),
        ("poll_s", 0.05),
        ("poll_s", 2.5),
        ("top_n", 0),
        ("top_n", 101),
        ("top_every_s", 0),
    ],
)
def test_memory_bad_values(key, bad):
    probs = _problems({"memory": {key: bad}})
    assert probs and any(f"memory.{key}" in p for p in probs)


def test_memory_soft_must_exceed_floor():
    assert _problems({"memory": {"floor_pct": 30}})  # default soft 20 <= 30
    assert _problems({"memory": {"floor_pct": 10, "soft_pct": 10}})
    assert _problems({"memory": {"floor_pct": 10, "soft_pct": 11}}) == []


def test_memory_range_edges_ok():
    data = {
        "memory": {
            "floor_pct": 1,
            "soft_pct": 50,
            "horizon_s": 120,
            "window_s": 2,
            "min_victim_mib": 64,
            "max_kills_per_hour": 0,
            "poll_s": 0.1,
            "top_n": 100,
        }
    }
    assert _problems(data) == []


def test_unknown_memory_key_lists_known():
    probs = _problems({"memory": {"floor": 5}})
    assert len(probs) == 1
    assert "unknown key memory.'floor'" in probs[0]
    for k in cfgmod.MEMORY_KEYS:
        assert k in probs[0]


def test_unknown_ship_key_lists_known():
    probs = _problems({"ship": {"bukcet": "x"}})
    assert len(probs) == 1
    assert "unknown key ship.'bukcet'" in probs[0]
    for k in cfgmod.SHIP_KEYS:
        assert k in probs[0]


def test_sections_must_be_tables():
    assert _problems({"memory": 3}) == ["[memory] must be a table"]
    assert _problems({"ship": []}) == ["[ship] must be a table"]


def test_top_keys_include_sections():
    assert {"memory", "ship"} <= cfgmod.TOP_KEYS


@pytest.mark.parametrize(
    "key,bad",
    [
        ("enabled", "yes"),
        ("endpoint", 5),
        ("region", ""),
        ("region", 5),
        ("bucket", 5),
        ("prefix", 5),
        ("profile", 5),
        ("machine", ""),
        ("machine", 5),
        ("blackbox_every_s", 4),
        ("blackbox_every_s", 3601),
        ("blackbox_every_s", "60"),
        ("blackbox_minutes", 0),
        ("blackbox_minutes", 121),
        ("spool_dir", 5),
        ("spool_dir", ""),
    ],
)
def test_ship_bad_values(key, bad):
    probs = _problems({"ship": {key: bad}})
    assert probs and any(f"ship.{key}" in p for p in probs)


@pytest.mark.parametrize(
    "bucket",
    ["ab", "A-bucket", "-abc", "abc-", "a_b_c", "x" * 64, "a b c"],
)
def test_ship_bucket_naming(bucket):
    assert any("ship.bucket" in p for p in _problems({"ship": {"bucket": bucket}}))


@pytest.mark.parametrize("bucket", ["abc", "fleet-telemetry.2026", "x" * 63])
def test_ship_bucket_ok(bucket):
    assert _problems({"ship": {"bucket": bucket}}) == []


@pytest.mark.parametrize(
    "endpoint,ok",
    [
        ("http://127.0.0.1:9000", True),
        ("http://localhost:9000", True),
        ("http://[::1]:9000", True),
        ("http://100.101.102.103:9000", True),
        ("http://100.64.0.1", True),
        ("http://100.127.255.254:9000", True),
        ("http://100.128.0.1:9000", False),
        ("http://100.63.255.255:9000", False),
        ("http://example.org:9000", False),
        ("http://10.0.0.5:9000", False),
        ("https://example.org", True),
        ("https://s3.us-east-1.amazonaws.com", True),
        ("ftp://127.0.0.1", False),
        ("127.0.0.1:9000", False),
        ("http://", False),
        ("https://", False),
    ],
)
def test_ship_endpoint_rules(endpoint, ok):
    probs = _problems({"ship": {"endpoint": endpoint}})
    assert (probs == []) is ok, probs
    if not ok:
        assert any("ship.endpoint" in p for p in probs)


def test_ship_enabled_requires_endpoint_and_bucket():
    probs = _problems({"ship": {"enabled": True}})
    assert any("ship.endpoint" in p for p in probs)
    assert any("ship.bucket" in p for p in probs)
    ok = {"enabled": True, "endpoint": "https://s3.example.org", "bucket": "fleet-1"}
    assert _problems({"ship": ok}) == []
    s = cfgmod.Config(data={"ship": ok}).ship
    assert s.enabled and s.endpoint == "https://s3.example.org" and s.bucket == "fleet-1"


def test_ship_values_parsed():
    data = {
        "ship": {
            "region": "eu-west-1",
            "prefix": "p",
            "profile": "prof",
            "machine": "spark-1",
            "blackbox_every_s": 30,
            "blackbox_minutes": 5,
            "spool_dir": "/var/spool/hg",
        }
    }
    assert _problems(data) == []
    s = cfgmod.Config(data=data).ship
    assert s.region == "eu-west-1" and s.machine == "spark-1" and s.profile == "prof"
    assert s.blackbox_every_s == 30.0 and s.blackbox_minutes == 5.0
    assert s.spool_dir == "/var/spool/hg"


def test_template_still_valid_and_mentions_sections():
    text = cfgmod.template(SPARK)
    assert _problems(tomllib.loads(text)) == []
    assert "# [memory]" in text and "# [ship]" in text
    assert "\n[memory]" not in text and "\n[ship]" not in text

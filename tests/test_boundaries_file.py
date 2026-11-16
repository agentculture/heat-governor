"""boundaries.toml: emitter, locked versioned writer, backups. Everything under tmp_path."""

from __future__ import annotations

import os
import threading
import tomllib

import pytest

from heat_governor import boundaries_file as bf


def _rule(name="vllm", **kw):
    r = {"name": name, "comm": ["vllm"], "max_mib": 1024}
    r.update(kw)
    return r


def _roundtrip(rules):
    return tomllib.loads(bf.emit(rules))["memory"]["allow"]


def test_emit_roundtrips_awkward_matchers():
    rules = [
        _rule(
            "q",
            comm=[
                'a"b',
                "back\\slash",
                "new\nline",
                "tab\there",
                "ctl\x01\x1f\x7f",
                "caf\u00e9\u4e2d",
            ],
            cmdline='*"x"\\y\n*',
            cgroup="*\u00fc*",
            scope="total",
        ),
        _rule("plain", cmdline="*serve*"),
    ]
    assert _roundtrip(rules) == rules


def test_emit_empty_is_valid_toml():
    assert tomllib.loads(bf.emit([])) == {}


def test_emit_rejects_invalid_rule():
    with pytest.raises(bf.BoundariesError) as e:
        bf.emit([{"name": "x", "max_mib": 1024}])
    assert "matcher" in str(e.value) or "needs at least one" in str(e.value)


def test_emit_rejects_unknown_key_and_low_max():
    with pytest.raises(bf.BoundariesError):
        bf.emit([_rule(extra=1)])
    with pytest.raises(bf.BoundariesError):
        bf.emit([_rule(max_mib=1)])


def test_emit_rejects_unencodable_string():
    with pytest.raises(bf.BoundariesError):
        bf.emit([_rule(comm=["bad\ud800"])])


def test_read_missing_file_uses_sentinel(tmp_path):
    snap = bf.read(tmp_path / "boundaries.toml")
    assert snap.rules == []
    assert snap.version == bf.MISSING_VERSION
    assert snap.error is None
    assert snap.exists is False


def test_write_create_then_read(tmp_path):
    p = tmp_path / "boundaries.toml"
    res = bf.write(p, bf.MISSING_VERSION, [_rule()])
    assert res.ok
    snap = bf.read(p)
    assert snap.rules == [_rule()]
    assert snap.version == res.version
    assert snap.version != bf.MISSING_VERSION
    assert snap.exists


def test_create_with_stale_base_conflicts(tmp_path):
    p = tmp_path / "boundaries.toml"
    assert bf.write(p, bf.MISSING_VERSION, [_rule()]).ok
    res = bf.write(p, bf.MISSING_VERSION, [_rule("other")])
    assert res.ok is False
    assert res.conflict
    assert res.rules == [_rule()]
    assert bf.read(p).rules == [_rule()]


def test_update_with_wrong_base_writes_nothing(tmp_path):
    p = tmp_path / "boundaries.toml"
    first = bf.write(p, bf.MISSING_VERSION, [_rule()])
    before = p.read_bytes()
    res = bf.write(p, "0" * 64, [_rule("z")])
    assert res.ok is False
    assert res.version == first.version
    assert res.rules == [_rule()]
    assert p.read_bytes() == before
    assert bf.list_backups(p) == []


def test_update_with_right_base_backs_up_previous(tmp_path):
    p = tmp_path / "boundaries.toml"
    first = bf.write(p, bf.MISSING_VERSION, [_rule()])
    old = p.read_bytes()
    res = bf.write(p, first.version, [_rule("two")])
    assert res.ok
    backups = bf.list_backups(p)
    assert len(backups) == 1
    assert backups[0].read_bytes() == old
    assert backups[0].parent == tmp_path


def test_no_backup_when_file_was_missing(tmp_path):
    p = tmp_path / "boundaries.toml"
    bf.write(p, bf.MISSING_VERSION, [_rule()])
    assert bf.list_backups(p) == []


def test_backups_capped_at_five_newest_kept(tmp_path):
    p = tmp_path / "boundaries.toml"
    version = bf.write(p, bf.MISSING_VERSION, [_rule(max_mib=1000)]).version
    contents = []
    for i in range(9):
        contents.append(p.read_bytes())
        version = bf.write(p, version, [_rule(max_mib=2000 + i)]).version
    backups = bf.list_backups(p)
    assert len(backups) == bf.MAX_BACKUPS == 5
    assert [b.read_bytes() for b in backups] == contents[::-1][:5]


def test_failure_midway_leaves_file_identical(tmp_path, monkeypatch):
    p = tmp_path / "boundaries.toml"
    first = bf.write(p, bf.MISSING_VERSION, [_rule()])
    before = p.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        bf.write(p, first.version, [_rule("two")])
    monkeypatch.undo()
    assert p.read_bytes() == before
    assert sorted(x.name for x in tmp_path.iterdir() if ".tmp" in x.name) == []
    assert bf.read(p).version == first.version


def test_invalid_rules_write_nothing(tmp_path):
    p = tmp_path / "boundaries.toml"
    with pytest.raises(bf.BoundariesError):
        bf.write(p, bf.MISSING_VERSION, [_rule(), _rule()])
    assert not p.exists()


def test_invalid_file_reports_error_and_version(tmp_path):
    p = tmp_path / "boundaries.toml"
    p.write_text("not [toml")
    snap = bf.read(p)
    assert snap.error
    assert snap.rules == []
    assert snap.version != bf.MISSING_VERSION
    res = bf.write(p, snap.version, [_rule()])
    assert res.ok


def test_file_with_foreign_keys_is_an_error(tmp_path):
    p = tmp_path / "boundaries.toml"
    p.write_text("[memory]\naction = 'kill'\n")
    assert "action" in bf.read(p).error


def test_concurrent_writes_one_base_one_success(tmp_path):
    p = tmp_path / "boundaries.toml"
    n = 8
    gate = threading.Barrier(n)
    results = []

    def go(i):
        gate.wait()
        results.append(bf.write(p, bf.MISSING_VERSION, [_rule(f"r{i}")]))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.ok for r in results) == 1
    winner = next(r for r in results if r.ok)
    assert bf.read(p).rules == winner.rules
    for r in results:
        if not r.ok:
            assert r.rules == winner.rules


def test_rule_to_dict_from_allowrule():
    from heat_governor.config import AllowRule

    d = bf.rule_to_dict(AllowRule(name="a", max_mib=100, comm=("x",), cmdline="*y*"))
    assert d == {"name": "a", "comm": ["x"], "cmdline": "*y*", "max_mib": 100, "scope": "process"}

"""Recorder: thread-safe writes, memory in the reboot report, tail()."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from heat_governor.recorder import Recorder, iter_records, report, tail

T0 = datetime(2026, 11, 14, 3, 0, 0, tzinfo=timezone.utc).timestamp()


def test_concurrent_writes_are_intact(tmp_path):
    rec = Recorder(tmp_path, fsync=False)

    def work(i):
        for j in range(500):
            rec.write({"t": T0 + j, "thread": i, "j": j, "pad": "x" * 200})

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    rec.close()
    lines = [ln for p in tmp_path.glob("*.jsonl") for ln in p.read_text().splitlines()]
    assert len(lines) == 4000
    assert all(json.loads(ln)["pad"] == "x" * 200 for ln in lines)


def _tick(t, avail, swap, psi):
    return {
        "t": t,
        "temp_c": 60.0,
        "level": "ok",
        "system": {
            "mem:avail_mib": avail,
            "mem:swap_used_mib": swap,
            "psi:memory_full_avg10": psi,
        },
    }


def _fixture(tmp_path):
    rec = Recorder(tmp_path, fsync=False)
    rec.write({"t": T0, "event": "start", "boot_id": "A"})
    rec.write({"t": T0 + 1, "temp_c": 50.0, "level": "ok"})  # old tick, no system
    rec.write(_tick(T0 + 100, 20000, 0, 0.0))
    rec.write({"t": T0 + 400, "event": "memory_kill", "victim": "vllm"})
    rec.write({"t": T0 + 410, "event": "oom_kill", "pid": 7})
    for i, (a, s, p) in enumerate([(3000, 2000, 40.0), (200, 3500, 80.0), (0, 4096, 98.0)]):
        rec.write(_tick(T0 + 420 + i, a, s, p))
    rec.write({"t": T0 + 2000, "event": "start", "boot_id": "B"})
    rec.close()


def test_reboot_entry_reports_memory(tmp_path):
    _fixture(tmp_path)
    (entry,) = report(tmp_path, window_s=60.0)
    assert entry["previous_boot"] == "A"
    assert entry["mem"] == {
        "min_avail_mib": 0,
        "max_swap_used_mib": 4096,
        "peak_psi_memory_full": 98.0,
    }
    assert [e["event"] for e in entry["events"]] == ["memory_kill", "oom_kill"]


def test_old_records_yield_null_mem(tmp_path):
    rec = Recorder(tmp_path, fsync=False)
    rec.write({"t": T0, "event": "start", "boot_id": "A"})
    rec.write({"t": T0 + 1, "temp_c": 50.0, "level": "ok", "system": {"mem:avail_mib": 5}})
    rec.write({"t": T0 + 9, "event": "start", "boot_id": "B"})
    rec.close()
    (entry,) = report(tmp_path)
    assert entry["mem"] == {
        "min_avail_mib": 5,
        "max_swap_used_mib": None,
        "peak_psi_memory_full": None,
    }
    assert entry["events"] == []
    assert entry["max_temp_c"] == 50.0  # existing keys intact


def test_mixed_rows_parse(tmp_path):
    _fixture(tmp_path)
    with open(tmp_path / "20261114.jsonl", "a") as fh:
        fh.write('{"t": %s, "temp_c": 1, "top": [{"pid": 1}]}\n{torn' % (T0 + 3000))
    assert len(list(iter_records(tmp_path))) > 8
    assert len(report(tmp_path)) == 1


def test_tail_spans_day_boundary(tmp_path):
    rec = Recorder(tmp_path, fsync=False)
    midnight = datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp()
    for dt in (-600, -100, -10, 5):
        rec.write({"t": midnight + dt, "n": dt})
    rec.close()
    rows = tail(tmp_path, 3, now=midnight + 10)
    assert [r["n"] for r in rows] == [-100, -10, 5]
    rows = tail(tmp_path, 3, clock=lambda: midnight + 10)
    assert [r["n"] for r in rows] == [-100, -10, 5]
    assert tail(tmp_path / "missing", 5, now=midnight) == []


def _old_tail(directory, minutes, now):
    """The pre-fix behaviour: parse every line of the day files."""
    import json as _json

    since = now - minutes * 60.0
    days = {datetime.fromtimestamp(x, timezone.utc).strftime("%Y%m%d") for x in (since, now)}
    out = []
    for day in sorted(days):
        path = directory / f"{day}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines(True):
            try:
                rec = _json.loads(line)
            except ValueError:
                continue
            t = rec.get("t") if isinstance(rec, dict) else None
            if isinstance(t, (int, float)) and since <= t <= now:
                out.append(rec)
    return out


def test_tail_matches_full_scan_on_mixed_rows(tmp_path):
    midnight = datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp()
    prev = tmp_path / "20261114.jsonl"
    cur = tmp_path / "20261115.jsonl"
    prev.write_text(
        "".join(
            json.dumps({"t": midnight - 700 + i * 10, "n": i, "top": [{"pid": i}]}) + "\n"
            for i in range(70)
        )
        + '{"t":%s,"event":"start","boot":"x"}\n' % (midnight - 5)
    )
    cur.write_text(
        'garbage line\n{"t":"str"}\n[1,2]\n\n'
        + "".join(json.dumps({"t": midnight + i, "n": 100 + i}) + "\n" for i in range(60))
        + '{"t": %s, "event": "memory_kill"}\n' % (midnight + 61)
        + '{"t": %s, "n": 9' % (midnight + 62)  # torn last line, no newline
    )
    for minutes in (0.5, 2, 5, 15, 60):
        now = midnight + 90
        got = tail(tmp_path, minutes, now=now)
        assert got == _old_tail(tmp_path, minutes, now)
        assert got  # the fixture always yields rows


def test_tail_reads_a_small_fraction_of_a_large_file(tmp_path, monkeypatch):
    from heat_governor import recorder

    base = datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp()
    rows = 200_000
    step = 86_000 / rows
    with open(tmp_path / "20261115.jsonl", "w") as fh:
        for i in range(rows):
            fh.write('{"t":%.3f,"n":%d}\n' % (base + i * step, i))
    now = base + 86_000
    calls = []
    real = recorder.json.loads
    monkeypatch.setattr(recorder.json, "loads", lambda s, *a, **k: calls.append(1) or real(s))
    got = tail(tmp_path, 10, now=now)
    expected = [i for i in range(rows) if base + i * step >= now - 600]
    assert [r["n"] for r in got] == expected
    assert len(calls) < rows // 50


# -- memory_boundary_changed: the daemon records and ships a change to the merged rules (t10) ----

from pathlib import Path  # noqa: E402

from heat_governor import recorder as recorder_mod  # noqa: E402
from tests.test_daemon_memory import (  # noqa: E402
    ALLOW,
    ALLOW2,
    MEMORY,
    SHIP_NAMED,
    Rig,
    _events,
)

CHANGED = "memory_boundary_changed"
BOUNDARIES_ONE = '[[memory.allow]]\nname = "bf"\nmax_mib = 5000\ncomm = ["bf"]\n'
BOUNDARIES_TWO = BOUNDARIES_ONE + '\n[[memory.allow]]\nname = "bg"\nmax_mib = 6000\ncomm = ["bg"]\n'


def _boundaries(rig: Rig, text: str) -> None:
    (rig.cfg_path.parent / "boundaries.toml").write_text(text)
    cfg = rig.daemon.opts.config
    rig.rewrite(rig.cfg_path.read_text())  # a new config mtime makes the daemon reload both
    assert cfg is not rig.daemon.opts.config


def _shipped(rig: Rig) -> list[dict]:
    return [rec for kind, rec in rig.shippers[-1].submitted if kind == CHANGED]


def _names(rules) -> list[str]:
    return [r["name"] for r in rules]


def test_event_is_a_memory_event():
    assert CHANGED in recorder_mod.MEMORY_EVENTS


def test_reload_changing_merged_rules_emits_exactly_one_record_and_ships_it(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED + ALLOW)
    rig.daemon.start()
    base, base_shipped = len(_events(rig.rows(), CHANGED)), len(_shipped(rig))
    rig.rewrite(MEMORY + SHIP_NAMED + ALLOW2)
    rows = _events(rig.rows(), CHANGED)
    assert len(rows) == base + 1
    ev = rows[-1]
    assert _names(ev["old_rules"]) == ["vllm"]
    assert _names(ev["new_rules"]) == ["vllm", "hog"]
    assert ev["old_hash"] != ev["new_hash"]
    shipped = _shipped(rig)
    assert len(shipped) == base_shipped + 1
    assert shipped[-1]["new_hash"] == ev["new_hash"]
    rig.daemon.shutdown()


def test_unchanged_reload_emits_nothing_more(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED + ALLOW)
    rig.daemon.start()
    base = len(_events(rig.rows(), CHANGED))
    rig.rewrite(MEMORY + SHIP_NAMED + ALLOW + "\n# touched\n")
    assert len(_events(rig.rows(), CHANGED)) == base
    rig.daemon.shutdown()


def test_boundaries_file_change_alone_emits_and_the_log_says_so(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED)
    rig.daemon.start()
    _boundaries(rig, BOUNDARIES_ONE)
    _boundaries(rig, BOUNDARIES_TWO)
    rows = _events(rig.rows(), CHANGED)
    assert [_names(r["new_rules"]) for r in rows] == [["bf"], ["bf", "bg"]]
    assert {r["source"] for r in rows[-1]["new_rules"]} == {"boundaries.toml"}
    assert not any("no effective change" in line and "reloaded" in line for line in rig.logs[-2:])
    assert any("boundaries" in line and "reloaded" in line for line in rig.logs[-2:])
    rig.daemon.shutdown()


def test_event_fires_without_a_running_guard_thread_and_without_memory_section(tmp_path):
    rig = Rig(tmp_path, SHIP_NAMED)
    rig.daemon.start()
    assert rig.daemon.guard is None
    _boundaries(rig, BOUNDARIES_ONE)
    assert len(_events(rig.rows(), CHANGED)) == 1
    assert len(_shipped(rig)) == 1
    rig.daemon.shutdown()


def test_event_holds_rule_definitions_only(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED + ALLOW)
    rig.daemon.start()
    rig.rewrite(MEMORY + SHIP_NAMED + ALLOW2)
    ev = _events(rig.rows(), CHANGED)[-1]
    assert set(ev) == {"t", "event", "at_startup", "old_rules", "new_rules", "old_hash", "new_hash"}
    keys = {k for r in ev["old_rules"] + ev["new_rules"] for k in r}
    assert keys <= {"name", "comm", "cmdline", "cgroup", "max_mib", "scope", "source"}
    rig.daemon.shutdown()


def _second_start(tmp_path: Path, first: Rig, text: str) -> Rig:
    rig = Rig(tmp_path / "again", text)
    stored = first.data / "boundaries-applied.json"
    if stored.exists():
        (rig.data).mkdir(parents=True, exist_ok=True)
        (rig.data / stored.name).write_bytes(stored.read_bytes())
    return rig


def test_first_start_with_rules_emits_once_with_no_old_rules(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED + ALLOW)
    rig.daemon.start()
    rows = _events(rig.rows(), CHANGED)
    assert len(rows) == 1
    assert rows[0]["at_startup"] is True
    assert rows[0]["old_rules"] is None
    assert _names(rows[0]["new_rules"]) == ["vllm"]
    assert len(_shipped(rig)) == 1
    rig.daemon.shutdown()


def test_first_start_with_no_rules_emits_nothing(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED)
    rig.daemon.start()
    assert not _events(rig.rows(), CHANGED)
    rig.daemon.shutdown()


def test_restart_with_the_same_rules_emits_none(tmp_path):
    first = Rig(tmp_path, MEMORY + SHIP_NAMED + ALLOW)
    first.daemon.start()
    first.daemon.shutdown()
    second = _second_start(tmp_path, first, MEMORY + SHIP_NAMED + ALLOW)
    second.daemon.start()
    assert not _events(second.rows(), CHANGED)
    assert not _shipped(second)
    second.daemon.shutdown()


def test_restart_after_an_edit_made_while_down_emits_one_with_the_old_rules(tmp_path):
    first = Rig(tmp_path, MEMORY + SHIP_NAMED + ALLOW)
    first.daemon.start()
    first.daemon.shutdown()
    second = _second_start(tmp_path, first, MEMORY + SHIP_NAMED + ALLOW2)
    second.daemon.start()
    rows = _events(second.rows(), CHANGED)
    assert len(rows) == 1
    assert rows[0]["at_startup"] is True
    assert _names(rows[0]["old_rules"]) == ["vllm"]
    assert _names(rows[0]["new_rules"]) == ["vllm", "hog"]
    assert len(_shipped(second)) == 1
    second.daemon.shutdown()


def test_removing_every_rule_while_down_emits_one(tmp_path):
    first = Rig(tmp_path, MEMORY + SHIP_NAMED + ALLOW)
    first.daemon.start()
    first.daemon.shutdown()
    second = _second_start(tmp_path, first, MEMORY + SHIP_NAMED)
    second.daemon.start()
    (row,) = _events(second.rows(), CHANGED)
    assert row["new_rules"] == []
    second.daemon.shutdown()


def test_a_failing_recorder_and_shipper_never_raise_into_the_tick(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED + ALLOW)
    rig.daemon.start()

    real = rig.daemon.recorder.write

    def boom(*a, **k):
        raise OSError("disk gone")

    def write(rec):
        return boom() if rec.get("event") == CHANGED else real(rec)

    rig.daemon.recorder.write = write
    rig.shippers[-1].submit = boom
    rig.rewrite(MEMORY + SHIP_NAMED + ALLOW2)  # must not raise
    assert rig.daemon.opts.memory.allow[-1].name == "hog"
    rig.daemon.shutdown()


def test_an_unwritable_state_dir_never_raises(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED + ALLOW)
    rig.daemon.start()
    (rig.data / "boundaries-applied.json").unlink()
    (rig.data / "boundaries-applied.json").mkdir()  # a directory where the file goes
    rig.rewrite(MEMORY + SHIP_NAMED + ALLOW2)  # must not raise
    rig.daemon.shutdown()


def test_memory_contract_lists_the_event_in_both_tables():
    text = (Path(__file__).resolve().parent.parent / "docs/contracts/memory.md").read_text()
    rows = [ln for ln in text.splitlines() if ln.startswith("|") and CHANGED in ln]
    assert len(rows) == 2

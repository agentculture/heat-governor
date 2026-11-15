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

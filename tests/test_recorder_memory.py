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

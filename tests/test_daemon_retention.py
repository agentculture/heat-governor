"""The daemon runs record retention on its own thread (record-retention t7).

Everything is local: tmp data dirs, a fake wall clock the tests move across UTC midnight, a
fake or a slowed-down prune pass. Nothing under the real state directory is touched.
"""

from __future__ import annotations

import functools
import gzip
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from heat_governor import retention
from heat_governor.config import load
from heat_governor.daemon import Daemon, Options, previous_boot_postmortem
from heat_governor.platforms import SPARK
from heat_governor.recorder import iter_records
from heat_governor.sense import Sensor
from tests.fakes import SPARK_ZONES, make_sys

DAY = 86400.0
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp()  # noon UTC
CHECK_S = 0.01  # how often the retention thread looks at the clock in these tests


class FakeClock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _empty() -> dict:
    return {"deleted": [], "compressed": [], "bytes_freed": 0, "errors": [], "warnings": []}


class FakePrune:
    """Records each call (thread, now, keep_days); returns ``result`` or raises ``error``."""

    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, float, int]] = []
        self.result = result or _empty()
        self.error = error

    def __call__(self, records_dir, now, keep_days, **kw) -> dict:
        self.calls.append((threading.current_thread().name, now, keep_days))
        if self.error is not None:
            raise self.error
        return dict(self.result)


class Rig:
    def __init__(self, tmp_path: Path, config_text: str = "", *, prune=None, record=True):
        self.tmp = tmp_path
        self.data = tmp_path / "data"
        self.records = self.data / "records"
        self.cfg_path = tmp_path / "config.toml"
        self.cfg_path.write_text(config_text)
        self.clock = FakeClock()
        self.logs: list[str] = []
        opts = Options(
            state_path=tmp_path / "state.json",
            data_dir=self.data,
            log=self.logs.append,
            config=load(self.cfg_path),
            platform="spark",
            reload_every_s=0.0,
            listeners=False,
            fsync=False,
            record=record,
            prune=prune,
            retention_check_s=CHECK_S,
        )
        self.daemon = Daemon(
            SPARK,
            opts,
            sensor=Sensor(SPARK.zones, sys_root=make_sys(tmp_path, SPARK_ZONES)),
            docker=None,
            clock=self.clock,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "cgsys",
        )

    def rewrite(self, text: str) -> None:
        cfg = self.daemon.opts.config
        self.cfg_path.write_text(text)
        os.utime(self.cfg_path, (cfg.mtime[0] + 5, cfg.mtime[0] + 5))
        self.daemon.maybe_reload(self.clock())

    def rows(self, event: str) -> list[dict]:
        return [r for r in iter_records(self.records) if r.get("event") == event]

    def retention_logs(self) -> list[str]:
        return [m for m in self.logs if "retention" in m]


def _wait(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def _day_name(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d")


def _write_day(records: Path, t: float, n: int = 50) -> Path:
    records.mkdir(parents=True, exist_ok=True)
    path = records / f"{_day_name(t)}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps({"t": t + i, "temp_c": 60.0, "level": "cool"}) + "\n")
    return path


# -- when a pass runs ------------------------------------------------------------------------


def test_a_pass_runs_once_at_start_and_once_per_utc_rollover_on_its_own_thread(tmp_path):
    prune = FakePrune()
    rig = Rig(tmp_path, prune=prune)
    rig.daemon.start()
    try:
        assert _wait(lambda: len(prune.calls) == 1)
        name, now, keep = prune.calls[0]
        assert name != threading.current_thread().name
        assert (now, keep) == (T0, 7)
        rig.clock.t = T0 + 3600  # same UTC day: no new pass
        time.sleep(20 * CHECK_S)
        assert len(prune.calls) == 1
        rig.clock.t = T0 + 12 * 3600 + 1  # past midnight: one pass for the new day
        assert _wait(lambda: len(prune.calls) == 2)
        time.sleep(20 * CHECK_S)
        assert len(prune.calls) == 2
        assert prune.calls[1][1] == T0 + 12 * 3600 + 1
        rig.clock.t += DAY  # and the next day again
        assert _wait(lambda: len(prune.calls) == 3)
    finally:
        rig.daemon.shutdown()
    thread = rig.daemon.retention_thread
    assert thread is not None, "a retention thread was started"
    assert not thread.is_alive(), "stopped with the daemon"


def test_the_start_pass_runs_after_todays_start_row(tmp_path):
    """Down for days: the only day file is old. The start row must exist first, or the
    pass's clock check would see a stale newest file and refuse to do anything."""
    rig = Rig(tmp_path, prune=retention.prune_pass)
    old = _write_day(rig.records, T0 - 3 * DAY)
    rig.daemon.start()
    try:
        assert _wait(lambda: not old.exists())
        rig.daemon.tick()
        (row,) = rig.rows("records_pruned")
        assert row["compressed"] == [old.name]
        assert row["warnings"] == []
        assert (rig.records / f"{old.name}.gz").exists()
    finally:
        rig.daemon.shutdown()


def test_no_recorder_means_no_retention_thread(tmp_path):
    prune = FakePrune()
    rig = Rig(tmp_path, prune=prune, record=False)
    rig.daemon.start()
    rig.daemon.tick()
    rig.daemon.shutdown()
    assert prune.calls == []
    assert rig.daemon.retention_thread is None


# -- the tick loop does not wait for it ---------------------------------------------------------


def test_ticks_keep_their_cadence_while_a_slow_fake_compressor_runs(tmp_path):
    started, release = threading.Event(), threading.Event()

    def slow_compress(path):
        started.set()
        release.wait(10)
        return retention.compress_day(path)

    rig = Rig(tmp_path, prune=functools.partial(retention.prune_pass, compress=slow_compress))
    _write_day(rig.records, T0 - 3 * DAY)
    rig.daemon.start()
    try:
        assert started.wait(5)
        durations = []
        for i in range(10):
            rig.clock.t = T0 + i
            t0 = time.monotonic()
            rig.daemon.tick()
            durations.append(time.monotonic() - t0)
        assert not release.is_set()
        assert rig.daemon.retention_thread.is_alive()
        assert max(durations) < 0.5, durations
        ticks = [r for r in iter_records(rig.records) if "temp_c" in r and "event" not in r]
        assert len([r for r in ticks if r["t"] >= T0]) == 10
        assert rig.rows("records_pruned") == [], "the pass has not finished yet"
    finally:
        release.set()
    assert _wait(lambda: rig.daemon.retention_pending() > 0)
    rig.daemon.tick()
    assert len(rig.rows("records_pruned")) == 1
    rig.daemon.shutdown()


# -- what a pass writes ---------------------------------------------------------------------


def test_a_pass_that_changed_something_writes_one_row_and_one_log_line(tmp_path):
    rig = Rig(tmp_path, prune=retention.prune_pass)
    expired = _write_day(rig.records, T0 - 10 * DAY)
    old = _write_day(rig.records, T0 - 3 * DAY)
    size = expired.stat().st_size
    rig.daemon.start()
    try:
        assert _wait(lambda: rig.daemon.retention_pending() > 0)
        rig.daemon.tick()
        rig.daemon.tick()
    finally:
        rig.daemon.shutdown()
    rows = rig.rows("records_pruned")
    assert len(rows) == 1
    row = rows[0]
    assert set(row) >= {"t", "event", "deleted", "compressed", "bytes_freed", "errors"}
    assert row["deleted"] == [expired.name]
    assert row["compressed"] == [old.name]
    assert row["bytes_freed"] >= size
    assert row["errors"] == []
    assert row["warnings"] == []
    assert row["t"] == T0
    logs = rig.retention_logs()
    assert len(logs) == 1, logs
    assert "deleted=1" in logs[0]
    assert "compressed=1" in logs[0]


def test_a_pass_that_changed_nothing_writes_no_row(tmp_path):
    prune = FakePrune()
    rig = Rig(tmp_path, prune=prune)
    rig.daemon.start()
    try:
        assert _wait(lambda: rig.daemon.retention_pending() == 1)
        rig.daemon.tick()
        assert rig.daemon.retention_pending() == 0
    finally:
        rig.daemon.shutdown()
    assert rig.rows("records_pruned") == []
    assert rig.retention_logs() == []


def test_a_warning_alone_is_recorded(tmp_path):
    result = {**_empty(), "warnings": ["clock jumped"]}
    rig = Rig(tmp_path, prune=FakePrune(result))
    rig.daemon.start()
    try:
        assert _wait(lambda: rig.daemon.retention_pending() > 0)
        rig.daemon.tick()
    finally:
        rig.daemon.shutdown()
    (row,) = rig.rows("records_pruned")
    assert row["warnings"] == ["clock jumped"]
    (line,) = rig.retention_logs()
    assert "clock jumped" in line


def test_an_exception_inside_the_pass_is_logged_and_the_daemon_keeps_ticking(tmp_path):
    prune = FakePrune(error=RuntimeError("disk on fire"))
    rig = Rig(tmp_path, prune=prune)
    rig.daemon.start()
    try:
        assert _wait(lambda: len(prune.calls) == 1)
        assert _wait(lambda: rig.daemon.retention_pending() > 0)
        for i in range(3):
            rig.clock.t = T0 + i
            rig.daemon.tick()
        logs = rig.retention_logs()
        assert len(logs) == 1, logs
        assert "disk on fire" in logs[0], logs
        assert rig.rows("records_pruned") == []
        rig.clock.t = T0 + DAY  # the thread survived: the next day runs a pass again
        assert _wait(lambda: len(prune.calls) == 2)
        assert rig.daemon.retention_thread.is_alive()
    finally:
        rig.daemon.shutdown()
    ticks = [r for r in iter_records(rig.records) if "temp_c" in r and "event" not in r]
    assert len(ticks) == 3


def test_a_result_still_queued_at_shutdown_is_written(tmp_path):
    rig = Rig(tmp_path, prune=FakePrune({**_empty(), "deleted": ["20200101.jsonl"]}))
    rig.daemon.start()
    assert _wait(lambda: rig.daemon.retention_pending() > 0)
    rig.daemon.shutdown()
    assert len(rig.rows("records_pruned")) == 1


# -- live reload ----------------------------------------------------------------------------


def test_a_reload_that_changes_keep_days_is_used_by_the_next_pass(tmp_path):
    prune = FakePrune()
    rig = Rig(tmp_path, "[daemon]\nkeep_days = 7\n", prune=prune)
    rig.daemon.start()
    try:
        assert _wait(lambda: len(prune.calls) == 1)
        rig.rewrite("[daemon]\nkeep_days = 2\n")
        assert not any("restart" in m and "keep_days" in m for m in rig.logs)
        rig.clock.t = T0 + DAY
        assert _wait(lambda: len(prune.calls) == 2)
    finally:
        rig.daemon.shutdown()
    assert [c[2] for c in prune.calls] == [7, 2]


# -- the postmortem reads raw and gzipped day files ---------------------------------------------


def _previous_boot_rows(t0: float) -> list[dict]:
    rows = [{"t": t0, "event": "start", "boot_id": "boot-old", "pid": 1}]
    rows += [{"t": t0 + i, "temp_c": 70.0 + i % 20, "level": "cool"} for i in range(1, 40)]
    return rows


def _store(records: Path, day: str, rows: list[dict], *, gz: bool) -> None:
    records.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
    if gz:
        with gzip.open(records / f"{day}.jsonl.gz", "wt", encoding="utf-8") as fh:
            fh.write(text)
    else:
        (records / f"{day}.jsonl").write_text(text, encoding="utf-8")


def _fixture(records: Path, *, gz_older: bool, gz_newer: bool) -> None:
    older, newer = T0 - 2 * DAY, T0 - DAY
    _store(records, "20200101", [{"t": 1.0, "event": "start", "boot_id": "x"}], gz=True)
    _store(records, _day_name(older), _previous_boot_rows(older), gz=gz_older)
    ticks = [{"t": newer + i, "temp_c": 80.0, "level": "warm"} for i in range(10)]
    ticks.insert(5, {"t": newer + 5, "event": "memory_kill", "pid": 9, "comm": "hog"})
    _store(records, _day_name(newer), ticks, gz=gz_newer)


def test_the_postmortem_reads_the_newest_days_raw_or_gzipped(tmp_path):
    entries = []
    for gz_older, gz_newer in ((False, False), (True, False), (True, True)):
        records = tmp_path / f"r-{gz_older}-{gz_newer}" / "records"
        _fixture(records, gz_older=gz_older, gz_newer=gz_newer)
        entries.append(previous_boot_postmortem(records, "boot-new", T0, T0 - 60, pid=2))
    assert entries[0] is not None
    assert entries[0]["previous_boot"] == "boot-old"
    assert [e["event"] for e in entries[0]["events"]] == ["memory_kill"]
    assert entries[1] == entries[0]
    assert entries[2] == entries[0]


def test_the_postmortem_reads_a_day_once_when_both_suffixes_exist(tmp_path):
    raw = tmp_path / "raw" / "records"
    _fixture(raw, gz_older=False, gz_newer=False)
    both = tmp_path / "both" / "records"
    shutil.copytree(raw, both)
    newer = both / f"{_day_name(T0 - DAY)}.jsonl"
    retention.compress_day(newer)
    shutil.copy(raw / newer.name, newer)  # a crash between rename and unlink: both remain
    a = previous_boot_postmortem(raw, "boot-new", T0, T0 - 60, pid=2)
    b = previous_boot_postmortem(both, "boot-new", T0, T0 - 60, pid=2)
    assert a is not None
    assert b == a


def test_a_forward_clock_jump_deletes_nothing_through_the_daemon(tmp_path):
    """Review of PR 23, M1: the daemon writes today's file before a pass, so the newest-file
    check alone cannot see a jump; the hole between the real days and the new ones does."""
    rig = Rig(tmp_path, prune=retention.prune_pass)
    rig.records.mkdir(parents=True)
    real = [f"202611{d}.jsonl" for d in range(18, 26)]
    for name in real:
        (rig.records / name).write_text('{"t":1}\n')
    rig.clock.t = datetime(2030, 1, 1, 12, tzinfo=timezone.utc).timestamp()
    rig.daemon.start()
    try:
        assert _wait(lambda: rig.daemon.retention_pending() > 0)
        rig.daemon.tick()
    finally:
        rig.daemon.shutdown()
    left = {p.name.split(".")[0] for p in rig.records.iterdir()}
    for name in real:
        assert name.split(".")[0] in left
    rows = rig.rows("records_pruned")
    assert rows
    assert any("gap" in w for w in rows[-1]["warnings"])

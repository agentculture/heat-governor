"""The daemon wires the memory guard and the shipper (task t9).

Everything is fake: /proc and the cgroup /sys are tmp trees, the killer's pidfd calls are
recorded (never sent), the shipper is an in-memory stand-in, no network. The thermal path
must not notice the guard at all: level, admit and concurrency_factor come out identical
with and without ``[memory]``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from heat_governor import client
from heat_governor.config import load
from heat_governor.daemon import Daemon, Options
from heat_governor.memguard import MemGuard
from heat_governor.memkill import MemKiller
from heat_governor.platforms import SPARK
from heat_governor.sense import Sensor
from heat_governor.state import read_state
from tests.fakes import SPARK_ZONES, make_sys
from tests.fakes_proc import add_proc
from tests.test_memguard import RATE_MIB_S, UID, Clock, FakePidfd, write_meminfo, write_vmstat

ME = 500
THIS_BOOT = "boot-new"
NOW = time.time()  # the daemon's wall clock in these tests (records land in today's file)
SELF_CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/heat-governor.service"

MEMORY = '[memory]\naction = "kill"\n'
SHIP = '[ship]\nenabled = true\nendpoint = "http://127.0.0.1:9"\nbucket = "fleet-box"\n'
SHIP_NAMED = SHIP + 'machine = "box1"\n'


class FakeShipper:
    def __init__(self, rule, **kw) -> None:
        self.rule = rule
        self.kw = kw
        self.submitted: list[tuple[str, dict]] = []
        self.postmortems: list[dict] = []
        self.postmortem_boots: list[str | None] = []
        self.started = self.stopped = False

    def submit(self, kind: str, record: dict, *, t: float | None = None) -> str:
        self.submitted.append((kind, record))
        return f"key/{kind}"

    def submit_postmortem(self, record: dict, *, boot_id: str | None = None) -> str:
        self.postmortems.append(record)
        self.postmortem_boots.append(boot_id)
        return "key/postmortem"

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True


class Rig:
    """One daemon on fake trees; ``guards`` / ``shippers`` collect everything it built."""

    def __init__(self, tmp_path: Path, config_text: str, *, threads: bool = False) -> None:
        self.tmp = tmp_path
        self.proc = tmp_path / "proc"
        self.cg_sys = tmp_path / "cgsys"
        self.data = tmp_path / "data"
        write_meminfo(self.proc, 60_000)
        write_vmstat(self.proc, 0)
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text(THIS_BOOT + "\n")
        (self.proc / "stat").write_text(f"cpu  1 2 3 4\nbtime {int(NOW) - 120}\n")
        (self.proc / "self").mkdir()
        (self.proc / "self/cgroup").write_text(f"0::{SELF_CGROUP}\n")
        add_proc(self.proc, 1, ppid=0, uid=0, comm="systemd")
        add_proc(self.proc, 400, ppid=1, comm="bash")
        add_proc(self.proc, ME, ppid=400, comm="heat-governor")
        add_proc(self.proc, 777, ppid=400, comm="hog", anon_kib=20_000 * 1024, start=4242)
        self.cfg_path = tmp_path / "config.toml"
        self.cfg_path.write_text(config_text)
        self.clock = Clock()
        self.pidfd = FakePidfd()
        self.guards: list[MemGuard] = []
        self.shippers: list[FakeShipper] = []
        self.logs: list[str] = []
        self.threads = threads
        self.shipper_error: Exception | None = None
        sysr = make_sys(tmp_path, SPARK_ZONES)
        opts = Options(
            state_path=tmp_path / "state.json",
            data_dir=self.data,
            log=self.logs.append,
            config=load(self.cfg_path),
            platform="spark",
            reload_every_s=0.0,
            listeners=False,
            fsync=False,
        )
        self.daemon = Daemon(
            SPARK,
            opts,
            sensor=Sensor(SPARK.zones, sys_root=sysr),
            docker=None,
            clock=lambda: NOW,
            proc_root=self.proc,
            sys_root=self.cg_sys,
            make_guard=self.make_guard,
            make_shipper=self.make_shipper,
        )

    def make_guard(self, rule, **kw) -> MemGuard:
        killer = MemKiller(
            rule,
            proc_root=self.proc,
            clock=self.clock.monotonic,
            uid=UID,
            self_pid=ME,
            pidfd_open=self.pidfd.open,
            pidfd_send_signal=self.pidfd.send,
            close=lambda fd: None,
        )
        guard = MemGuard(
            rule,
            clock=self.clock.wall,
            monotonic=self.clock.monotonic,
            killer=killer,
            mlockall=lambda flags: 0,
            uid=UID,
            self_pid=ME,
            **kw,
        )
        if not self.threads:
            guard.start = lambda: None  # the test drives step() itself
        self.guards.append(guard)
        return guard

    def make_shipper(self, rule, **kw) -> FakeShipper:
        if self.shipper_error is not None:
            raise self.shipper_error
        s = FakeShipper(rule, **kw)
        self.shippers.append(s)
        return s

    @property
    def guard(self) -> MemGuard:
        return self.guards[-1]

    def wave(self, start_mib: float = 40_000.0, stop_mib: float = 0.0) -> None:
        """Available memory falling at 1.2 GB/s, one guard poll per poll_s."""
        avail = start_mib
        while avail > stop_mib and not self.pidfd.sent:
            write_meminfo(self.proc, avail)
            self.guard.step()
            self.clock.mono += self.guard.rule.poll_s
            avail -= RATE_MIB_S * self.guard.rule.poll_s
        self.daemon.memsink.flush()  # guard events are written on the sink thread (review #5)

    def rows(self) -> list[dict]:
        self.daemon.memsink.flush()
        out: list[dict] = []
        for path in sorted((self.data / "records").glob("*.jsonl")):
            out += [json.loads(line) for line in path.read_text().splitlines()]
        return out

    def rewrite(self, text: str) -> None:
        cfg = self.daemon.opts.config
        self.cfg_path.write_text(text)
        os.utime(self.cfg_path, (cfg.mtime[0] + 5, cfg.mtime[0] + 5))
        self.daemon.maybe_reload(NOW)


def _events(rows: list[dict], name: str) -> list[dict]:
    return [r for r in rows if r.get("event") == name]


# -- kill -> row + ship ---------------------------------------------------------------------


def test_kill_writes_a_memory_kill_row_and_ships_it(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED)
    rig.daemon.start()
    rig.wave()
    assert rig.pidfd.opened == [777]
    assert len(rig.pidfd.sent) == 1
    rows = rig.rows()
    kills = _events(rows, "memory_kill")
    assert len(kills) == 1
    assert kills[0]["pid"] == 777
    assert kills[0]["comm"] == "hog"
    assert not _events(rows, "memory_top"), "top snapshots are not rows"
    shipped = rig.shippers[-1].submitted
    assert [k for k, _ in shipped] == ["memory_kill"]
    assert shipped[0][1]["pid"] == 777
    rig.daemon.shutdown()


def test_record_mode_writes_would_kill_but_does_not_ship_it(tmp_path):
    rig = Rig(tmp_path, '[memory]\naction = "record"\n' + SHIP_NAMED)
    rig.daemon.start()
    rig.wave(stop_mib=500)
    assert rig.pidfd.sent == []
    assert len(_events(rig.rows(), "memory_would_kill")) == 1
    assert rig.shippers[-1].submitted == []


def test_oom_kill_and_budget_exhausted_are_recorded_and_shipped(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED)
    rig.daemon.start()
    rig.daemon._on_memory_event({"t": NOW, "event": "oom_kill", "source": "vmstat", "delta": 1})
    rig.daemon._on_memory_event({"t": NOW, "event": "memory_budget_exhausted", "kills": 3})
    rows = rig.rows()
    assert len(_events(rows, "oom_kill")) == 1
    assert len(_events(rows, "memory_budget_exhausted")) == 1
    kinds = [k for k, _ in rig.shippers[-1].submitted]
    assert kinds == ["oom_kill", "memory_budget_exhausted"]


def test_latest_top_snapshot_goes_on_the_next_tick_row_once(tmp_path):
    rig = Rig(tmp_path, MEMORY)
    rig.daemon.start()
    write_meminfo(rig.proc, 10_000)  # under soft_pct: a snapshot every poll
    rig.guard.step()
    rig.clock.mono += 0.25
    rig.guard.step()
    rig.daemon.tick()
    rig.daemon.tick()
    ticks = [r for r in rig.rows() if "temp_c" in r and "event" not in r]
    assert len(ticks) == 2
    assert [p["comm"] for p in ticks[0]["top"]][0] == "hog"
    assert "top" not in ticks[1], "a snapshot is attached once, then cleared"


def test_routing_failures_never_reach_the_guard_thread(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED)
    rig.daemon.start()

    def boom(*a, **kw):
        raise RuntimeError("bucket on fire")

    rig.shippers[-1].submit = boom
    rig.wave()
    assert len(rig.pidfd.sent) == 1
    assert rig.guard._errors == set(), "the guard saw no exception from emit"
    assert any("bucket on fire" in line for line in rig.logs)


# -- the thermal path does not read memory ------------------------------------------------


def test_level_admit_and_factor_identical_with_and_without_memory(tmp_path):
    with_mem = Rig(tmp_path / "a", MEMORY)
    without = Rig(tmp_path / "b", "")
    for rig in (with_mem, without):
        rig.daemon.start()
    with_mem.wave()  # the guard fired and killed on one side only
    assert with_mem.pidfd.sent
    write_meminfo(with_mem.proc, 60_000)  # same meminfo for the sensors block on both sides
    da, db = with_mem.daemon.tick(), without.daemon.tick()
    assert (da.level, da.admit, da.concurrency_factor) == (
        db.level,
        db.admit,
        db.concurrency_factor,
    )
    a, b = read_state(tmp_path / "a/state.json"), read_state(tmp_path / "b/state.json")
    assert "mem_guard" in a
    assert "mem_guard" not in b
    for doc in (a, b):
        doc.pop("mem_guard", None)
        doc.pop("config")  # each rig's own config path
    assert a == b


# -- the state doc ------------------------------------------------------------------------


def test_state_doc_has_mem_guard_only_with_memory_and_clients_still_read_it(tmp_path):
    rig = Rig(tmp_path / "m", MEMORY)
    rig.daemon.start()
    rig.wave()
    rig.daemon.clock = time.time  # a fresh doc for the client's staleness check
    rig.daemon.tick()
    st = read_state(rig.daemon.opts.state_path)
    mg = st["mem_guard"]
    assert mg["action"] == "kill"
    assert mg["last_kill"]["pid"] == 777
    assert set(mg) >= {"action", "armed", "avail_mib", "last_kill", "kills_last_hour"}
    path = str(rig.daemon.opts.state_path)
    adm = client.read(path)
    assert adm.level == st["level"]
    assert adm.admit == st["admit"]
    assert client.wait(path, timeout=1, poll_s=0.05).admit == st["admit"]
    if st["admit"]:
        with client.Gate(2, path, poll_s=0.05).slot() as slot:
            assert slot.level == st["level"]
    plain = Rig(tmp_path / "p", "")
    plain.daemon.start()
    plain.daemon.tick()
    assert "mem_guard" not in read_state(plain.daemon.opts.state_path)
    assert plain.guards == []
    assert plain.shippers == []


def test_no_memory_section_means_no_guard_and_no_top(tmp_path):
    rig = Rig(tmp_path, SHIP_NAMED)
    rig.daemon.start()
    rig.daemon.tick()
    assert rig.guards == []
    assert rig.daemon.guard is None
    assert all("top" not in r for r in rig.rows())
    assert len(rig.shippers) == 1, "shipping does not need the guard"


# -- lifecycle and live reload ------------------------------------------------------------


def test_guard_gets_the_daemons_own_cgroup_and_the_fake_roots(tmp_path):
    rig = Rig(tmp_path, MEMORY)
    rig.daemon.start()
    g = rig.guard
    assert g.proc_root == rig.proc
    assert g.sys_root == rig.cg_sys
    assert SELF_CGROUP.strip("/") in g.cgroups


def test_reload_removing_memory_stops_the_guard_thread(tmp_path):
    rig = Rig(tmp_path, MEMORY, threads=True)
    rig.daemon.start()
    first = rig.guard
    assert first._thread is not None
    assert first._thread.is_alive()
    thread = first._thread
    rig.rewrite('[memory]\naction = "record"\n')  # changed: rebuilt
    assert len(rig.guards) == 2
    assert rig.daemon.guard is rig.guards[1]
    assert not thread.is_alive()
    assert rig.daemon.guard.rule.action == "record"
    second = rig.guard._thread
    rig.rewrite("")  # removed: stopped, gone
    assert rig.daemon.guard is None
    assert not second.is_alive()
    rig.daemon.tick()
    assert "mem_guard" not in read_state(rig.daemon.opts.state_path)
    rig.rewrite(MEMORY)  # added back live
    assert rig.daemon.guard is rig.guards[-1]
    assert rig.guard._thread.is_alive()
    rig.daemon.shutdown()
    assert not rig.guard._thread or not rig.guard._thread.is_alive()


def test_unchanged_memory_on_reload_keeps_the_guard(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED)
    rig.daemon.start()
    rig.rewrite(MEMORY + SHIP_NAMED + "[daemon]\npoll_s = 2.0\n")
    assert len(rig.guards) == 1
    assert len(rig.shippers) == 1


def test_reload_rebuilds_and_removes_the_shipper(tmp_path):
    rig = Rig(tmp_path, SHIP_NAMED)
    rig.daemon.start()
    first = rig.shippers[0]
    assert first.started
    rig.rewrite(SHIP_NAMED.replace("box1", "box2"))
    assert first.stopped
    assert len(rig.shippers) == 2
    assert rig.shippers[1].started
    assert rig.shippers[1].rule.machine == "box2"
    rig.rewrite(SHIP_NAMED.replace("enabled = true", "enabled = false"))
    assert rig.shippers[1].stopped
    assert rig.daemon.shipper is None
    rig.rewrite("")
    assert rig.daemon.shipper is None


def test_shutdown_stops_guard_and_shipper(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED, threads=True)
    rig.daemon.start()
    thread = rig.guard._thread
    rig.daemon.shutdown()
    assert not thread.is_alive()
    assert rig.shippers[0].stopped


def test_shipper_arguments(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED)
    rig.daemon.start()
    kw = rig.shippers[0].kw
    assert kw["boot_id"] == THIS_BOOT
    assert kw["spool_dir"] == rig.data / "spool"
    assert kw["records_dir"] == rig.data / "records"
    assert kw["default_machine"] == "box1"
    low = kw["low_memory"]
    assert low() is False
    write_meminfo(rig.proc, 10_000)  # 8 % free is normal on a spark (d3): above its 6 GiB soft
    rig.guard.step()
    assert low() is False
    write_meminfo(rig.proc, 5_000)  # under the spark platform's 6144 MiB soft limit
    rig.guard.step()
    assert low() is True
    write_meminfo(rig.proc, 60_000)
    rig.guard.step()
    assert low() is False


# -- machine name: config, else the local fleet entry, never the hostname ------------------


def test_machine_defaults_to_the_local_fleet_entry(tmp_path):
    (tmp_path / "fleet.toml").write_text(
        '[[machines]]\nname = "spark1"\nhost = "spark1.lan"\n\n'
        '[[machines]]\nname = "elitemini"\ntransport = "local"\n'
    )
    rig = Rig(tmp_path, SHIP)
    rig.daemon.start()
    assert rig.shippers[0].kw["default_machine"] == "elitemini"


def test_no_machine_and_no_local_fleet_entry_disables_shipping(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP)
    rig.daemon.start()
    assert rig.shippers == []
    assert rig.daemon.shipper is None
    want = "set [ship] machine (no local fleet entry); shipping disabled"
    assert any(want in m for m in rig.logs)
    rig.wave()  # the guard still runs and records
    assert len(_events(rig.rows(), "memory_kill")) == 1


def test_shipper_value_error_is_logged_and_the_daemon_runs(tmp_path):
    rig = Rig(tmp_path, MEMORY + SHIP_NAMED)
    rig.shipper_error = ValueError("no ship credentials (AWS_* env vars or credentials profile)")
    rig.daemon.start()
    rig.daemon.tick()
    assert rig.daemon.shipper is None
    assert any("no ship credentials" in m and "shipping disabled" in m for m in rig.logs)


# -- postmortem after an unclean boot -----------------------------------------------------


def _day(t: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(t))


def _write_records(records: Path, rows: list[dict], day: str | None = None) -> None:
    records.mkdir(parents=True, exist_ok=True)
    day = day or _day(rows[0]["t"])
    with open(records / f"{day}.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")


def _previous_boot(t0: float, *, clean: bool, boot: str = "boot-old") -> list[dict]:
    rows = [{"t": t0, "event": "start", "boot_id": boot}]
    rows += [{"t": t0 + i, "temp_c": 70.0 + i, "level": "cool"} for i in range(1, 30)]
    rows.append({"t": t0 + 25, "event": "memory_kill", "pid": 9, "comm": "hog"})
    if clean:
        rows.append({"t": t0 + 31, "event": "action", "msg": "stop: own pauses released"})
    return rows


def test_unclean_previous_boot_submits_a_postmortem_once(tmp_path):
    rig = Rig(tmp_path, SHIP_NAMED)
    _write_records(rig.data / "records", _previous_boot(NOW - 600, clean=False))
    rig.daemon.start()
    pms = rig.shippers[0].postmortems
    assert len(pms) == 1
    pm = pms[0]
    assert pm["previous_boot"] == "boot-old"
    assert pm["new_boot"] == THIS_BOOT
    # review #8: keyed under the crashed boot, where ship fetch looks for it
    assert rig.shippers[0].postmortem_boots == ["boot-old"]
    assert [e["event"] for e in pm["events"]] == ["memory_kill"]
    assert pm["max_temp_c"] == 99.0
    rig.rewrite(SHIP_NAMED.replace("box1", "box2"))  # a reload rebuilds the shipper
    assert len(rig.shippers) == 2
    assert rig.shippers[1].postmortems == []


def test_clean_stop_or_same_boot_submits_no_postmortem(tmp_path):
    clean = Rig(tmp_path / "c", SHIP_NAMED)
    _write_records(clean.data / "records", _previous_boot(NOW - 600, clean=True))
    clean.daemon.start()
    assert clean.shippers[0].postmortems == []
    same = Rig(tmp_path / "s", SHIP_NAMED)
    _write_records(same.data / "records", _previous_boot(NOW - 60, clean=False, boot=THIS_BOOT))
    same.daemon.start()
    assert same.shippers[0].postmortems == []


def test_postmortem_waits_for_a_shipper_added_by_reload(tmp_path):
    rig = Rig(tmp_path, "")
    _write_records(rig.data / "records", _previous_boot(NOW - 600, clean=False))
    rig.daemon.start()
    assert rig.shippers == []
    rig.rewrite(SHIP_NAMED)
    assert len(rig.shippers[0].postmortems) == 1
    rig.rewrite(SHIP_NAMED.replace("box1", "box3"))
    assert rig.shippers[1].postmortems == []


def test_postmortem_reads_only_the_newest_two_day_files(tmp_path):
    rig = Rig(tmp_path, SHIP_NAMED)
    records = rig.data / "records"
    # an old file holds the previous boot's start row; the newest two hold only its ticks
    _write_records(records, [{"t": 1.0, "event": "start", "boot_id": "ancient"}], "20200101")
    for day, base in (("20200102", 86400.0), ("20200103", 2 * 86400.0)):
        rows = [{"t": base + i, "temp_c": 60.0, "level": "cool"} for i in range(5)]
        _write_records(records, rows, day)
    rig.daemon.start()
    pms = rig.shippers[0].postmortems
    assert len(pms) == 1, "ticks older than this kernel's boot time, no clean stop"
    assert pms[0]["previous_boot"] == "unknown", "the 2020-01-01 file was never read"
    assert pms[0]["new_boot"] == THIS_BOOT


def test_ticks_after_this_boot_without_start_row_are_not_a_reboot(tmp_path):
    rig = Rig(tmp_path, SHIP_NAMED)
    rows = [{"t": NOW - 30 + i, "temp_c": 60.0, "level": "cool"} for i in range(5)]
    _write_records(rig.data / "records", rows)  # a crash of this boot's daemon, not a reboot
    rig.daemon.start()
    assert rig.shippers[0].postmortems == []


def test_a_stuck_recorder_never_blocks_the_guard_thread(tmp_path):
    """Review #5: the emit sink hands events to the sink thread; a recorder write stuck in
    fsync (writeback under memory pressure) must not hold up the guard's next poll."""
    import threading
    import time

    rig = Rig(tmp_path, MEMORY)
    rig.daemon.start()
    release = threading.Event()
    real_write = rig.daemon.recorder.write
    rig.daemon.recorder.write = lambda rec: (release.wait(5), real_write(rec))
    try:
        t0 = time.monotonic()
        rig.daemon._on_memory_event({"t": NOW, "event": "oom_kill", "source": "vmstat"})
        assert time.monotonic() - t0 < 0.5
    finally:
        release.set()
    rig.daemon.memsink.flush()
    assert any(r.get("event") == "oom_kill" for r in rig.rows())


def test_reload_keeps_the_kill_budget_and_repeat_history(tmp_path):
    """Review #6: an unrelated [memory] edit rebuilds the guard but must not reset the
    rolling kill budget (a safety stop) or the repeat-offender counts."""
    rig = Rig(tmp_path, MEMORY)
    rig.daemon.start()
    now = rig.clock.monotonic()
    rig.guard.killer.import_state({"kills": (now, now, now), "repeats": {}})
    old = rig.daemon.guard
    rig.rewrite(MEMORY + "top_n = 5\n")
    assert rig.daemon.guard is not old  # rebuilt
    assert rig.daemon.guard.rule.top_n == 5
    assert rig.daemon.guard.killer.kills_last_hour(now) == 3


# -- a boundary-only save keeps the guard's window and budget (t9) -----------------------------

ALLOW = '\n[[memory.allow]]\nname = "vllm"\nmax_mib = 90000\ncomm = ["vllm"]\n'
ALLOW_OTHER = '\n[[memory.allow]]\nname = "db"\nmax_mib = 1000\ncomm = ["db"]\n'
ALLOW2 = ALLOW + '\n[[memory.allow]]\nname = "hog"\nmax_mib = 30000\ncomm = ["hog"]\n'


def _poll_to(rig: Rig, avail: float, polls: int) -> float:
    for _ in range(polls):
        write_meminfo(rig.proc, avail)
        rig.guard.step()
        rig.clock.mono += rig.guard.rule.poll_s
        avail -= RATE_MIB_S * rig.guard.rule.poll_s
    return avail


def test_boundary_only_save_keeps_the_trigger_window(tmp_path):
    rig = Rig(tmp_path, MEMORY + ALLOW)
    rig.daemon.start()
    _poll_to(rig, 40_000.0, 6)
    guard, trigger = rig.guard, rig.guard._trigger
    window = list(trigger._win)
    assert window
    rig.rewrite(MEMORY + ALLOW2)
    assert len(rig.guards) == 1
    assert rig.guard is guard
    assert guard._trigger is trigger
    assert list(trigger._win) == window
    assert [a.name for a in guard.rule.allow] == ["vllm", "hog"]
    assert [a.name for a in guard.killer.rule.allow] == ["vllm", "hog"]
    assert [b["name"] for b in guard.doc()["boundaries"]] == ["vllm", "hog"]
    rig.daemon.shutdown()


def test_boundary_only_save_keeps_the_kill_budget(tmp_path):
    rig = Rig(tmp_path, MEMORY + ALLOW)
    rig.daemon.start()
    rig.wave()
    assert len(rig.pidfd.sent) == 1
    guard = rig.guard
    before = guard.killer.export_state()
    assert before["kills"]
    rig.rewrite(MEMORY + ALLOW2)
    assert rig.guard is guard
    assert guard.killer.export_state() == before
    assert guard.killer.kills_last_hour(rig.clock.monotonic()) == 1
    rig.daemon.shutdown()


def _first_kill_avail(tmp_path, save_mid: bool) -> float:
    rig = Rig(tmp_path, MEMORY + ALLOW)
    rig.daemon.start()
    seen: dict = {}
    rig.pidfd.on_send = lambda: seen.setdefault("avail", cur["avail"])
    cur = {"avail": 40_000.0}
    for i in range(400):
        if seen:
            break
        if save_mid and i == 4:
            rig.rewrite(MEMORY + ALLOW + ALLOW_OTHER)
        write_meminfo(rig.proc, cur["avail"])
        rig.guard.step()
        rig.clock.mono += rig.guard.rule.poll_s
        cur["avail"] -= RATE_MIB_S * rig.guard.rule.poll_s
    rig.daemon.shutdown()
    return seen["avail"]


def test_runaway_fires_no_later_with_a_boundary_save_midway(tmp_path):
    plain = _first_kill_avail(tmp_path / "a", False)
    saved = _first_kill_avail(tmp_path / "b", True)
    assert saved >= plain


def test_other_memory_field_change_still_rebuilds_the_guard(tmp_path):
    rig = Rig(tmp_path, MEMORY + ALLOW)
    rig.daemon.start()
    first = rig.guard
    rig.rewrite(MEMORY + "poll_s = 0.5\n" + ALLOW2)
    assert len(rig.guards) == 2
    assert rig.guard is not first
    assert rig.guard.rule.poll_s == 0.5
    rig.daemon.shutdown()


def test_boundary_change_with_another_field_changed_rebuilds(tmp_path):
    rig = Rig(tmp_path, MEMORY + ALLOW)
    rig.daemon.start()
    rig.rewrite(MEMORY.replace("kill", "record") + ALLOW2)
    assert len(rig.guards) == 2
    rig.daemon.shutdown()

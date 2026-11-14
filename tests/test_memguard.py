"""memguard: the guard poll loop over a fake /proc + /sys tree, with fake pidfd and clock.

No test signals a real process or reads the real /proc or /sys: every path is under
``tmp_path`` and the killer gets fake pidfd callables. The only real-thread tests run the
loop for a fraction of a second against the fake tree.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from heat_governor import memguard
from heat_governor.config import MemoryRule
from heat_governor.memkill import MemKiller
from tests.fakes_proc import add_proc

MIB = 1024  # KiB per MiB
UID = 1000
ME = 500
TOTAL_MIB = 122_880  # ~120 GiB, a Spark-sized box
EPOCH = 1_763_100_000.0
RATE_MIB_S = 1.2e9 / 2**20  # 1.2 GB/s


class Clock:
    def __init__(self) -> None:
        self.mono = 1000.0

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> float:
        return EPOCH + self.mono


class FakePidfd:
    def __init__(self) -> None:
        self.opened: list[int] = []
        self.sent: list[tuple[int, int]] = []
        self.on_send = None

    def open(self, pid: int, flags: int = 0) -> int:
        self.opened.append(pid)
        return 9000 + pid

    def send(self, fd: int, sig: int, siginfo=None, flags: int = 0) -> None:
        self.sent.append((fd, sig))
        if self.on_send is not None:
            self.on_send()


def write_meminfo(proc: Path, avail_mib: float, total_mib: float = TOTAL_MIB) -> None:
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text(
        f"MemTotal:       {int(total_mib * MIB)} kB\n"
        f"MemFree:        {int(avail_mib * MIB) // 2} kB\n"
        f"MemAvailable:   {int(avail_mib * MIB)} kB\n"
    )


def write_vmstat(proc: Path, oom_kill: int) -> None:
    (proc / "vmstat").write_text(f"pgfault 123\noom_kill {oom_kill}\npswpin 0\n")


def write_events(sys_root: Path, cgroup: str, oom: int, oom_kill: int) -> None:
    d = sys_root / "fs" / "cgroup" / cgroup
    d.mkdir(parents=True, exist_ok=True)
    (d / "memory.events").write_text(
        f"low 0\nhigh 0\nmax 3\noom {oom}\noom_kill {oom_kill}\noom_group_kill 0\n"
    )


def make(tmp_path: Path, action: str = "kill", **kw):
    proc, sys_root = tmp_path / "proc", tmp_path / "sys"
    write_meminfo(proc, 60_000)
    add_proc(proc, 1, ppid=0, uid=0, comm="systemd")
    add_proc(proc, 400, ppid=1, comm="bash")
    add_proc(proc, ME, ppid=400, comm="heat-governor")
    add_proc(proc, 777, ppid=400, comm="hog", anon_kib=20_000 * MIB, start=4242)
    rule = MemoryRule(action=action, **kw)
    clock, fake, events, logs = Clock(), FakePidfd(), [], []
    killer = MemKiller(
        rule,
        proc_root=proc,
        clock=clock.monotonic,
        uid=UID,
        self_pid=ME,
        pidfd_open=fake.open,
        pidfd_send_signal=fake.send,
        close=lambda fd: None,
    )
    guard = memguard.MemGuard(
        rule,
        proc_root=proc,
        sys_root=sys_root,
        clock=clock.wall,
        monotonic=clock.monotonic,
        killer=killer,
        emit=events.append,
        mlockall=lambda flags: 0,
        log=logs.append,
        uid=UID,
        self_pid=ME,
    )
    return guard, clock, fake, events, logs, proc, sys_root


def falling_wave(guard, clock, proc, fake=None, start_mib=40_000.0, stop_mib=0.0):
    """Feed avail falling at 1.2 GB/s, one poll per poll_s. Returns avail at the first send."""
    seen = {}
    if fake is not None:
        fake.on_send = lambda: seen.setdefault("avail", current["avail"])
    current = {"avail": start_mib}
    while current["avail"] > stop_mib:
        write_meminfo(proc, current["avail"])
        guard.step()
        clock.mono += guard.rule.poll_s
        current["avail"] -= RATE_MIB_S * guard.rule.poll_s
    return seen.get("avail")


def named(events, name):
    return [e for e in events if e.get("event") == name]


# --- trigger -> kill / record ----------------------------------------------------------------


def test_fires_and_kills_before_avail_reaches_2048_mib(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(tmp_path)
    at = falling_wave(guard, clock, proc, fake)
    assert at is not None, "the injected kill was never called"
    assert at >= 2048
    assert fake.opened == [777]
    assert len(fake.sent) == 1
    kills = named(events, "memory_kill")
    assert len(kills) == 1
    k = kills[0]
    assert k["pid"] == 777 and k["comm"] == "hog" and k["signal"] == "SIGKILL"
    assert k["t"] >= EPOCH  # re-stamped to wall clock, not memkill's monotonic clock
    assert k["avail_mib"] >= 2048 and k["rate_mib_s"] > 1000


def test_record_mode_emits_would_kill_and_never_kills(tmp_path):
    guard, clock, fake, events, logs, proc, _ = make(tmp_path, action="record")
    falling_wave(guard, clock, proc, fake)
    would = named(events, "memory_would_kill")
    assert len(would) == 1
    assert would[0]["pid"] == 777 and would[0]["why_not"] == "record-only"
    assert would[0]["t"] >= EPOCH
    assert not named(events, "memory_kill")
    assert fake.opened == [] and fake.sent == []
    assert guard.doc()["last_kill"] is None


def test_one_fire_per_wave_and_rearm_after_recovery(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(tmp_path, action="record")
    falling_wave(guard, clock, proc, stop_mib=1000)
    assert guard.doc()["armed"] is False
    write_meminfo(proc, 60_000)
    guard.step()
    assert guard.doc()["armed"] is True
    clock.mono += 30
    falling_wave(guard, clock, proc, stop_mib=1000)
    assert len(named(events, "memory_would_kill")) == 2


def test_doc_after_kill_matches_state_row(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(tmp_path)
    falling_wave(guard, clock, proc, fake, stop_mib=3000)
    d = guard.doc()
    keys = {"action", "armed", "avail_mib", "last_kill", "kills_last_hour", "limits"}  # d3
    assert set(d) == keys
    assert d["limits"]["source"] == "percent" and d["limits"]["soft_mib"] > d["limits"]["floor_mib"]
    assert d["action"] == "kill"
    assert d["armed"] is False
    assert isinstance(d["avail_mib"], int)
    assert set(d["last_kill"]) == {"t", "pid", "comm", "rss_mib", "repeat"}
    assert d["last_kill"]["pid"] == 777 and d["last_kill"]["repeat"] == 1
    assert d["last_kill"]["t"] >= EPOCH
    assert d["kills_last_hour"] == 1
    d["last_kill"]["pid"] = -1  # a copy: mutating it never touches the guard
    assert guard.doc()["last_kill"]["pid"] == 777


# --- top snapshots / scan cadence -----------------------------------------------------------


def test_top_snapshot_every_top_every_s_when_calm(tmp_path, monkeypatch):
    guard, clock, _, events, _, proc, _ = make(tmp_path, top_every_s=10.0)
    scans = []
    real = memguard.memprocs.scan
    monkeypatch.setattr(memguard.memprocs, "scan", lambda root: scans.append(1) or real(root))
    for _ in range(81):  # 20 s at 0.25 s
        guard.step()
        clock.mono += 0.25
    tops = named(events, "memory_top")
    assert len(tops) == 3  # t=0, 10, 20
    assert len(scans) == 3  # no scan between routine snapshots while calm
    top = tops[0]
    assert set(top) >= {"t", "event", "avail_mib", "total_mib", "top"}
    assert top["t"] >= EPOCH and top["top"][0]["pid"] == 777


def test_top_snapshot_every_poll_while_below_soft(tmp_path):
    guard, clock, _, events, _, proc, _ = make(tmp_path, action="record")
    write_meminfo(proc, TOTAL_MIB * 0.15)  # below soft_pct=20, steady (no fire)
    for _ in range(8):
        guard.step()
        clock.mono += 0.25
    assert len(named(events, "memory_top")) == 8
    assert not named(events, "memory_would_kill")


# --- OOM counters ----------------------------------------------------------------------------


def test_oom_counter_deltas_emit_one_event_each(tmp_path):
    guard, clock, _, events, _, proc, sys_root = make(tmp_path)
    write_vmstat(proc, 2)
    write_events(sys_root, "user.slice/user-1000.slice", oom=5, oom_kill=2)
    guard.step()  # baseline: nothing emitted
    assert not named(events, "oom_kill")
    guard.step()  # unchanged: nothing
    assert not named(events, "oom_kill")

    write_vmstat(proc, 3)
    write_events(sys_root, "user.slice/user-1000.slice", oom=6, oom_kill=3)
    clock.mono += 0.25
    guard.step()
    ooms = named(events, "oom_kill")
    assert len(ooms) == 2
    by = {e["source"]: e for e in ooms}
    assert by["vmstat"] == {
        "t": EPOCH + clock.mono,
        "event": "oom_kill",
        "source": "vmstat",
        "delta": 1,
        "total": 3,
    }
    assert by["user.slice/user-1000.slice"]["delta"] == 1
    assert by["user.slice/user-1000.slice"]["total"] == 3

    write_vmstat(proc, 6)
    guard.step()
    ooms = named(events, "oom_kill")
    assert len(ooms) == 3
    assert ooms[-1]["source"] == "vmstat" and ooms[-1]["delta"] == 3


def test_extra_cgroups_watched_and_missing_files_skipped(tmp_path):
    proc, sys_root = tmp_path / "proc", tmp_path / "sys"
    write_meminfo(proc, 60_000)
    events = []
    guard = memguard.MemGuard(
        MemoryRule(),
        proc_root=proc,
        sys_root=sys_root,
        emit=events.append,
        uid=UID,
        self_pid=ME,
        cgroups=("/system.slice/heat-governor.service/", "nope.slice"),
        log=lambda m: None,
    )
    write_events(sys_root, "system.slice/heat-governor.service", oom=0, oom_kill=0)
    guard.step()
    write_events(sys_root, "system.slice/heat-governor.service", oom=1, oom_kill=1)
    guard.step()
    ooms = named(events, "oom_kill")
    assert [e["source"] for e in ooms] == ["system.slice/heat-governor.service"]


# --- robustness ------------------------------------------------------------------------------


def test_missing_meminfo_and_failing_emit_never_raise(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    logs = []

    def boom(ev):
        raise RuntimeError("sink down")

    guard = memguard.MemGuard(
        MemoryRule(), proc_root=proc, sys_root=tmp_path / "sys", emit=boom, log=logs.append
    )
    guard.step()
    assert guard.doc()["avail_mib"] is None
    write_meminfo(proc, 60_000)
    guard.step()
    guard.step()
    assert guard.doc()["avail_mib"] == 60_000
    assert guard.polls == 3
    assert len([m for m in logs if "sink down" in m]) == 1  # logged once, not per poll


def test_poll_exception_is_logged_once_and_polling_continues(tmp_path, monkeypatch):
    guard, clock, _, _, logs, proc, _ = make(tmp_path)

    def broken(root):
        raise ValueError("bad meminfo")

    monkeypatch.setattr(memguard, "read_meminfo", broken)
    for _ in range(3):
        guard.step()
    assert guard.polls == 3
    assert len([m for m in logs if "bad meminfo" in m]) == 1


# --- mlockall --------------------------------------------------------------------------------


def test_mlockall_refused_is_logged_and_guard_runs(tmp_path):
    guard, clock, _, _, logs, proc, _ = make(tmp_path, poll_s=0.01)
    calls = []

    def refuse(flags):
        calls.append(flags)
        return -1

    guard._mlockall = refuse
    guard.start()
    try:
        deadline = time.monotonic() + 1.0
        while guard.polls < 3 and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        guard.stop()
    assert calls == [memguard.MCL_CURRENT | memguard.MCL_FUTURE]
    assert any(m.startswith("mlockall: refused errno=") and "continuing" in m for m in logs)
    assert guard.polls >= 3
    d = guard.doc()
    keys = {"action", "armed", "avail_mib", "last_kill", "kills_last_hour", "limits"}  # d3
    assert set(d) == keys
    assert d.pop("limits")["source"] == "percent"
    assert d == {
        "action": "kill",
        "armed": True,
        "avail_mib": 60_000,
        "last_kill": None,
        "kills_last_hour": 0,
    }


def test_mlockall_ok_logged_and_raising_mlockall_does_not_fail_start(tmp_path):
    guard, *_, logs, _, _ = make(tmp_path, poll_s=0.01)
    guard.start()
    guard.stop()
    assert "mlockall: ok" in logs

    guard2, *_, logs2, _, _ = make(tmp_path, poll_s=0.01)

    def missing(flags):
        raise AttributeError("no mlockall in this libc")

    guard2._mlockall = missing
    guard2.start()
    guard2.stop()
    assert any(m.startswith("mlockall: unavailable") for m in logs2)


def test_mlockall_not_called_in_record_mode(tmp_path):
    guard, *_ = make(tmp_path, action="record", poll_s=0.01)
    calls = []
    guard._mlockall = lambda flags: calls.append(flags) or 0
    guard.start()
    guard.stop()
    assert calls == []


# --- threading -------------------------------------------------------------------------------


def test_polls_continue_while_tick_thread_sleeps(tmp_path):
    """A tick stuck for '30 s' holding the daemon's lock does not slow the guard."""
    guard, *_ = make(tmp_path, action="record", poll_s=0.005)
    daemon_lock = threading.Lock()
    release = threading.Event()
    held = threading.Event()

    def tick():  # stands in for a 30 s blocking tick; released by the test
        with daemon_lock:
            held.set()
            release.wait(30)

    t = threading.Thread(target=tick, daemon=True)
    t.start()
    held.wait(1)
    guard.start()
    try:
        began = time.monotonic()
        while guard.polls < 20 and time.monotonic() - began < 1.0:
            time.sleep(0.005)
            guard.doc()  # readable from another thread while the tick is stuck
        elapsed = time.monotonic() - began
    finally:
        guard.stop()
        release.set()
        t.join(1)
    assert daemon_lock.locked() is False
    assert guard.polls >= 20, f"only {guard.polls} polls in {elapsed:.2f}s"


def test_stop_joins_promptly_even_with_long_poll(tmp_path):
    guard, *_ = make(tmp_path, action="record", poll_s=60.0)
    guard.start()
    began = time.monotonic()
    guard.stop()
    assert time.monotonic() - began < 0.5
    assert guard._thread is None

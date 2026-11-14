"""memkill: victim choice, pidfd-pinned SIGKILL, protect list, kill budget, repeats.

Everything runs over a fake /proc (tests/fakes_proc.py) with fake pidfd callables,
except ``test_real_pidfd_kills_only_the_child``, which signals only a ``sleep`` child
this test spawned itself.
"""

from __future__ import annotations

import hashlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from heat_governor import memkill, memprocs
from heat_governor.config import MemoryRule
from tests.fakes_proc import add_proc

MIB = 1024  # KiB per MiB
UID = 1000
ME = 500  # the daemon's pid in the fake tree
SHELL = 400  # the daemon's parent


class FakeTracker:
    def __init__(self, rates: dict[int, float] | None = None) -> None:
        self.rates = rates or {}

    def growth(self, pid: int, start: int) -> float:
        return self.rates.get(pid, 0.0)


class FakePidfd:
    """Records pidfd calls; never touches a real process."""

    def __init__(self, open_error: OSError | None = None, send_error: OSError | None = None):
        self.opened: list[int] = []
        self.sent: list[tuple[int, int]] = []
        self.closed: list[int] = []
        self.open_error = open_error
        self.send_error = send_error
        self.hook = None  # called after open, before the re-check (simulates races)

    def open(self, pid: int, flags: int = 0) -> int:
        if self.open_error is not None:
            raise self.open_error
        self.opened.append(pid)
        if self.hook is not None:
            self.hook(pid)
        return 9000 + pid

    def send(self, fd: int, sig: int, siginfo=None, flags: int = 0) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((fd, sig))

    def close(self, fd: int) -> None:
        self.closed.append(fd)


def base_tree(root: Path) -> None:
    """pid 1 systemd -> 300 systemd --user -> 400 bash -> 500 heat-governor (the daemon)."""
    add_proc(root, 1, ppid=0, uid=0, comm="systemd", cgroup="/init.scope")
    add_proc(
        root,
        300,
        ppid=1,
        uid=UID,
        comm="systemd",
        cmdline=["/lib/systemd/systemd", "--user"],
        anon_kib=8 * MIB,
        cgroup="/user.slice/user-1000.slice/user@1000.service/init.scope",
    )
    add_proc(
        root,
        SHELL,
        ppid=300,
        uid=UID,
        comm="bash",
        anon_kib=4 * MIB,
        cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/hg.service",
    )
    add_proc(
        root,
        ME,
        ppid=SHELL,
        uid=UID,
        comm="heat-governor",
        anon_kib=50 * MIB,
        cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/hg.service",
    )


def make(tmp_path, rule=None, clock=None, pidfd=None, **kw):
    pidfd = pidfd or FakePidfd()
    k = memkill.MemKiller(
        rule or MemoryRule(action="kill"),
        proc_root=tmp_path,
        clock=clock or (lambda: 1000.0),
        uid=UID,
        self_pid=ME,
        pidfd_open=pidfd.open,
        pidfd_send_signal=pidfd.send,
        close=pidfd.close,
        **kw,
    )
    return k, pidfd


def choose(tmp_path, rates=None, rule=None, **kw):
    k, _ = make(tmp_path, rule=rule, **kw)
    return k.choose(memprocs.scan(tmp_path), FakeTracker(rates))


# --- victim choice -------------------------------------------------------------------------


def test_biggest_own_uid_grower_over_minimum_is_chosen(tmp_path):
    base_tree(tmp_path)
    add_proc(tmp_path, 600, ppid=SHELL, uid=UID, comm="slowbig", anon_kib=9000 * MIB)
    add_proc(tmp_path, 601, ppid=SHELL, uid=UID, comm="grower", anon_kib=3000 * MIB)
    c = choose(tmp_path, rates={600: 1.0, 601: 900.0})
    assert c.proc.pid == 601
    assert c.how == "process"


def test_other_uid_protected_self_parent_chain_and_small_never_chosen(tmp_path):
    base_tree(tmp_path)
    big = 40_000 * MIB
    # the daemon and its parent chain grow huge: still never chosen
    add_proc(tmp_path, ME, ppid=SHELL, uid=UID, comm="heat-governor", anon_kib=big)
    add_proc(tmp_path, SHELL, ppid=300, uid=UID, comm="bash", anon_kib=big)
    add_proc(tmp_path, 300, ppid=1, uid=UID, comm="systemd", anon_kib=big)
    add_proc(tmp_path, 700, ppid=1, uid=10001, comm="container", anon_kib=big)  # other uid
    add_proc(tmp_path, 701, ppid=1, uid=0, comm="root-job", anon_kib=big)  # root
    for pid, comm in enumerate(
        ["gnome-shell", "Xwayland", "tailscaled", "dockerd", "containerd", "sshd", "custom"], 710
    ):
        add_proc(
            tmp_path,
            pid,
            ppid=1,
            uid=UID,
            comm=comm,
            anon_kib=big,
            cgroup=f"/user.slice/{comm}.scope",
        )
    add_proc(
        tmp_path,
        720,
        ppid=1,
        uid=UID,
        comm="tiny",
        anon_kib=100 * MIB,
        cgroup="/user.slice/tiny.scope",
    )  # under min, alone in its group
    rates = {p: 999.0 for p in (ME, SHELL, 300, 700, 701, *range(710, 717), 720)}
    rule = MemoryRule(action="kill", protect=("custom",))
    c = choose(tmp_path, rates=rates, rule=rule)
    assert c.proc is None
    assert c.why_not


def test_protect_matches_argv0_basename_beyond_comm_truncation(tmp_path):
    base_tree(tmp_path)
    add_proc(
        tmp_path,
        600,
        ppid=1,
        uid=UID,
        comm="my-very-long-se",
        cmdline=["/opt/bin/my-very-long-service-name", "--x"],
        anon_kib=9000 * MIB,
        cgroup="/user.slice/a.scope",
    )
    rule = MemoryRule(action="kill", protect=("my-very-long-service-name",))
    assert choose(tmp_path, rates={600: 50.0}, rule=rule).proc is None


def test_root_daemon_refuses_to_choose(tmp_path):
    base_tree(tmp_path)
    add_proc(tmp_path, 600, ppid=1, uid=0, comm="job", anon_kib=9000 * MIB)
    k = memkill.MemKiller(MemoryRule(action="kill"), proc_root=tmp_path, uid=0, self_pid=ME)
    c = k.choose(memprocs.scan(tmp_path), FakeTracker({600: 10.0}))
    assert c.proc is None
    assert "root" in c.why_not


def test_group_fallback_picks_biggest_member_of_fastest_growing_group(tmp_path):
    base_tree(tmp_path)
    # group A (top 800): three 1 GiB workers growing slowly
    a = "/user.slice/user-1000.slice/session-1.scope"
    add_proc(tmp_path, 800, ppid=1, uid=UID, comm="make", anon_kib=10 * MIB, cgroup=a)
    for pid in (801, 802, 803):
        add_proc(tmp_path, pid, ppid=800, uid=UID, comm="cc1", anon_kib=1000 * MIB, cgroup=a)
    # group B (top 900): node test runner whose workers grow fast
    b = "/user.slice/user-1000.slice/run-x.scope"
    add_proc(tmp_path, 900, ppid=1, uid=UID, comm="node", anon_kib=200 * MIB, cgroup=b)
    add_proc(tmp_path, 901, ppid=900, uid=UID, comm="node", anon_kib=1500 * MIB, cgroup=b)
    add_proc(tmp_path, 902, ppid=900, uid=UID, comm="node", anon_kib=900 * MIB, cgroup=b)
    rates = {801: 1.0, 802: 1.0, 803: 1.0, 901: 50.0, 902: 80.0}
    c = choose(tmp_path, rates=rates)
    assert c.how == "group"
    assert c.proc.pid == 901
    assert c.group["pid"] == 900


def test_group_fallback_refused_when_group_total_under_minimum(tmp_path):
    base_tree(tmp_path)
    g = "/user.slice/user-1000.slice/run-y.scope"
    add_proc(tmp_path, 900, ppid=1, uid=UID, comm="node", anon_kib=500 * MIB, cgroup=g)
    add_proc(tmp_path, 901, ppid=900, uid=UID, comm="node", anon_kib=500 * MIB, cgroup=g)
    c = choose(tmp_path, rates={900: 30.0, 901: 30.0})
    assert c.proc is None


def test_group_fallback_never_picks_protected_or_daemon_members(tmp_path):
    base_tree(tmp_path)
    # the daemon's own group grows fastest, but its only members are the daemon + parent
    add_proc(
        tmp_path,
        ME,
        ppid=SHELL,
        uid=UID,
        comm="heat-governor",
        anon_kib=1500 * MIB,
        cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/hg.service",
    )
    add_proc(
        tmp_path,
        SHELL,
        ppid=300,
        uid=UID,
        comm="bash",
        anon_kib=1500 * MIB,
        cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/hg.service",
    )
    c = choose(tmp_path, rates={ME: 500.0, SHELL: 500.0})
    assert c.proc is None


# --- kill path -----------------------------------------------------------------------------


def _victim(tmp_path, start=4242):
    base_tree(tmp_path)
    add_proc(
        tmp_path,
        600,
        ppid=SHELL,
        uid=UID,
        comm="runaway",
        cmdline=["python", "eat.py"],
        anon_kib=9000 * MIB,
        start=start,
    )
    return memprocs.scan(tmp_path)


def test_kill_goes_through_injected_pidfd_send_signal_with_sigkill(tmp_path):
    procs = _victim(tmp_path)
    k, fd = make(tmp_path)
    events = k.act(procs, FakeTracker({600: 900.0}), reason="horizon")
    assert fd.opened == [600]
    assert fd.sent == [(9600, signal.SIGKILL)]
    assert fd.closed == [9600]
    (ev,) = events
    assert ev["event"] == "memory_kill"
    assert ev["pid"] == 600
    assert ev["signal"] == "SIGKILL"
    assert ev["reason"] == "horizon"
    assert ev["repeat"] == 1
    assert ev["kills_last_hour"] == 1
    assert ev["parents"][0] == {"pid": SHELL, "comm": "bash"}
    assert ev["growth_mib_s"] == 900.0
    assert ev["rss_anon_mib"] == 9000.0


def test_pid_reused_between_scan_and_kill_is_not_signalled(tmp_path):
    procs = _victim(tmp_path, start=4242)
    k, fd = make(tmp_path)
    # between scan and kill, 600 exits and a new process takes its pid (new start time)
    fd.hook = lambda pid: add_proc(
        tmp_path, 600, ppid=SHELL, uid=UID, comm="runaway", anon_kib=9000 * MIB, start=9999
    )
    (ev,) = k.act(procs, FakeTracker({600: 900.0}), reason="floor")
    assert fd.sent == []
    assert fd.closed == [9600]
    assert ev["event"] == "memory_would_kill"
    assert "start" in ev["why_not"]
    assert k.kills_last_hour() == 0


def test_uid_changed_between_scan_and_kill_is_not_signalled(tmp_path):
    procs = _victim(tmp_path)
    k, fd = make(tmp_path)
    fd.hook = lambda pid: add_proc(
        tmp_path, 600, ppid=SHELL, uid=0, comm="runaway", anon_kib=9000 * MIB, start=4242
    )
    (ev,) = k.act(procs, FakeTracker({600: 900.0}), reason="floor")
    assert fd.sent == []
    assert "uid" in ev["why_not"]


def test_pid_gone_between_scan_and_kill_is_not_signalled(tmp_path):
    procs = _victim(tmp_path)
    k, fd = make(tmp_path)
    fd.hook = lambda pid: shutil.rmtree(tmp_path / "600")
    (ev,) = k.act(procs, FakeTracker({600: 900.0}), reason="floor")
    assert fd.sent == []
    assert ev["event"] == "memory_would_kill"


@pytest.mark.parametrize(
    "where", ["open", "send"], ids=["pidfd_open-ESRCH", "pidfd_send_signal-EPERM"]
)
def test_oserror_in_kill_path_records_reason_and_no_kill(tmp_path, where):
    procs = _victim(tmp_path)
    err = ProcessLookupError(3, "No such process") if where == "open" else PermissionError(1, "x")
    fd = FakePidfd(**{f"{where}_error": err})
    k, _ = make(tmp_path, pidfd=fd)
    (ev,) = k.act(procs, FakeTracker({600: 900.0}), reason="floor")
    assert fd.sent == []
    assert ev["event"] == "memory_would_kill"
    assert ev["why_not"].startswith("refused")
    assert k.kills_last_hour() == 0
    if where == "send":
        assert fd.closed == [9600]


def test_record_action_never_calls_kill(tmp_path):
    procs = _victim(tmp_path)
    k, fd = make(tmp_path, rule=MemoryRule(action="record"))
    (ev,) = k.act(procs, FakeTracker({600: 900.0}), reason="floor")
    assert fd.opened == []
    assert fd.sent == []
    assert ev["event"] == "memory_would_kill"
    assert ev["why_not"] == "record-only"
    assert ev["pid"] == 600


def test_no_victim_returns_would_kill_without_signal(tmp_path):
    base_tree(tmp_path)
    k, fd = make(tmp_path)
    (ev,) = k.act(memprocs.scan(tmp_path), FakeTracker(), reason="floor")
    assert fd.opened == []
    assert ev["event"] == "memory_would_kill"
    assert ev["pid"] is None
    assert ev["why_not"].startswith("no-victim")


def test_event_cmdline_is_redacted_and_raw_never_stored(tmp_path):
    base_tree(tmp_path)
    secret = "hunter2" * 3  # built at runtime: scan-secrets flags 20+ char literals
    add_proc(
        tmp_path,
        600,
        ppid=SHELL,
        uid=UID,
        comm="python",
        cmdline=["python", "eat.py", f"--api-key={secret}"],
        anon_kib=9000 * MIB,
    )
    k, _ = make(tmp_path)
    (ev,) = k.act(memprocs.scan(tmp_path), FakeTracker({600: 5.0}), reason="floor")
    assert secret not in repr(ev)
    raw_hash = hashlib.sha256(f"python eat.py --api-key={secret}".encode()).hexdigest()
    assert raw_hash not in repr(ev)


# --- budget and repeats --------------------------------------------------------------------


class Clock:
    def __init__(self, t: float = 10_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _respawn(tmp_path, start, cmd=("python", "eat.py")):
    add_proc(
        tmp_path,
        600,
        ppid=SHELL,
        uid=UID,
        comm="python",
        cmdline=list(cmd),
        anon_kib=9000 * MIB,
        start=start,
    )
    return memprocs.scan(tmp_path)


def test_fourth_trigger_in_an_hour_is_budget_exhausted_and_sends_nothing(tmp_path):
    base_tree(tmp_path)
    clock = Clock()
    k, fd = make(tmp_path, clock=clock)
    for i in range(3):
        (ev,) = k.act(_respawn(tmp_path, 100 + i), FakeTracker({600: 9.0}), reason="horizon")
        assert ev["event"] == "memory_kill"
        clock.t += 600
    sent_before = list(fd.sent)
    events = k.act(_respawn(tmp_path, 200), FakeTracker({600: 9.0}), reason="horizon")
    assert [e["event"] for e in events] == ["memory_would_kill", "memory_budget_exhausted"]
    assert events[0]["why_not"] == "budget"
    assert events[1]["max_kills_per_hour"] == 3
    assert events[1]["kills_last_hour"] == 3
    assert fd.sent == sent_before
    assert len(fd.opened) == 3
    # the window rolls: an hour after the first kill, one kill is allowed again
    clock.t = 10_000.0 + 3600.5
    (ev,) = k.act(_respawn(tmp_path, 300), FakeTracker({600: 9.0}), reason="horizon")
    assert ev["event"] == "memory_kill"
    assert ev["kills_last_hour"] == 3


def test_three_kills_of_same_cmdline_in_30_min_repeat_1_2_3_and_new_cmdline_resets(tmp_path):
    base_tree(tmp_path)
    clock = Clock()
    k, _ = make(tmp_path, clock=clock, rule=MemoryRule(action="kill", max_kills_per_hour=10))
    repeats = []
    for i in range(3):
        (ev,) = k.act(_respawn(tmp_path, 100 + i), FakeTracker({600: 9.0}), reason="horizon")
        repeats.append(ev["repeat"])
        clock.t += 300
    assert repeats == [1, 2, 3]
    (ev,) = k.act(_respawn(tmp_path, 200, ("python", "other.py")), FakeTracker(), reason="floor")
    assert ev["repeat"] == 1
    # more than 30 min after the last eat.py kill, the count starts over
    clock.t += 1801
    (ev,) = k.act(_respawn(tmp_path, 300), FakeTracker(), reason="floor")
    assert ev["repeat"] == 1


def test_repeat_key_uses_exe_link_when_readable(tmp_path):
    base_tree(tmp_path)
    k, _ = make(tmp_path)
    procs = _respawn(tmp_path, 100)
    (tmp_path / "600" / "exe").symlink_to("/usr/bin/python3.13")
    assert k.repeat_key(procs[600])[1] == "/usr/bin/python3.13"
    (tmp_path / "600" / "exe").unlink()
    assert k.repeat_key(procs[600])[1] == "python"


# --- the one real process ------------------------------------------------------------------


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(signal, "pidfd_send_signal"),
    reason="needs Linux pidfd",
)
def test_real_pidfd_kills_only_the_child(tmp_path):
    sleep = shutil.which("sleep")
    assert sleep
    victim = subprocess.Popen([sleep, "60"])  # noqa: S603 - our own child
    bystander = subprocess.Popen([sleep, "60"])  # noqa: S603 - our own child
    try:
        p = None
        for _ in range(100):  # wait until exec has replaced the forked python image
            p = memkill.read_proc(Path("/proc"), victim.pid)
            if p is not None and p.comm == "sleep":
                break
            time.sleep(0.01)
        assert p is not None
        assert p.comm == "sleep"
        k = memkill.MemKiller(MemoryRule(action="kill"), proc_root=Path("/proc"))
        # a stale start time (as if the pid were recycled) is refused on the real path too
        stale = memprocs.Proc(**{**p.__dict__, "start_ticks": p.start_ticks - 1})
        assert k.kill(stale) is not None
        assert victim.poll() is None
        assert k.kill(p) is None
        assert victim.wait(timeout=5) == -signal.SIGKILL
        assert bystander.poll() is None
    finally:
        for child in (victim, bystander):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

"""Memory-guard victim choice and the pidfd-pinned SIGKILL. Returns event dicts; writes nothing.

``MemKiller.act(procs, tracker, reason)`` is called once per trigger fire. It:

1. chooses a victim (``choose``): an own-uid process with anonymous RSS >= ``min_victim_mib``
   that is not protected, ranked by anon growth (MiB/s) then anon size. If none qualifies, it
   falls back to the own-uid group (``memprocs.groups``) whose anon memory grows fastest, and
   picks that group's biggest non-protected member (decision c37). A fallback group must be
   growing (> 0 MiB/s) and hold >= ``min_victim_mib`` anon in total.
2. with ``action = "record"`` returns ``memory_would_kill`` and never signals;
3. past ``max_kills_per_hour`` (rolling) returns ``memory_would_kill`` plus
   ``memory_budget_exhausted`` and never signals;
4. otherwise kills (``kill``): ``pidfd_open(pid)``, then re-reads ``/proc/<pid>/stat`` start
   time and ``/proc/<pid>/status`` uid and only if both still match the scanned process sends
   SIGKILL through ``pidfd_send_signal``. A recycled pid is therefore never signalled. Any
   mismatch or OSError means no kill, and the event says why.

Never chosen: other uids, pid <= 2, the daemon itself and its parent chain, any process
whose comm / argv[0] basename / exe basename is in ``PROTECTED`` or the ``[memory] protect``
list. A daemon running as root refuses to choose at all (own uid would mean everything).

Sizes and growth are anonymous memory (``rss_anon_mib``, ``Tracker.growth``), never total
RSS. Command lines leave this module only through ``memprocs.redact``; the repeat key holds
a sha256 of the raw command line in memory and never puts it in an event.
"""

from __future__ import annotations

import hashlib
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from heat_governor import memprocs
from heat_governor.memprocs import Proc

PROTECTED = frozenset(
    {
        "systemd",
        "init",
        "sshd",
        "sshd-session",
        "login",
        "gnome-shell",
        "Xwayland",
        "tailscaled",
        "dockerd",
        "containerd",
    }
)
HOUR_S = 3600.0
REPEAT_WINDOW_S = 1800.0

EV_KILL = "memory_kill"
EV_WOULD = "memory_would_kill"
EV_BUDGET = "memory_budget_exhausted"


def read_proc(proc_root: Path, pid: int) -> Proc | None:
    """Re-read one pid through ``proc_root`` (None if it is gone or unreadable)."""
    return memprocs._read_proc(Path(proc_root), pid)  # noqa: SLF001 - same package


def _default_pidfd_open(pid: int, flags: int = 0) -> int:
    return os.pidfd_open(pid, flags)


def _default_send(fd: int, sig: int, siginfo=None, flags: int = 0) -> None:
    signal.pidfd_send_signal(fd, sig, siginfo, flags)


@dataclass
class Choice:
    proc: Proc | None
    how: str = ""  # "process" | "group" | "" (none)
    why_not: str = ""  # set when proc is None
    group: dict | None = None
    growth_mib_s: float = 0.0


@dataclass
class MemKiller:
    rule: object  # config.MemoryRule
    proc_root: Path = Path("/proc")
    clock: object = time.time
    uid: int | None = None
    self_pid: int | None = None
    pidfd_open: object = _default_pidfd_open
    pidfd_send_signal: object = _default_send
    close: object = os.close
    _kills: deque = field(default_factory=deque, init=False, repr=False)
    _repeats: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.proc_root = Path(self.proc_root)
        if self.uid is None:
            self.uid = os.getuid()
        if self.self_pid is None:
            self.self_pid = os.getpid()

    # --- protection ------------------------------------------------------------------------

    def _names(self, p: Proc) -> set[str]:
        names = {p.comm}
        argv0 = p.cmdline.split(" ", 1)[0] if p.cmdline else ""
        if argv0:
            names.add(os.path.basename(argv0))
        exe = self._exe(p.pid)
        if exe:
            names.add(os.path.basename(exe))
        return names

    def _shielded(self, procs: dict[int, Proc]) -> set[int]:
        """The daemon and its whole parent chain."""
        return {self.self_pid, *memprocs.ancestors(procs, self.self_pid)}

    def _refusal(self, p: Proc, shielded: set[int]) -> str | None:
        if p.uid != self.uid:
            return "other-uid"
        if p.pid <= 2:
            return "pid<=2"
        if p.pid in shielded:
            return "self-or-parent"
        protect = PROTECTED | set(getattr(self.rule, "protect", ()) or ())
        if self._names(p) & protect:
            return "protected"
        return None

    # --- choice ----------------------------------------------------------------------------

    def choose(self, procs: dict[int, Proc], tracker) -> Choice:
        if self.uid == 0:
            return Choice(None, why_not="no-victim: daemon runs as root; refusing to choose")
        min_mib = float(self.rule.min_victim_mib)
        shielded = self._shielded(procs)

        def growth(p: Proc) -> float:
            return tracker.growth(p.pid, p.start_ticks) if tracker is not None else 0.0

        eligible = [p for p in procs.values() if self._refusal(p, shielded) is None]
        singles = [p for p in eligible if p.rss_anon_mib >= min_mib]
        if singles:
            best = max(singles, key=lambda p: (growth(p), p.rss_anon_mib, -p.pid))
            return Choice(best, how="process", growth_mib_s=growth(best))

        ok = {p.pid for p in eligible}
        ranked = sorted(
            memprocs.groups(procs, self.uid, tracker),
            key=lambda g: (g["growth_mib_s"], g["rss_anon_mib"]),
            reverse=True,
        )
        for g in ranked:
            if g["growth_mib_s"] <= 0 or g["rss_anon_mib"] < min_mib:
                continue
            members = [
                procs[pid]
                for pid in ok
                if memprocs.top_level(procs, pid) == g["pid"] and procs[pid].uid == self.uid
            ]
            if not members:
                continue
            best = max(members, key=lambda p: (p.rss_anon_mib, growth(p), -p.pid))
            return Choice(best, how="group", group=dict(g), growth_mib_s=growth(best))
        return Choice(
            None,
            why_not=f"no-victim: no eligible own-uid process >= {int(min_mib)} MiB "
            "and no growing group over it",
        )

    # --- kill ------------------------------------------------------------------------------

    def kill(self, proc: Proc) -> str | None:
        """SIGKILL ``proc`` through a pidfd. None on success, else why it was not signalled."""
        if proc.uid != self.uid or self.uid == 0 or proc.pid <= 2 or proc.pid == self.self_pid:
            return "refused: not a killable own-uid process"
        try:
            fd = self.pidfd_open(proc.pid, 0)
        except OSError as e:
            return f"refused: pidfd_open: {e.strerror or e}"
        try:
            now = read_proc(self.proc_root, proc.pid)
            if now is None:
                return "refused: process gone before signal"
            if now.start_ticks != proc.start_ticks:
                return "refused: start time changed (pid reused)"
            if now.uid != proc.uid:
                return "refused: uid changed"
            self.pidfd_send_signal(fd, signal.SIGKILL, None, 0)
            return None
        except OSError as e:
            return f"refused: pidfd_send_signal: {e.strerror or e}"
        finally:
            try:
                self.close(fd)
            except OSError:
                pass

    # --- budget / repeats ------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        while self._kills and self._kills[0] <= now - HOUR_S:
            self._kills.popleft()
        for key in [k for k, q in self._repeats.items() if not q or q[-1] <= now - REPEAT_WINDOW_S]:
            del self._repeats[key]

    def kills_last_hour(self, now: float | None = None) -> int:
        self._prune(self.clock() if now is None else now)
        return len(self._kills)

    def _exe(self, pid: int) -> str | None:
        try:
            return os.readlink(self.proc_root / str(pid) / "exe")
        except OSError:
            return None

    def repeat_key(self, p: Proc) -> tuple[int, str, str]:
        exe = self._exe(p.pid) or p.comm
        return (p.uid, exe, hashlib.sha256(p.cmdline.encode()).hexdigest())

    def _repeat_count(self, key, now: float) -> int:
        q = self._repeats.get(key, ())
        return sum(1 for t in q if t > now - REPEAT_WINDOW_S)

    # --- the entry point -------------------------------------------------------------------

    def act(self, procs: dict[int, Proc], tracker, reason: str, now: float | None = None):
        """One trigger fire -> list of event dicts (never empty)."""
        now = self.clock() if now is None else now
        self._prune(now)
        c = self.choose(procs, tracker)
        if c.proc is None:
            return [self._event(EV_WOULD, now, reason, None, procs, c, 0, why_not=c.why_not)]
        key = self.repeat_key(c.proc)
        repeat = self._repeat_count(key, now) + 1
        if self.rule.action != "kill":
            return [self._event(EV_WOULD, now, reason, c.proc, procs, c, repeat, "record-only")]
        if len(self._kills) >= int(self.rule.max_kills_per_hour):
            would = self._event(EV_WOULD, now, reason, c.proc, procs, c, repeat, "budget")
            exhausted = {
                "t": now,
                "event": EV_BUDGET,
                "reason": reason,
                "max_kills_per_hour": int(self.rule.max_kills_per_hour),
                "kills_last_hour": len(self._kills),
                "pid": c.proc.pid,
                "comm": c.proc.comm,
            }
            return [would, exhausted]
        refused = self.kill(c.proc)
        if refused is not None:
            return [self._event(EV_WOULD, now, reason, c.proc, procs, c, repeat, refused)]
        self._kills.append(now)
        self._repeats.setdefault(key, deque()).append(now)
        return [self._event(EV_KILL, now, reason, c.proc, procs, c, repeat)]

    def _event(self, name, now, reason, p, procs, c: Choice, repeat, why_not=None) -> dict:
        ev: dict = {"t": now, "event": name, "reason": reason}
        if p is None:
            ev.update({"pid": None, "comm": None})
        else:
            ev.update(
                {
                    "pid": p.pid,
                    "ppid": p.ppid,
                    "uid": p.uid,
                    "comm": p.comm,
                    "cmdline": memprocs.redact(p.cmdline),
                    "rss_anon_mib": p.rss_anon_mib,
                    "rss_mib": p.rss_mib,
                    "growth_mib_s": c.growth_mib_s,
                    "start_ticks": p.start_ticks,
                    "cgroup": p.cgroup,
                    "parents": [
                        {"pid": a, "comm": procs[a].comm} for a in memprocs.ancestors(procs, p.pid)
                    ],
                    "victim": c.how,
                    "group": c.group,
                    "repeat": repeat,
                }
            )
            if name == EV_KILL:
                ev["signal"] = "SIGKILL"
        ev["kills_last_hour"] = len(self._kills)
        if why_not is not None:
            ev["why_not"] = why_not
        return ev

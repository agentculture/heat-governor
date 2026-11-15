"""Memory-guard victim choice and the pidfd-pinned SIGKILL. Returns event dicts; writes nothing.

``MemKiller.act(procs, tracker, reason)`` is called once per trigger fire. It:

0. with boundaries (``bounds``, a ``memboundary.Assessment``): processes within their
   ``[[memory.allow]]`` boundary are never candidates, and the eligible process most MiB over
   its boundary (>= ``min_victim_mib``) is chosen first (``victim = "boundary"``); only when
   there is none does the growth choice below run;
1. chooses a victim (``choose``) by anon growth (MiB/s). Candidates are (a) the fastest-growing
   eligible single: an own-uid, non-protected process with anonymous RSS >= ``min_victim_mib``
   whose growth is > 0, and (b) the fastest-growing own-uid group (``memprocs.groups``,
   decision c37) that grows (> 0 MiB/s), holds >= ``min_victim_mib`` anon in total and has at
   least one growing eligible member. A group's score is its growth minus the single's when
   the single is one of its members, so a runaway is never outbid by its own group. The
   faster of the two wins (a tie goes to the single); for a group the victim is its biggest
   growing eligible member. A big idle process is therefore never chosen while something
   else is growing. Only when nothing is growing (a floor fire on a flat curve) does it fall
   back to the biggest eligible single >= ``min_victim_mib``.
2. with ``action = "record"`` returns ``memory_would_kill`` and never signals;
3. past ``max_kills_per_hour`` (rolling) returns ``memory_would_kill`` plus
   ``memory_budget_exhausted`` and never signals;
4. otherwise kills (``kill``): ``pidfd_open(pid)``, then re-reads ``/proc/<pid>/stat`` start
   time and ``/proc/<pid>/status`` uid and only if both still match the scanned process sends
   SIGKILL through ``pidfd_send_signal``. A recycled pid is therefore never signalled. Any
   mismatch or OSError means no kill, and the event says why.

``act_on(procs, tracker, proc, reason)`` acts on one named process (a far-over boundary)
through the same record-only / budget / pinned-kill path; a process it may not kill
(``refusal``: another uid, a protected name, the daemon's chain, below ``min_victim_mib``, or
a root daemon) yields ``memory_would_kill`` with ``why_not = "boundary: not killable (...)"``.

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
from typing import Callable

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


def _growth(tracker, p: Proc) -> float:
    return tracker.growth(p.pid, p.start_ticks) if tracker is not None else 0.0


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
    boundary: dict | None = None  # memboundary.BoundSet.info() when chosen for a boundary


@dataclass
class MemKiller:
    rule: object  # config.MemoryRule
    proc_root: Path = Path("/proc")
    clock: Callable[[], float] = time.time
    uid: int | None = None
    self_pid: int | None = None
    pidfd_open: Callable[[int, int], int] = _default_pidfd_open
    pidfd_send_signal: Callable[..., None] = _default_send
    close: Callable[[int], None] = os.close
    _kills: deque = field(default_factory=deque, init=False, repr=False)
    _repeats: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.proc_root = Path(self.proc_root)
        if self.uid is None:
            self.uid = os.getuid()
        if self.self_pid is None:
            self.self_pid = os.getpid()

    # --- protection ------------------------------------------------------------------------

    def names(self, p: Proc) -> set[str]:
        """comm, argv[0] basename and exe basename: the names protection and boundaries match."""
        return self._names(p)

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

    def choose(self, procs: dict[int, Proc], tracker, bounds=None) -> Choice:
        """The victim of one trigger fire. ``bounds`` (a ``memboundary.Assessment``) removes
        processes within their boundary from every tier and puts eligible processes over
        their boundary first (most MiB over); without it the choice is growth only."""
        if self.uid == 0:
            return Choice(None, why_not="no-victim: daemon runs as root; refusing to choose")
        min_mib = float(self.rule.min_victim_mib)
        shielded = self._shielded(procs)
        within = bounds.within if bounds is not None else frozenset()
        eligible = [
            p for p in procs.values() if p.pid not in within and self._refusal(p, shielded) is None
        ]
        first = self._over_boundary(eligible, bounds, min_mib)
        if first is not None:
            return first
        return self._by_growth(procs, tracker, eligible, min_mib)

    @staticmethod
    def _over_boundary(eligible: list[Proc], bounds, min_mib: float) -> Choice | None:
        """Tier 0: the eligible process most over its boundary (``bounds.over()`` order)."""
        if bounds is None:
            return None
        ok = {p.pid for p in eligible if p.rss_anon_mib >= min_mib}
        for st in bounds.over():
            if st.proc.pid in ok:
                return Choice(st.proc, how="boundary", boundary=st.bset.info())
        return None

    def _by_growth(self, procs, tracker, eligible: list[Proc], min_mib: float) -> Choice:
        def growth(p: Proc) -> float:
            return _growth(tracker, p)

        singles = [p for p in eligible if p.rss_anon_mib >= min_mib]

        # 1. the fastest-growing single (it must actually be growing)
        growing = [p for p in singles if growth(p) > 0]
        single = max(growing, key=lambda p: (growth(p), p.rss_anon_mib, -p.pid), default=None)
        single_g = growth(single) if single is not None else 0.0

        # 2. the fastest-growing group, scored by the growth the single does not explain
        group, group_g, group_best = self._best_group(
            procs, tracker, eligible, min_mib, single, single_g
        )

        # 3. act on whichever grows faster (a tie goes to the single: the precise target)
        if group is not None and group_g > single_g:
            return Choice(
                group_best, how="group", group=dict(group), growth_mib_s=growth(group_best)
            )
        if single is not None:
            return Choice(single, how="process", growth_mib_s=single_g)

        # 4. nothing is growing (a floor fire on a flat curve): the biggest eligible single
        if singles:
            best = max(singles, key=lambda p: (p.rss_anon_mib, -p.pid))
            return Choice(best, how="process", growth_mib_s=max(growth(best), 0.0))
        return Choice(
            None,
            why_not=f"no-victim: no eligible own-uid process >= {int(min_mib)} MiB "
            "and no growing group over it",
        )

    def _best_group(self, procs, tracker, eligible, min_mib, single, single_g):
        """(group, score, victim) of the fastest-growing own-uid group, or (None, 0.0, None)."""

        def growth(p: Proc) -> float:
            return _growth(tracker, p)

        group, group_g, group_best = None, 0.0, None
        ok = {p.pid for p in eligible}
        for g in memprocs.groups(procs, self.uid, tracker):
            if g["growth_mib_s"] <= 0 or g["rss_anon_mib"] < min_mib:
                continue
            members = [
                procs[pid]
                for pid in ok
                if memprocs.top_level(procs, pid) == g["pid"] and procs[pid].uid == self.uid
            ]
            moving = [p for p in members if growth(p) > 0]
            if not moving:
                continue  # its growth is all in protected members: no kill here stops it
            score = g["growth_mib_s"]
            if single is not None and single in members:
                score -= single_g
            best = max(moving, key=lambda p: (p.rss_anon_mib, growth(p), -p.pid))
            if group is None or (score, g["rss_anon_mib"]) > (group_g, group["rss_anon_mib"]):
                group, group_g, group_best = g, score, best
        return group, group_g, group_best

    def refusal(self, procs: dict[int, Proc], p: Proc) -> str | None:
        """Why ``p`` may not be killed on a boundary (None when it may): a root daemon, another
        uid, pid <= 2, the daemon or its parents, a protected name, or below min_victim_mib."""
        if self.uid == 0:
            return "daemon runs as root"
        why = self._refusal(p, self._shielded(procs))
        if why == "other-uid":
            return f"uid {p.uid}"
        if why is None and p.rss_anon_mib < float(self.rule.min_victim_mib):
            return f"below min_victim_mib {int(self.rule.min_victim_mib)}"
        return why

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

    def export_state(self) -> dict:
        """A snapshot of the rolling kill budget and repeat history, for ``import_state``.

        Lets a caller that rebuilds the killer (a config reload) carry both across. The
        timestamps are on this killer's ``clock``, so import only into a killer on the same
        clock in the same process. The snapshot shares nothing with this killer."""
        return {
            "kills": tuple(self._kills),
            "repeats": {key: tuple(q) for key, q in self._repeats.items()},
        }

    def import_state(self, state: dict | None) -> None:
        """Replace the budget and repeat history with an ``export_state`` snapshot (None or
        an empty dict leaves a fresh killer)."""
        state = state or {}
        self._kills = deque(sorted(state.get("kills", ())))
        self._repeats = {key: deque(q) for key, q in dict(state.get("repeats", {})).items()}

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

    def act(
        self, procs: dict[int, Proc], tracker, reason: str, now: float | None = None, bounds=None
    ):
        """One trigger fire -> list of event dicts (never empty)."""
        now = self.clock() if now is None else now
        self._prune(now)
        c = self.choose(procs, tracker, bounds)
        if c.proc is None:
            return [self._event(EV_WOULD, now, reason, None, procs, c, 0, why_not=c.why_not)]
        return self._execute(procs, c, reason, now)

    def act_on(
        self,
        procs: dict[int, Proc],
        tracker,
        proc: Proc,
        reason: str,
        now: float | None = None,
        boundary: dict | None = None,
    ):
        """Kill one named process (a far-over boundary) under the same protections, budget,
        record-only rule and pidfd kill as a trigger fire -> list of event dicts."""
        now = self.clock() if now is None else now
        self._prune(now)
        c = Choice(proc, how="boundary", growth_mib_s=_growth(tracker, proc), boundary=boundary)
        why = self.refusal(procs, proc)
        if why is not None:
            repeat = self._repeat_count(self.repeat_key(proc), now) + 1
            why_not = f"boundary: not killable ({why})"
            return [self._event(EV_WOULD, now, reason, proc, procs, c, repeat, why_not)]
        return self._execute(procs, c, reason, now)

    def _execute(self, procs: dict[int, Proc], c: Choice, reason: str, now: float) -> list:
        """Record-only, budget or the pinned kill of ``c.proc``."""
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
                    "boundary": c.boundary,
                    "repeat": repeat,
                }
            )
            if name == EV_KILL:
                ev["signal"] = "SIGKILL"
        ev["kills_last_hour"] = len(self._kills)
        if why_not is not None:
            ev["why_not"] = why_not
        return ev

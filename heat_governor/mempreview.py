"""On-box preview of a draft ``[[memory.allow]]`` rule against the running processes.

Pure over a ``memprocs.scan`` result: it reads the process table it is given (plus the exe
links of matching pids, through the killer's own names function) and signals nothing. For each
process a draft rule matches, ``state`` is what ``memboundary.assess`` says and ``killable`` /
``reason`` are what ``MemKiller.refusal`` says for the same table, protect list,
``min_victim_mib`` and uid, so the preview agrees with the live guard. Command lines leave this
module only through ``memprocs.redact``.
"""

from __future__ import annotations

import pwd
from pathlib import Path
from typing import Callable, Iterable

from heat_governor import memboundary, memkill, memprocs
from heat_governor.memboundary import FAR_OVER, UNBOUNDED


def _user_name(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return None


def _entry(p, status, why: str | None, user_of: Callable[[int], str | None]) -> dict:
    return {
        "pid": p.pid,
        "uid": p.uid,
        "user": user_of(p.uid),
        "comm": p.comm,
        "cmdline": memprocs.redact(p.cmdline),
        "container": memboundary.container_id(p.cgroup),
        "rss_anon_mib": p.rss_anon_mib,
        "rule": status.rule.name,
        "usage_mib": round(status.usage_mib, 1),
        "state": status.state,
        "killable": why is None,
        "reason": why,
        "far_over_killable": why is None and status.state == FAR_OVER,
    }


def preview(
    procs: dict,
    rules: Iterable,
    memory_rule,
    proc_root: Path = Path("/proc"),
    uid: int | None = None,
    self_pid: int | None = None,
    user_of: Callable[[int], str | None] = _user_name,
) -> dict:
    """What ``rules`` (the draft rule, or the whole would-be rule set) would match right now.

    ``memory_rule`` is the ``[memory]`` section (``action``, ``protect``, ``min_victim_mib``,
    ``act_over_pct``). Returns ``{"action", "matching", "far_over_killable", "processes"}``:
    ``processes`` has one entry per matching process (biggest anonymous memory first), each with
    pid, uid, user, comm, redacted cmdline, container, rss_anon_mib, rule, usage_mib, state,
    killable, reason (the refusal text, None when killable) and far_over_killable (a killable
    own-user process far over its boundary: what the guard would SIGKILL on the next scan)."""
    rules = tuple(rules)
    killer = memkill.MemKiller(memory_rule, proc_root=Path(proc_root), uid=uid, self_pid=self_pid)
    pct = float(getattr(memory_rule, "act_over_pct", 50.0))
    bounds = memboundary.assess(procs, rules, pct, killer.names)
    rows = []
    for pid in sorted(procs):
        status = bounds.status(pid)
        if status is None or bounds.state(pid) == UNBOUNDED:
            continue
        rows.append(_entry(procs[pid], status, killer.refusal(procs, procs[pid]), user_of))
    rows.sort(key=lambda r: (-r["rss_anon_mib"], r["pid"]))
    return {
        "action": getattr(memory_rule, "action", "record"),
        "matching": len(rows),
        "far_over_killable": sum(1 for r in rows if r["far_over_killable"]),
        "processes": rows,
    }

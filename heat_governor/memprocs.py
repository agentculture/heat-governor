"""Per-process memory view from /proc: anon RSS, ownership, ancestry, growth.

Everything takes an injected ``proc_root`` so tests run against a fake tree
(pattern: ``SystemProbe`` in ``sense.py``). The module never reads
``/proc/<pid>/environ``; command lines are the only free-text it keeps, and
``redact`` masks credential-looking values before they leave this module.

Top-level ancestor rule (used by ``ancestors`` / ``top_level`` / ``groups``):
starting from a process, climb to its parent while the parent (a) is in the
scan, (b) lives in the *same cgroup leaf* (e.g. ``session-545.scope`` or a
``run-*.scope`` / ``app.slice`` unit) and (c) is not a unit root. Unit roots are
pid 1/2 and the supervisors that own a unit: ``systemd``, ``init``, ``sshd``,
``sshd-session``, ``login``, ``tmux: server`` is *not* a root (it is work).
The last process reached is the top-level ancestor: the child of the session
leader / systemd --user / pid 1, i.e. "the thing the user started".
"""

from __future__ import annotations

import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

CMDLINE_MAX = 512
MASK = "***"
UNIT_ROOT_COMMS = frozenset({"systemd", "init", "sshd", "sshd-session", "login"})


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    uid: int
    comm: str
    cmdline: str  # raw, space-joined; pass through redact() before publishing
    rss_anon_mib: float
    rss_mib: float
    start_ticks: int
    cgroup: str


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except (OSError, ValueError):
        return None


def _kib(status: dict[str, str], key: str) -> float:
    parts = status.get(key, "").split()
    return int(parts[0]) / 1024.0 if parts and parts[0].isdigit() else 0.0


def _read_proc(root: Path, pid: int) -> Proc | None:
    d = root / str(pid)
    raw_status, raw_stat = _read(d / "status"), _read(d / "stat")
    if raw_status is None or raw_stat is None:
        return None
    status: dict[str, str] = {}
    for line in raw_status.splitlines():
        k, _, v = line.partition(":")
        status[k.strip()] = v.strip()
    try:
        uid = int(status["Uid"].split()[0])
        ppid = int(status["PPid"])
        rest = raw_stat[raw_stat.rindex(")") + 2 :].split()
        start = int(rest[19])  # field 22 (starttime); rest[0] is field 3
    except (KeyError, ValueError, IndexError):
        return None
    cmd = _read(d / "cmdline")
    if cmd is None and not d.exists():
        return None
    cmdline = " ".join(a for a in (cmd or "").split("\0") if a)
    cgroup = ""
    for line in (_read(d / "cgroup") or "").splitlines():
        path = line.rsplit(":", 1)[-1]
        if path:
            cgroup = path
            if line.startswith("0::"):
                break
    anon = _kib(status, "RssAnon")
    rss = _kib(status, "VmRSS") or anon + _kib(status, "RssFile")
    return Proc(
        pid=pid,
        ppid=ppid,
        uid=uid,
        comm=status.get("Name", ""),
        cmdline=cmdline,
        rss_anon_mib=round(anon, 3),
        rss_mib=round(rss, 3),
        start_ticks=start,
        cgroup=cgroup,
    )


def scan(proc_root: Path = Path("/proc")) -> dict[int, Proc]:
    """Read every numeric pid; a pid that vanishes (or is unreadable) is skipped."""
    out: dict[int, Proc] = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return out
    for name in names:
        if name.isdigit():
            p = _read_proc(Path(proc_root), int(name))
            if p is not None:
                out[p.pid] = p
    return out


class Tracker:
    """Keeps ~``window_s`` of anon-RSS samples per (pid, start time)."""

    def __init__(self, window_s: float = 60.0, clock=time.monotonic) -> None:
        self.window_s = window_s
        self._clock = clock
        self._s: dict[tuple[int, int], deque[tuple[float, float]]] = {}

    def update(self, procs: dict[int, Proc], now: float | None = None) -> None:
        now = self._clock() if now is None else now
        live = {(p.pid, p.start_ticks) for p in procs.values()}
        for key in [k for k in self._s if k not in live]:
            del self._s[key]
        for p in procs.values():
            q = self._s.setdefault((p.pid, p.start_ticks), deque())
            q.append((now, p.rss_anon_mib))
            while len(q) > 2 and q[1][0] < now - self.window_s:
                q.popleft()  # keep one sample at/older than the window edge

    def growth(self, pid: int, start: int) -> float:
        """Anon growth in MiB/s over the window (0.0 with fewer than 2 samples)."""
        q = self._s.get((pid, start))
        if not q or len(q) < 2 or q[-1][0] <= q[0][0]:
            return 0.0
        return round((q[-1][1] - q[0][1]) / (q[-1][0] - q[0][0]), 4)


def _is_unit_root(p: Proc) -> bool:
    return p.pid <= 2 or p.comm in UNIT_ROOT_COMMS


def ancestors(procs: dict[int, Proc], pid: int) -> list[int]:
    """Parent chain of ``pid`` (nearest first), stopping at a missing parent or a loop."""
    chain: list[int] = []
    seen = {pid}
    cur = procs.get(pid)
    while cur is not None and cur.ppid in procs and cur.ppid not in seen:
        chain.append(cur.ppid)
        seen.add(cur.ppid)
        cur = procs[cur.ppid]
    return chain


def top_level(procs: dict[int, Proc], pid: int) -> int:
    """Top-level ancestor of ``pid`` under the module's same-cgroup / unit-root rule."""
    me = procs[pid]
    best = pid
    for a in ancestors(procs, pid):
        anc = procs[a]
        if anc.cgroup != me.cgroup or _is_unit_root(anc):
            break
        best = a
    return best


def groups(procs: dict[int, Proc], uid: int, tracker: Tracker | None = None) -> list[dict]:
    """Own-uid processes summed by top-level ancestor, biggest anon total first."""
    acc: dict[int, dict] = {}
    for p in procs.values():
        if p.uid != uid:
            continue
        top = procs[top_level(procs, p.pid)]
        g = acc.setdefault(
            top.pid,
            {
                "pid": top.pid,
                "comm": top.comm,
                "cmdline": redact(top.cmdline),
                "cgroup": top.cgroup,
                "rss_anon_mib": 0.0,
                "growth_mib_s": 0.0,
                "procs": 0,
            },
        )
        g["rss_anon_mib"] = round(g["rss_anon_mib"] + p.rss_anon_mib, 3)
        g["procs"] += 1
        if tracker is not None:
            g["growth_mib_s"] = round(g["growth_mib_s"] + tracker.growth(p.pid, p.start_ticks), 4)
    return sorted(acc.values(), key=lambda g: g["rss_anon_mib"], reverse=True)


def top(procs: dict[int, Proc], tracker: Tracker | None = None, n: int = 10) -> list[dict]:
    """Record snapshot: the ``n`` biggest processes by anon RSS."""
    ranked = sorted(procs.values(), key=lambda p: p.rss_anon_mib, reverse=True)[: max(n, 0)]
    return [
        {
            "pid": p.pid,
            "ppid": p.ppid,
            "uid": p.uid,
            "comm": p.comm,
            "cmdline": redact(p.cmdline),
            "rss_anon_mib": p.rss_anon_mib,
            "rss_mib": p.rss_mib,
            "growth_mib_s": tracker.growth(p.pid, p.start_ticks) if tracker else 0.0,
            "start_t": p.start_ticks,
            "cgroup": p.cgroup,
        }
        for p in ranked
    ]


_SENSITIVE_FLAG_WORDS = r"(?:token|key|secret|passw(?:or)?d|passwd|pwd|auth|credential)"
_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s\"']+")
_FLAG_RE = re.compile(rf"(?i)(--?[\w.-]*{_SENSITIVE_FLAG_WORDS}[\w.-]*)(=|\s+)(?!-)(\S+)")
_ENV_NAME = r"[A-Za-z_][A-Za-z0-9_]*"  # NOSONAR python:S6353 -- ASCII names: \w is Unicode
_ENV_RE = re.compile(rf"(?<!\S)({_ENV_NAME})=(\S*)")
_KNOWN_RES = [
    re.compile(p)
    for p in (
        r"AKIA[0-9A-Z]{16}",
        r"gh[pousr]_[A-Za-z0-9]{36,}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"sk-[A-Za-z0-9]{20,}",
    )
]


def redact(cmdline: str | list[str] | tuple[str, ...]) -> str:
    """Mask credential values in a command line and truncate to 512 chars.

    Masks the value of token/key/secret/password/auth-style flags (``--token=X``,
    ``--api-key X``), every ``KEY=VALUE`` env-style argument, ``Authorization``
    headers and known credential shapes (AWS key ids, GitHub/Slack/OpenAI-style)."""
    s = cmdline if isinstance(cmdline, str) else " ".join(cmdline)
    s = _AUTH_RE.sub(lambda m: m.group(1) + MASK, s)
    s = _FLAG_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, s)
    s = _ENV_RE.sub(lambda m: m.group(1) + "=" + MASK, s)
    for rx in _KNOWN_RES:
        s = rx.sub(MASK, s)
    return s[:CMDLINE_MAX]

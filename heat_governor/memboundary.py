"""Memory boundaries (``[[memory.allow]]``) and blame: pure, no I/O, no threads.

A boundary says how much anonymous memory the processes it matches may use. Matching is an
AND of the matchers a rule gives: ``comm`` (exact comm, argv0 or exe basename), ``cmdline``
(fnmatch glob over the raw command line) and ``cgroup`` (fnmatch glob over the cgroup path).
A process matched by several rules gets the **tightest** one (smallest ``max_mib``; a tie goes
to the rule listed first), and counts towards that rule only.

``assess(procs, rules, act_over_pct)`` classifies every bounded process:

- ``within``: usage <= max_mib. Never blamed, never a victim.
- ``over``: usage > max_mib. The first victim candidate when the memory trigger fires.
- ``far_over``: usage > max_mib x (1 + act_over_pct / 100). Acted on at every scan, fire or no.

Usage is the process's own anon RSS for ``scope = "process"`` and the sum over every process
assigned to the rule for ``scope = "total"``. Unmatched processes are ``unbounded``.

``blame`` ranks up to three processes of ANY uid as the likely culprits: over their boundary
first (most MiB over), then by anon growth (> 0 only), then by anon size.

``Watch`` keeps the once-only state the guard needs: a first-crossing event per process
instance (pid + start time) per rule, reset only when usage falls back below max_mib by at
least ``RESET_PCT`` %; and one far-over action per process instance per rule.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from heat_governor import memprocs
from heat_governor.memprocs import Proc

WITHIN, OVER, FAR_OVER, UNBOUNDED, IDLE = "within", "over", "far_over", "unbounded", "idle"
RESET_PCT = 5.0  # a crossed boundary re-arms only below max_mib x (1 - RESET_PCT / 100)
BLAME_N = 3
_CONTAINER_RE = re.compile(r"(?:docker|containerd|libpod)[-/:]([0-9a-f]{64})")


def default_names(p: Proc) -> set[str]:
    """comm plus the argv0 basename (the exe basename needs I/O: the guard injects it)."""
    names = {p.comm}
    argv0 = p.cmdline.split(" ", 1)[0] if p.cmdline else ""
    if argv0:
        names.add(os.path.basename(argv0))
    return names


def matches(rule, p: Proc, names: Callable[[Proc], set[str]] | None = None) -> bool:
    """True when every matcher ``rule`` gives matches ``p``."""
    if rule.cmdline is not None and not fnmatch.fnmatchcase(p.cmdline, rule.cmdline):
        return False
    if rule.cgroup is not None and not fnmatch.fnmatchcase(p.cgroup, rule.cgroup):
        return False
    if rule.comm and not (names or default_names)(p) & set(rule.comm):
        return False
    return bool(rule.comm or rule.cmdline is not None or rule.cgroup is not None)


def applied_rule(rules: Iterable, p: Proc, names=None):
    """The tightest rule matching ``p`` (smallest max_mib, first listed on a tie), or None."""
    best = None
    for rule in rules:
        if (best is None or rule.max_mib < best.max_mib) and matches(rule, p, names):
            best = rule
    return best


def classify(usage_mib: float, max_mib: float, act_over_pct: float) -> str:
    if usage_mib > float(max_mib) * (1.0 + float(act_over_pct) / 100.0):
        return FAR_OVER
    if usage_mib > float(max_mib):
        return OVER
    return WITHIN


@dataclass(frozen=True)
class BoundSet:
    """What one boundary judges: one process (scope process) or every match (scope total)."""

    rule: object  # config.AllowRule
    members: tuple[Proc, ...]
    usage_mib: float
    state: str

    @property
    def over_mib(self) -> float:
        return round(max(self.usage_mib - float(self.rule.max_mib), 0.0), 3)

    def key(self) -> tuple:
        """The instance a first crossing is reported once for."""
        if self.rule.scope == "total":
            return (self.rule.name, None, None)  # same shape as a per-process key
        p = self.members[0]
        return (self.rule.name, p.pid, p.start_ticks)

    def info(self) -> dict:
        return {
            "name": self.rule.name,
            "max_mib": int(self.rule.max_mib),
            "usage_mib": round(self.usage_mib, 1),
            "over_mib": round(self.over_mib, 1),
            "scope": self.rule.scope,
        }


@dataclass(frozen=True)
class Status:
    """One bounded process: its set's usage and state."""

    proc: Proc
    bset: BoundSet

    @property
    def rule(self):
        return self.bset.rule

    @property
    def usage_mib(self) -> float:
        return self.bset.usage_mib

    @property
    def state(self) -> str:
        return self.bset.state

    @property
    def over_mib(self) -> float:
        return self.bset.over_mib


class Assessment:
    def __init__(self, rules: tuple, sets: list[BoundSet]) -> None:
        self.rules = rules
        self.sets = sets
        self._by_pid = {p.pid: Status(p, s) for s in sets for p in s.members}
        self.within = frozenset(pid for pid, st in self._by_pid.items() if st.state == WITHIN)

    def status(self, pid: int) -> Status | None:
        return self._by_pid.get(pid)

    def state(self, pid: int) -> str:
        st = self._by_pid.get(pid)
        return st.state if st is not None else UNBOUNDED

    def over(self) -> list[Status]:
        """Processes over (or far over) their boundary, most MiB over first."""
        out = [st for st in self._by_pid.values() if st.state != WITHIN]
        return sorted(out, key=lambda st: (-st.over_mib, -st.proc.rss_anon_mib, st.proc.pid))

    def doc(self) -> list[dict]:
        """One entry per rule: the worst set's usage and state, ``idle`` when nothing matches."""
        return [self._rule_doc(rule) for rule in self.rules]

    def _rule_doc(self, rule) -> dict:
        mine = [s for s in self.sets if s.rule is rule]
        if not mine:
            return _entry(rule, 0.0, IDLE)
        worst = max(mine, key=lambda s: s.usage_mib)
        return _entry(rule, worst.usage_mib, worst.state)


def _entry(rule, usage: float, state: str) -> dict:
    return {
        "name": rule.name,
        "max_mib": int(rule.max_mib),
        "scope": rule.scope,
        "usage_mib": round(usage, 1),
        "state": state,
        "comm": list(rule.comm),
        "cmdline": rule.cmdline,
        "cgroup": rule.cgroup,
        "source": rule.source,
    }


def idle_doc(rules: Iterable) -> list[dict]:
    """The ``boundaries`` doc before the first scan."""
    return [_entry(rule, 0.0, IDLE) for rule in rules]


def assess(procs: dict[int, Proc], rules: Iterable, act_over_pct: float, names=None) -> Assessment:
    """Assign each process to its tightest rule and classify every set."""
    rules = tuple(rules)
    names = _cached(names or default_names)
    assigned: dict[int, list[Proc]] = {id(rule): [] for rule in rules}
    for p in sorted(procs.values(), key=lambda q: q.pid):
        rule = applied_rule(rules, p, names) if rules else None
        if rule is not None:
            assigned[id(rule)].append(p)
    sets: list[BoundSet] = []
    for rule in rules:
        sets.extend(_sets(rule, assigned[id(rule)], act_over_pct))
    return Assessment(rules, sets)


def _cached(names: Callable[[Proc], set[str]]) -> Callable[[Proc], set[str]]:
    """``names`` memoised per process for one assessment (the exe lookup is a readlink)."""
    memo: dict[tuple[int, int], set[str]] = {}

    def lookup(p: Proc) -> set[str]:
        key = (p.pid, p.start_ticks)
        if key not in memo:
            memo[key] = names(p)
        return memo[key]

    return lookup


def _sets(rule, members: list[Proc], act_over_pct: float) -> list[BoundSet]:
    if rule.scope != "total":
        return [
            BoundSet(
                rule, (p,), p.rss_anon_mib, classify(p.rss_anon_mib, rule.max_mib, act_over_pct)
            )
            for p in members
        ]
    if not members:
        return []
    total = round(sum(p.rss_anon_mib for p in members), 3)
    return [BoundSet(rule, tuple(members), total, classify(total, rule.max_mib, act_over_pct))]


def container_id(cgroup: str) -> str | None:
    """The short (12 hex) docker/containerd container id in a cgroup path, or None."""
    m = _CONTAINER_RE.search(cgroup or "")
    return m.group(1)[:12] if m else None


def blame(
    procs: dict[int, Proc], tracker, bounds: Assessment | None, n: int = BLAME_N
) -> list[dict]:
    """Up to ``n`` likely culprits across all uids: processes over their boundary, then
    processes that are growing. Only when nothing is over or growing (a flat curve under the
    floor) does it fall back to the biggest processes, so the evidence is never empty.
    Processes within their boundary and processes without anonymous memory are never blamed.
    """

    def growth(p: Proc) -> float:
        return tracker.growth(p.pid, p.start_ticks) if tracker is not None else 0.0

    def over(p: Proc) -> float:
        st = bounds.status(p.pid) if bounds is not None else None
        return st.over_mib if st is not None else 0.0

    def rank(p: Proc) -> tuple:
        return (-over(p), -max(growth(p), 0.0), -p.rss_anon_mib, p.pid)

    within = bounds.within if bounds is not None else frozenset()
    pool = [p for p in procs.values() if p.pid not in within and p.rss_anon_mib > 0]
    culprits = [p for p in pool if over(p) > 0 or growth(p) > 0] or pool
    return [_blamed(p, growth(p), bounds) for p in sorted(culprits, key=rank)[: max(n, 0)]]


def _blamed(p: Proc, growth: float, bounds: Assessment | None) -> dict:
    st = bounds.status(p.pid) if bounds is not None else None
    return {
        "pid": p.pid,
        "uid": p.uid,
        "comm": p.comm,
        "cmdline": memprocs.redact(p.cmdline),
        "rss_anon_mib": p.rss_anon_mib,
        "growth_mib_s": growth,
        "cgroup": p.cgroup,
        "container": container_id(p.cgroup),
        "boundary": st.bset.info() if st is not None else None,
    }


class Watch:
    """Once-only bookkeeping across scans (owned by the guard thread; not thread-safe)."""

    def __init__(self) -> None:
        self._crossed: set[tuple] = set()
        self._acted: set[tuple] = set()

    @staticmethod
    def _well_under(s: BoundSet | None) -> bool:
        """Gone, or back under its max by RESET_PCT: the next crossing is reported again."""
        return s is None or s.usage_mib < float(s.rule.max_mib) * (1.0 - RESET_PCT / 100.0)

    def crossings(self, bounds: Assessment) -> list[BoundSet]:
        """Sets over their boundary for the first time since they were last well under it."""
        live: dict[tuple, BoundSet] = {s.key(): s for s in bounds.sets}
        stale = {key for key in self._crossed if self._well_under(live.get(key))}
        self._crossed -= stale
        new = [s for k, s in live.items() if s.state != WITHIN and k not in self._crossed]
        self._crossed.update(s.key() for s in new)
        return new

    def due(self, bounds: Assessment, victim_of: Callable[[BoundSet], Proc | None]) -> list:
        """``(set, victim)`` for each far-over set whose victim (``victim_of(set)``) has not
        been acted on for that rule yet. Each returned pair is marked as acted on."""
        live = {(p.pid, p.start_ticks) for s in bounds.sets for p in s.members}
        self._acted = {k for k in self._acted if (k[1], k[2]) in live}
        out = []
        for s in bounds.sets:
            victim = victim_of(s) if s.state == FAR_OVER else None
            if victim is None:
                continue
            key = (s.rule.name, victim.pid, victim.start_ticks)
            if key not in self._acted:
                self._acted.add(key)
                out.append((s, victim))
        return out

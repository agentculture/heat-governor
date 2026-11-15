"""The memory guard: its own thread polling /proc/meminfo, trigger, kill, OOM counters, top.

``MemGuard(rule, ...).start()`` runs one daemon thread that calls ``step()`` every
``rule.poll_s`` seconds. It shares nothing with the thermal tick except ``emit`` (the
caller's sink) and ``doc()`` (an immutable snapshot swapped under a private lock), so a
tick that blocks for 30 s never delays a poll. ``step()`` is public so tests drive the loop
with a fake clock and no thread.

Each ``step()``:

1. reads ``MemTotal`` / ``MemAvailable`` from ``<proc_root>/meminfo`` and feeds the
   ``memtrigger.Trigger`` (monotonic time);
2. scans processes and updates the growth ``Tracker`` only while available memory is under
   the soft limit (a full scan costs ~34 ms, too much for every 0.25 s poll), plus
   once every ``top_every_s`` for the routine top snapshot;
3. emits a ``memory_top`` snapshot every ``top_every_s``, and every poll while under
   the soft limit;
4. on a trigger fire hands the scan to ``memkill.MemKiller.act`` (which kills with
   ``action = "kill"`` or returns ``memory_would_kill`` with ``action = "record"``) and emits
   its events, re-stamped with wall-clock ``t``. When that fire killed nothing because no
   victim was found or the kill was refused, the trigger is re-armed (``Trigger.rearm``)
   so the guard tries again in the same wave: after a fresh ``CONFIRM`` streak and no sooner
   than ``max(REFIRE_POLLS x poll_s, REFIRE_MIN_S)`` (1 s by default). ``record-only`` and
   ``budget`` fires do not re-arm, so they stay one per wave;
5. reads ``/proc/vmstat`` ``oom_kill`` and the ``oom_kill`` line of each watched cgroup's
   ``memory.events``. The first read is the baseline; each later increase emits one
   ``oom_kill`` event per counter.

``emit(event_dict)`` is the only output. An exception inside a poll is logged once per
distinct message and the thread keeps polling.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path

from heat_governor import memprocs
from heat_governor.memkill import EV_KILL, EV_WOULD, MemKiller
from heat_governor.memtrigger import Trigger

MCL_CURRENT = 1
EV_TOP = "memory_top"
EV_OOM = "oom_kill"
# A fire that found no victim or whose kill was refused re-arms the trigger, but the next
# fire waits at least max(REFIRE_POLLS x poll_s, REFIRE_MIN_S): 1 s at the 0.25 s default.
# That leaves a refused kill a retry every second (a pid reused mid-kill, a victim that
# appears a moment later) without a would-kill row on every poll for the rest of the wave.
REFIRE_POLLS = 4
REFIRE_MIN_S = 1.0
RETRY_WHY_NOT = ("no-victim", "refused")


def _libc_mlockall(flags: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    return int(libc.mlockall(flags))


def _noop(*_args, **_kwargs) -> None:
    return None


def read_meminfo(proc_root: Path) -> tuple[float, float] | None:
    """(MemTotal, MemAvailable) in MiB, or None if the file or a field is missing."""
    try:
        text = (Path(proc_root) / "meminfo").read_text()
    except OSError:
        return None
    vals: dict[str, float] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if key in ("MemTotal", "MemAvailable") and parts and parts[0].isdigit():
            vals[key] = int(parts[0]) / 1024.0
    if "MemTotal" not in vals or "MemAvailable" not in vals or vals["MemTotal"] <= 0:
        return None
    return vals["MemTotal"], vals["MemAvailable"]


def _read_counter(path: Path, key: str) -> int | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key and parts[1].isdigit():
            return int(parts[1])
    return None


def effective_limits(
    rule, platform_limits: tuple[int, int] | None, total_mib: float
) -> tuple[float, float, str]:
    """(floor_mib, soft_mib, source) with precedence config > platform > percent (d3).

    ``source`` is "config" ([memory] floor_mib/soft_mib), "platform" (the profile's declared
    limits, for boxes that run full of AI work) or "percent" (floor_pct/soft_pct of RAM)."""
    floor, soft = getattr(rule, "floor_mib", None), getattr(rule, "soft_mib", None)
    if floor is not None and soft is not None:
        return float(floor), float(soft), "config"
    if platform_limits is not None:
        return float(platform_limits[0]), float(platform_limits[1]), "platform"
    total = float(total_mib)
    return total * float(rule.floor_pct) / 100.0, total * float(rule.soft_pct) / 100.0, "percent"


class MemGuard:
    def __init__(
        self,
        rule,
        proc_root: Path = Path("/proc"),
        sys_root: Path = Path("/sys"),
        clock=time.time,
        monotonic=time.monotonic,
        killer: MemKiller | None = None,
        emit=_noop,
        mlockall=_libc_mlockall,
        log=_noop,
        cgroups: tuple[str, ...] = (),
        uid: int | None = None,
        self_pid: int | None = None,
        platform_limits: tuple[int, int] | None = None,
    ) -> None:
        self.rule = rule
        self.platform_limits = platform_limits
        self._limits: tuple[float, float, str] | None = None
        self.proc_root = Path(proc_root)
        self.sys_root = Path(sys_root)
        self._clock = clock
        self._mono = monotonic
        self._emit = emit
        self._mlockall = mlockall
        self._log = log
        uid = os.getuid() if uid is None else uid
        self.killer = killer or MemKiller(
            rule, proc_root=self.proc_root, clock=monotonic, uid=uid, self_pid=self_pid
        )
        paths = [f"user.slice/user-{uid}.slice", *cgroups]
        self.cgroups = tuple(dict.fromkeys(p.strip("/") for p in paths if p.strip("/")))
        self.tracker = memprocs.Tracker(window_s=float(rule.window_s), clock=monotonic)
        self._trigger = None
        self._total_mib: float | None = None
        self._armed = True
        self._last_top: float | None = None
        self._last_kill: dict | None = None
        self._counters: dict[str, int] = {}
        self._errors: set[str] = set()
        self.polls = 0
        self._lock = threading.Lock()
        self._doc = self._build_doc(None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.rule.action == "kill":
            self._lock_memory()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="heat-governor-memguard")
        self._thread.daemon = True
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout)

    def _run(self) -> None:
        poll = max(float(self.rule.poll_s), 0.001)
        while not self._stop.is_set():
            began = self._mono()
            self.step()
            spent = self._mono() - began
            if self._stop.wait(max(poll - spent, 0.0)):
                break

    def _lock_memory(self) -> None:
        try:
            # MCL_CURRENT only: lock the interpreter, modules and guard code resident now.
            # MCL_FUTURE would lock and fault in every later allocation (thread stacks, HTTP
            # threads, parse buffers): 420-590 MB locked against ~100 MB RSS on the live
            # daemons. The unit's MemoryMin covers residency beyond the start-up pages.
            rc = self._mlockall(MCL_CURRENT)
        except Exception as e:  # noqa: BLE001 - best effort that must never fail start()
            self._log(f"mlockall: unavailable ({e}), continuing")
            return
        if rc == 0:
            self._log("mlockall: ok (current pages)")
        else:
            self._log(f"mlockall: refused errno={ctypes.get_errno()}, continuing")

    # --- state -----------------------------------------------------------------------------

    def doc(self) -> dict:
        """The ``mem_guard`` state object (a fresh copy; safe from any thread)."""
        with self._lock:
            d = dict(self._doc)
        if d["last_kill"] is not None:
            d["last_kill"] = dict(d["last_kill"])
        return d

    def _build_doc(self, avail_mib: float | None, now: float | None = None) -> dict:
        try:
            kills = self.killer.kills_last_hour(now)
        except Exception:  # noqa: BLE001 - a doc must always build
            kills = 0
        return {
            "action": str(self.rule.action),
            "armed": self._armed,
            "avail_mib": None if avail_mib is None else int(avail_mib),
            "last_kill": None if self._last_kill is None else dict(self._last_kill),
            "kills_last_hour": int(kills),
            "limits": self._limits_doc(),
        }

    def _limits_doc(self) -> dict:
        """The effective limits; floor/soft are None until MemTotal has been read."""
        if self._limits is None:
            src = "config" if getattr(self.rule, "floor_mib", None) is not None else None
            src = src or ("platform" if self.platform_limits is not None else "percent")
            return {"floor_mib": None, "soft_mib": None, "source": src}
        floor, soft, src = self._limits
        return {"floor_mib": int(floor), "soft_mib": int(soft), "source": src}

    # --- one poll --------------------------------------------------------------------------

    def step(self) -> None:
        """One poll. Never raises."""
        try:
            self._step()
        except Exception as e:  # noqa: BLE001 - the guard thread must survive anything
            msg = f"{type(e).__name__}: {e}"
            if msg not in self._errors:
                self._errors.add(msg)
                self._log(f"memguard: poll failed ({msg}), continuing")
        finally:
            self.polls += 1

    def _send(self, ev: dict) -> None:
        try:
            self._emit(ev)
        except Exception as e:  # noqa: BLE001 - a broken sink must not stop the guard
            msg = f"emit: {type(e).__name__}: {e}"
            if msg not in self._errors:
                self._errors.add(msg)
                self._log(f"memguard: {msg}, continuing")

    def _step(self) -> None:
        mono, wall = self._mono(), self._clock()
        mem = read_meminfo(self.proc_root)
        avail = None
        if mem is not None:
            total, avail = mem
            self._memory(mono, wall, total, avail)
        self._oom_counters(wall)
        doc = self._build_doc(avail, mono)
        with self._lock:
            self._doc = doc

    def _memory(self, mono: float, wall: float, total: float, avail: float) -> None:
        r = self.rule
        if self._trigger is None or total != self._total_mib:
            self._limits = effective_limits(r, self.platform_limits, total)
            floor_mib, soft_mib, _ = self._limits
            self._trigger = Trigger(
                total,
                horizon_s=r.horizon_s,
                window_s=r.window_s,
                floor_mib=floor_mib,
                soft_mib=soft_mib,
            )
            self._total_mib = total
        soft_mib = self._limits[1]
        fire = self._trigger.step(mono, avail)
        self._armed = self._trigger.armed
        low = avail < soft_mib
        routine = self._last_top is None or mono - self._last_top >= float(r.top_every_s)
        procs = None
        if low or routine or fire is not None:
            procs = memprocs.scan(self.proc_root)
            self.tracker.update(procs, mono)
        if low or routine:
            self._last_top = mono
            self._send(
                {
                    "t": wall,
                    "event": EV_TOP,
                    "avail_mib": round(avail, 1),
                    "total_mib": round(total, 1),
                    "top": memprocs.top(procs, self.tracker, int(r.top_n)),
                }
            )
        if fire is not None:
            events = self.killer.act(procs, self.tracker, fire.reason, mono)
            if self._should_retry(events):
                gap = max(REFIRE_POLLS * float(r.poll_s), REFIRE_MIN_S)
                self._trigger.rearm(not_before=mono + gap)
                self._armed = self._trigger.armed
            for ev in events:
                ev = dict(ev)
                ev["t"] = wall  # memkill stamps its monotonic clock; records use epoch
                ev.setdefault("avail_mib", round(fire.avail_mib, 1))
                ev.setdefault("rate_mib_s", round(fire.rate_mib_s, 1))
                if ev.get("event") == EV_KILL:
                    self._last_kill = {
                        "t": wall,
                        "pid": ev.get("pid"),
                        "comm": ev.get("comm"),
                        "rss_mib": ev.get("rss_mib"),
                        "repeat": ev.get("repeat"),
                    }
                self._send(ev)

    @staticmethod
    def _should_retry(events: list[dict]) -> bool:
        """True when a fire killed nothing because no victim was found or the kill was
        refused; never for ``record-only`` or ``budget``, which would repeat every poll."""
        if any(ev.get("event") == EV_KILL for ev in events):
            return False
        return any(
            ev.get("event") == EV_WOULD and str(ev.get("why_not", "")).startswith(RETRY_WHY_NOT)
            for ev in events
        )

    def _oom_counters(self, wall: float) -> None:
        sources = [("vmstat", self.proc_root / "vmstat")]
        cg = self.sys_root / "fs" / "cgroup"
        sources += [(p, cg / p / "memory.events") for p in self.cgroups]
        for source, path in sources:
            total = _read_counter(path, "oom_kill")
            if total is None:
                continue
            prev = self._counters.get(source)
            self._counters[source] = total
            if prev is not None and total > prev:
                self._send(
                    {
                        "t": wall,
                        "event": EV_OOM,
                        "source": source,
                        "delta": total - prev,
                        "total": total,
                    }
                )

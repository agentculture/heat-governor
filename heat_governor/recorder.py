"""Recording (one fsync'd JSON line per tick), reset reports and trace replay.

Recording is the *learning* half: it never changes the machine. Every tick
writes the temperatures, the GPU reading and the decision; every daemon start
writes a ``start`` event with the kernel boot id. A hard reset therefore shows
up as a start event with a NEW boot id after a gap -- :func:`report` finds
those and summarises the last seconds before each one.

:func:`replay` feeds a recorded trace (this module's JSONL, or the 1 s
``t1s-*.log`` telemetry format already running on the Sparks) through a
:class:`~heat_governor.policy.Controller`, so a profile change can be checked
against the traces of real resets before it governs anything.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from heat_governor.platforms import Profile
from heat_governor.policy import Controller
from heat_governor.sense import flat_sensors


def boot_id(proc_root: Path = Path("/proc")) -> str:
    try:
        return (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


class Recorder:
    def __init__(self, directory: Path, *, fsync: bool = True) -> None:
        self.directory = directory
        self.fsync = fsync
        self._day: str | None = None
        self._fh = None
        self._lock = threading.Lock()  # the tick loop, guard and shipper all write

    def _file(self, t: float):
        day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d")
        if day != self._day:
            if self._fh:
                self._fh.close()
            self.directory.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.directory / f"{day}.jsonl", "a", encoding="utf-8")
            self._day = day
        return self._fh

    def write(self, rec: dict) -> None:
        t = float(rec.get("t") or time.time())
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with self._lock:
            fh = self._file(t)
            fh.write(line)
            fh.flush()
            if self.fsync:
                os.fsync(fh.fileno())

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None
            self._day = None


def tail(
    directory: Path, minutes: float, *, now: float | None = None, clock=time.time
) -> list[dict]:
    """Rows with ``t >= now - minutes*60``, oldest first, from today's file and (the window
    may straddle UTC midnight) the previous day's. ``now`` or ``clock`` is injectable."""
    now = clock() if now is None else now
    since = now - minutes * 60.0
    days = {datetime.fromtimestamp(x, timezone.utc).strftime("%Y%m%d") for x in (since, now)}
    out: list[dict] = []
    for day in sorted(days):
        out.extend(_rows_between(directory / f"{day}.jsonl", since, now))
    return out


_TAIL_BLOCK = 64 * 1024
_T_PREFIX = re.compile(rb'^\s*\{\s*"t"\s*:\s*(-?\d[\d.eE+-]*)')


def _lines_backwards(fh, block: int = _TAIL_BLOCK) -> Iterator[bytes]:
    """The lines of an open binary file, last first, reading ``block`` bytes at a time."""
    pos = fh.seek(0, os.SEEK_END)
    carry = b""
    while pos > 0:
        size = min(block, pos)
        pos -= size
        fh.seek(pos)
        parts = (fh.read(size) + carry).split(b"\n")
        carry = parts[0]  # may continue in the previous block
        yield from reversed(parts[1:])
    yield carry


def _rows_between(path: Path, since: float, now: float) -> list[dict]:
    """The rows of one day file with ``since <= t <= now``, oldest first; none if it cannot
    be opened. Reads from the end and stops at the first row older than ``since`` (rows are
    appended in time order), parsing only lines whose leading ``{"t":<number>`` is in range."""
    out: list[dict] = []
    try:
        fh = open(path, "rb")
    except OSError:
        return out
    with fh:
        for raw in _lines_backwards(fh):
            verdict = _lead_verdict(raw, since, now)
            if verdict == "stop":
                break
            if verdict == "skip":
                continue
            rec = _row_in_window(raw, since, now)
            if rec is not None:
                out.append(rec)
    out.reverse()
    return out


def _lead_verdict(raw: bytes, since: float, now: float) -> str:
    """From the line's leading ``{"t":<number>`` alone: "stop" (older than the window: every
    earlier line is older too), "skip" (newer than ``now``, or blank) or "parse"."""
    m = _T_PREFIX.match(raw)
    if m is None:
        return "parse" if raw.strip() else "skip"
    try:
        lead = float(m.group(1))
    except ValueError:
        return "parse"
    if lead < since:
        return "stop"
    return "skip" if lead > now else "parse"


def _row_in_window(raw: bytes, since: float, now: float) -> dict | None:
    """The parsed row when it is a dict whose ``t`` is in ``[since, now]``, else None."""
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    t = rec.get("t") if isinstance(rec, dict) else None
    return rec if isinstance(t, (int, float)) and since <= t <= now else None


def iter_records(directory: Path) -> Iterator[dict]:
    for path in sorted(directory.glob("*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue  # a torn last line after a power loss


MEMORY_EVENTS = ("memory_kill", "oom_kill", "memory_budget_exhausted", "memory_boundary_exceeded")
EVENT_LOOKBACK_S = 600.0  # kills precede the final freeze: look 10 minutes back from the last tick


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _mem_block(recent: list[dict]) -> dict:
    """Memory over the report window; a field absent from every tick is None."""
    avail, swap, psi = [], [], []
    for r in recent:
        sysd = r.get("system")
        if not isinstance(sysd, dict):
            continue
        for key, bucket in (
            ("mem:avail_mib", avail),
            ("mem:swap_used_mib", swap),
            ("psi:memory_full_avg10", psi),
        ):
            v = _num(sysd.get(key))
            if v is not None:
                bucket.append(v)
    return {
        "min_avail_mib": min(avail) if avail else None,
        "max_swap_used_mib": max(swap) if swap else None,
        "peak_psi_memory_full": max(psi) if psi else None,
    }


def _reboot_entry(
    boot: str,
    new: str | None,
    rec: dict,
    tail: list[dict],
    window_s: float,
    events: list[dict] | None = None,
) -> dict:
    last = tail[-1]
    recent = [r for r in tail if last["t"] - r["t"] <= window_s]
    temps = [r["temp_c"] for r in recent if r.get("temp_c") is not None]
    return {
        "previous_boot": boot,
        "new_boot": new,
        "last_record_utc": _utc(last["t"]),
        "restart_utc": _utc(rec["t"]),
        "gap_s": round(rec["t"] - last["t"], 1),
        "window_s": window_s,
        "max_temp_c": max(temps) if temps else None,
        "last": {k: last.get(k) for k in ("temp_c", "effective_c", "level", "duty", "cpus", "gpu")},
        "last_zones": last.get("zones"),
        "levels": dict(Counter(r.get("level") for r in recent)),
        "mem": _mem_block(recent),
        "events": [e for e in (events or []) if last["t"] - EVENT_LOOKBACK_S <= e["t"] <= rec["t"]],
    }


def report(directory: Path, *, window_s: float = 60.0) -> list[dict]:
    """One entry per reboot seen in the records: the gap and what came before."""
    out: list[dict] = []
    boot: str | None = None
    tail: list[dict] = []
    events: list[dict] = []
    for rec in iter_records(directory):
        if rec.get("event") == "start":
            new = rec.get("boot_id")
            if boot is not None and new != boot and tail:
                out.append(_reboot_entry(boot, new, rec, tail, window_s, events))
            boot, tail, events = new, [], []
        elif rec.get("event") in MEMORY_EVENTS and _num(rec.get("t")) is not None:
            _append_capped(events, rec, cap=500, drop=250)
        elif "temp_c" in rec and "t" in rec:
            _append_capped(tail, rec, cap=4000, drop=2000)
    return out


def _append_capped(rows: list[dict], rec: dict, *, cap: int, drop: int) -> None:
    """Append ``rec``; past ``cap`` rows, drop the oldest ``drop``."""
    rows.append(rec)
    if len(rows) > cap:
        del rows[:drop]


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- replay ----------------------------------------------------------------


def _float_or_none(v: object) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _t1s_samples(lines: Iterable[str]) -> Iterator[tuple]:
    """Yield (uptime_s, hottest zone, gpu temp) from a ``t1s-*.log``; Nones at a START marker."""
    cols: list[str] | None = None
    for line in lines:
        if line.startswith("#"):
            yield None, None, None
            continue
        parts = line.split()
        if cols is None or parts[:1] == ["utc"]:
            cols = parts
            continue
        row = dict(zip(cols, parts))
        up = _float_or_none(row.get("up_s"))
        if up is None:
            continue
        zone_temps = (
            _float_or_none(v) for k, v in row.items() if k.startswith("z") and k.endswith("_c")
        )
        temps = [x for x in zone_temps if x is not None]
        yield up, (max(temps) if temps else None), _float_or_none(row.get("gpu_c", ""))


def _jsonl_samples(lines: Iterable[str]) -> Iterator[tuple]:
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == "start":
            yield None, None, None
        elif "t" in rec and "temp_c" in rec:
            gpu = (rec.get("gpu") or {}).get("temp_c")
            yield float(rec["t"]), rec.get("board_temp_c", rec.get("temp_c")), gpu


def _max_or(current: float | None, value: float | None) -> float | None:
    if value is None:
        return current
    return value if current is None else max(current, value)


class _Tally:
    """What :func:`replay` accumulates over a trace."""

    def __init__(self) -> None:
        self.seconds: Counter[str] = Counter()
        self.by_source: Counter[str] = Counter()
        self.duty_s = 0.0
        self.held_s = 0.0
        self.n = 0
        self.max_temp: float | None = None
        self.max_eff: float | None = None
        self.boots = 0
        self.before_restart: list[dict] = []
        self.last_decisions: list[dict] = []

    def restart(self) -> None:
        if self.last_decisions:
            self.before_restart.append({"last_decisions": self.last_decisions[-10:]})
        self.boots += 1
        self.last_decisions = []

    def add(self, t: float, temp: float | None, d, dt: float) -> None:
        self.by_source[d.source] += dt
        self.n += 1
        self.seconds[d.level] += dt
        self.duty_s += d.duty * dt
        self.held_s += 0.0 if d.admit else dt
        self.max_temp = _max_or(self.max_temp, temp)
        self.max_eff = _max_or(self.max_eff, d.effective_c)
        self.last_decisions.append({"t": t, **d.as_dict()})
        if len(self.last_decisions) > 60:
            del self.last_decisions[:30]


def replay(path: Path, profile: Profile) -> dict:
    """Run the controller over a trace; what would it have decided?"""
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    first = next((ln for ln in lines if ln.strip()), "")
    samples = _jsonl_samples(lines) if first.lstrip().startswith("{") else _t1s_samples(lines)
    ctrl = Controller(profile)
    tally = _Tally()
    prev_t: float | None = None
    for t, temp, gpu in samples:
        if t is None:  # a (re)start: the controller starts fresh like the daemon would
            tally.restart()
            ctrl = Controller(profile)
            prev_t = None
            continue
        if prev_t is not None and t <= prev_t:
            continue
        dt = 1.0 if prev_t is None else min(t - prev_t, 5.0)
        prev_t = t
        tally.add(t, temp, ctrl.step(temp, t, gpu), dt)
    total = sum(tally.seconds.values()) or 1.0
    return {
        "profile": profile.name,
        "samples": tally.n,
        "starts_in_trace": tally.boots,
        "seconds_by_level": dict(tally.seconds),
        "seconds_by_source": dict(tally.by_source),
        "share_held": round(tally.held_s / total, 4),
        "mean_duty": round(tally.duty_s / total, 4),
        "max_temp_c": tally.max_temp,
        "max_effective_c": tally.max_eff,
        "before_each_restart": tally.before_restart,
    }


# -- per-sensor statistics -------------------------------------------------------


def parse_when(text: str | None, *, now: float | None = None) -> float | None:
    """``2026-10-23T16:00:00Z``, ``16:00`` / ``16:00:30`` (today, UTC), an epoch
    number, or a relative ``-30m`` / ``-2h`` / ``-90s``."""
    if not text:
        return None
    now = time.time() if now is None else now
    t = text.strip()
    if t.startswith("-") and t[-1] in "smh" and t[1:-1].replace(".", "", 1).isdigit():
        return now - float(t[1:-1]) * {"s": 1, "m": 60, "h": 3600}[t[-1]]
    try:
        return float(t)
    except ValueError:
        pass
    if "T" in t:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    hms = t if t.count(":") == 2 else t + ":00"
    return datetime.fromisoformat(f"{day}T{hms}+00:00").timestamp()


def _is_temp(name: str) -> bool:
    if name.startswith("nvme:"):
        return not name.endswith(("_max_c", "_crit_c"))  # the drive's limits are not readings
    return name.startswith("zone:") or name == "gpu:temp_c"  # limit_c is a limit, not a reading


def _in_window(t: float, since: float | None, until: float | None) -> bool:
    return (since is None or t >= since) and (until is None or t <= until)


def _collect_series(
    directory: Path, since: float | None, until: float | None
) -> tuple[dict[str, list[float]], float | None, float | None, int]:
    """Every numeric sensor's values in the window, the window's first and last
    record times, and how many ticks the operator's GPU ceiling held the policy."""
    series: dict[str, list[float]] = {}
    first = last = None
    held = 0
    for rec in iter_records(directory):
        t = rec.get("t")
        if rec.get("event") or "temp_c" not in rec or t is None or not _in_window(t, since, until):
            continue
        first = t if first is None else first
        last = t
        held += rec.get("gpu_held_by_operator") is True
        sensors = flat_sensors(rec.get("zones"), rec.get("gpu"), rec.get("fans"), rec.get("system"))
        for name, v in sensors.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                series.setdefault(name, []).append(float(v))
    return series, first, last, held


def _stats_row(name: str, vals: list[float], above_c: float | None) -> dict:
    vals = sorted(vals)
    n = len(vals)
    row = {
        "samples": n,
        "min": round(vals[0], 2),
        "mean": round(sum(vals) / n, 2),
        "p95": round(vals[min(n - 1, int(0.95 * (n - 1) + 0.5))], 2),
        "max": round(vals[-1], 2),
    }
    if above_c is not None and _is_temp(name):
        row["s_at_or_above"] = sum(1 for v in vals if v >= above_c)
    return row


def sensor_stats(
    directory: Path,
    *,
    since: float | None = None,
    until: float | None = None,
    above_c: float | None = None,
) -> dict:
    """Per sensor over [since, until]: samples, min, mean, p95, max, and for
    temperature sensors the seconds at or above ``above_c``; and
    ``gpu_held_by_operator_ticks``, the ticks on which the policy wanted a GPU
    ceiling above the operator's (it would have happened)."""
    series, first, last, held = _collect_series(directory, since, until)
    return {
        "from_utc": _utc(first) if first else None,
        "to_utc": _utc(last) if last else None,
        "above_c": above_c,
        "gpu_held_by_operator_ticks": held,
        "sensors": {name: _stats_row(name, series[name], above_c) for name in sorted(series)},
    }

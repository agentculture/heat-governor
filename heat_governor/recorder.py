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
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from heat_governor.platforms import Profile
from heat_governor.policy import Controller


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
        fh = self._file(t)
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        fh.flush()
        if self.fsync:
            os.fsync(fh.fileno())

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def iter_records(directory: Path) -> Iterator[dict]:
    for path in sorted(directory.glob("*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue  # a torn last line after a power loss


def report(directory: Path, *, window_s: float = 60.0) -> list[dict]:
    """One entry per reboot seen in the records: the gap and what came before."""
    out: list[dict] = []
    boot: str | None = None
    tail: list[dict] = []
    for rec in iter_records(directory):
        if rec.get("event") == "start":
            new = rec.get("boot_id")
            if boot is not None and new != boot and tail:
                last = tail[-1]
                recent = [r for r in tail if last["t"] - r["t"] <= window_s]
                temps = [r["temp_c"] for r in recent if r.get("temp_c") is not None]
                out.append(
                    {
                        "previous_boot": boot,
                        "new_boot": new,
                        "last_record_utc": _utc(last["t"]),
                        "restart_utc": _utc(rec["t"]),
                        "gap_s": round(rec["t"] - last["t"], 1),
                        "window_s": window_s,
                        "max_temp_c": max(temps) if temps else None,
                        "last": {
                            k: last.get(k)
                            for k in ("temp_c", "effective_c", "level", "duty", "cpus", "gpu")
                        },
                        "last_zones": last.get("zones"),
                        "levels": dict(Counter(r.get("level") for r in recent)),
                    }
                )
            boot, tail = new, []
            continue
        if "temp_c" in rec and "t" in rec:
            tail.append(rec)
            if len(tail) > 4000:
                del tail[:2000]
    return out


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- replay ----------------------------------------------------------------


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
        try:
            up = float(row["up_s"])
        except (KeyError, ValueError):
            continue
        temps = []
        for k, v in row.items():
            if k.startswith("z") and k.endswith("_c"):
                try:
                    temps.append(float(v))
                except ValueError:
                    continue
        try:
            gpu = float(row.get("gpu_c", ""))
        except ValueError:
            gpu = None
        yield up, (max(temps) if temps else None), gpu


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


def replay(path: Path, profile: Profile) -> dict:
    """Run the controller over a trace; what would it have decided?"""
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    first = next((ln for ln in lines if ln.strip()), "")
    samples = _jsonl_samples(lines) if first.lstrip().startswith("{") else _t1s_samples(lines)
    ctrl = Controller(profile)
    seconds: Counter[str] = Counter()
    duty_s = 0.0
    held_s = 0.0
    n = 0
    max_temp = max_eff = None
    boots = 0
    before_restart: list[dict] = []
    last_decisions: list[dict] = []
    prev_t: float | None = None
    by_source: Counter[str] = Counter()
    for t, temp, gpu in samples:
        if t is None:  # a (re)start: the controller starts fresh like the daemon would
            if last_decisions:
                before_restart.append({"last_decisions": last_decisions[-10:]})
            boots += 1
            ctrl = Controller(profile)
            prev_t = None
            last_decisions = []
            continue
        if prev_t is not None and t <= prev_t:
            continue
        dt = 1.0 if prev_t is None else min(t - prev_t, 5.0)
        prev_t = t
        d = ctrl.step(temp, t, gpu)
        by_source[d.source] += dt
        n += 1
        seconds[d.level] += dt
        duty_s += d.duty * dt
        held_s += 0.0 if d.admit else dt
        if temp is not None:
            max_temp = temp if max_temp is None else max(max_temp, temp)
        if d.effective_c is not None:
            max_eff = d.effective_c if max_eff is None else max(max_eff, d.effective_c)
        last_decisions.append({"t": t, **d.as_dict()})
        if len(last_decisions) > 60:
            del last_decisions[:30]
    total = sum(seconds.values()) or 1.0
    return {
        "profile": profile.name,
        "samples": n,
        "starts_in_trace": boots,
        "seconds_by_level": dict(seconds),
        "seconds_by_source": dict(by_source),
        "share_held": round(held_s / total, 4),
        "mean_duty": round(duty_s / total, 4),
        "max_temp_c": max_temp,
        "max_effective_c": max_eff,
        "before_each_restart": before_restart,
    }

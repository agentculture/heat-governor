"""The adaptive user-unit loop: a feedback loop whose step sizes come from the
data it gathers, not from fixed CPU counts.

``mode = "levels"`` caps a unit at a fixed CPU count per temperature level.
``mode = "adaptive"`` instead measures how the governing temperature's RATE
responds to the CPU the loop takes away from the unit, learns that response
per unit (``gain``: degrees C per second of rate, per CPU removed), and sizes
each change from it. What it learned is kept across restarts
(:class:`ModelStore`, ``adaptive.json`` in the daemon's data dir).

Everything here is pure except :class:`ModelStore` (one JSON file) -- no
systemctl, no clock of its own. The daemon feeds :meth:`AdaptiveLoop.step` the
level, the temperature trend over the last interval (:class:`Trend`: the
least-squares slope of every governing reading, ``None`` on sensor loss), the
unit's measured CPU use over the same interval, the host CPU count and the time, and hands the
resulting cap to the existing :class:`heat_governor.units.UnitCapper`, so the
ledger-first, never-raise-a-tighter-original, dry-run, missing-unit and
restore rules apply unchanged.

Rules, evaluated at most once per ``interval_s``:

* **engaged** (level >= ``engage``): ``error = rate - target``. Above the
  deadband: TIGHTEN by ``error / gain`` CPUs once a gain is learned, else the
  cold-start step ``max(1, 25% of use)``; the step is clamped to
  ``[1 CPU, 50% of use]``; the new cap is ``max(min_cpus, use - step)`` and
  never above the current cap. Below minus the deadband at exactly the engage
  level: RELEASE one step (``max(1, 10% of host CPUs)``), at most once per
  ``2 x interval_s``. Otherwise HOLD.
* **not engaged**: RELEASE one step per ``2 x interval_s``; at host CPUs the
  cap is removed (the unit's own quota is restored).
* **sensor loss** (rate unknown): engaged -> the cold-start step (no gain can
  be applied to an unknown error, and nothing is learned); not engaged ->
  HOLD (never release while blind).
* **learning**: a TIGHTEN arms one sample ``(time, use, rate)``; the next
  evaluation takes ``(rate_before - rate_now) / (use_before - use_now)`` when
  use fell by >= 0.5 CPU and the rate fell, else counts it ``ignored``. The
  gain is an EWMA (alpha 0.3) of the samples, clamped to [1e-4, 0.05]. Nothing
  is armed in dry-run: a change that is not made has no response to measure.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

LEVELS = ("cool", "warm", "hot", "critical")
DEADBAND = 0.005  # C/s: the smallest band; the measured trend noise widens it
NOISE_K = 2.0  # an error or a learned change must exceed NOISE_K x its standard error
IDLE_USE = 0.5  # CPUs: a unit using less cannot be the heat source -- never cut it
BINDING_SHARE = 0.5  # a cap the unit uses less than this share of is not binding
ALPHA = 0.3  # EWMA weight of a new sample
GAIN_MIN, GAIN_MAX = 1e-4, 0.5  # C/s per CPU (live Thor samples reached 0.16)
MIN_DELTA_USE = 0.5  # CPUs: a smaller change in use teaches nothing
COLD_START_SHARE = 0.25  # of measured use, before a gain is learned
MAX_STEP_SHARE = 0.5  # of measured use, the largest single cut
RELEASE_SHARE = 0.10  # of host CPUs per release step

Log = Callable[[str], None]


@dataclass
class Model:
    """What the loop learned about one unit on one platform."""

    gain: float | None = None  # C/s of rate per CPU removed
    samples: int = 0
    ignored: int = 0
    updated: float | None = None  # epoch seconds of the last accepted sample

    def add(self, sample: float, now: float) -> None:
        g = sample if self.gain is None else ALPHA * sample + (1 - ALPHA) * self.gain
        self.gain = min(GAIN_MAX, max(GAIN_MIN, g))
        self.samples += 1
        self.updated = now

    def as_dict(self) -> dict:
        return {
            "gain": self.gain,
            "samples": self.samples,
            "ignored": self.ignored,
            "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Model":
        gain, upd = d.get("gain"), d.get("updated")
        samples, ignored = d.get("samples", 0), d.get("ignored", 0)
        if gain is not None and not _num(gain):
            raise ValueError(f"gain {gain!r}")
        if upd is not None and not _num(upd):
            raise ValueError(f"updated {upd!r}")
        if not (isinstance(samples, int) and isinstance(ignored, int)):
            raise ValueError("samples / ignored must be integers")
        g = None if gain is None else min(GAIN_MAX, max(GAIN_MIN, float(gain)))
        return cls(g, max(0, samples), max(0, ignored), None if upd is None else float(upd))


def _num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt(c: float | None) -> str:
    return "none" if c is None else f"{c:g}"


@dataclass(frozen=True)
class Step:
    """One evaluation's outcome. ``action``: tighten | release | hold | wait."""

    action: str
    cap: float | None
    prev: float | None
    use: float | None
    reason: str
    learned: bool = False
    lesson: str | None = None  # what the evaluation's sample taught (or why it was ignored)

    @property
    def changed(self) -> bool:
        return self.action in ("tighten", "release")

    def describe(self, unit: str, *, dry: bool = False) -> str:
        frm = self.prev if self.prev is not None else self.use
        pre = "dry-run would " if dry else ""
        tail = "; not learning in dry-run" if dry and self.action == "tighten" else ""
        return (
            f"adaptive {unit}: {pre}{self.action} {_fmt(frm)} -> {_fmt(self.cap)}"
            f"{' cpus' if self.cap is not None else ''} ({self.reason}{tail})"
        )


@dataclass
class AdaptiveLoop:
    """One unit's loop state. ``cap`` None = uncapped (the unit's own quota)."""

    min_cpus: float = 1.0
    engage: str = "warm"
    interval_s: float = 20.0
    target_rate_c_min: float = 0.0
    model: Model = field(default_factory=Model)
    cap: float | None = None
    evaluated_at: float | None = None
    adjusted_at: float | None = None
    pending: tuple[float, float, float, float] | None = None  # (time, use, rate, noise)
    last_use: float | None = None
    _now: float = field(default=0.0, repr=False, compare=False)

    def configure(self, min_cpus, engage, interval_s, target_rate_c_min) -> None:
        """A config reload: new parameters, same cap and learned model."""
        self.min_cpus, self.engage = min_cpus, engage
        self.interval_s, self.target_rate_c_min = interval_s, target_rate_c_min

    @property
    def target_c_s(self) -> float:
        return self.target_rate_c_min / 60.0

    def due(self, now: float) -> bool:
        return self.evaluated_at is None or now - self.evaluated_at >= self.interval_s

    # -- pieces ---------------------------------------------------------------
    def band(self, noise: float | None) -> float:
        """The deadband: the measured trend noise, never below DEADBAND."""
        return max(DEADBAND, NOISE_K * noise) if noise else DEADBAND

    def _learn(
        self, rate: float | None, use: float | None, noise: float | None
    ) -> tuple[bool, str | None]:
        if self.pending is None:
            return False, None
        _, use0, rate0, noise0 = self.pending
        self.pending = None
        if rate is None or use is None:
            self.model.ignored += 1
            return False, "sample ignored: rate or use unknown"
        d_use, d_rate = use0 - use, rate0 - rate
        # the change must stand out from both trends' own noise, or it teaches noise
        need = max(0.0, NOISE_K * ((noise0 or 0.0) ** 2 + (noise or 0.0) ** 2) ** 0.5)
        if d_use >= MIN_DELTA_USE and d_rate > need:
            self.model.add(d_rate / d_use, self._now)
            return True, (
                f"learned gain {self.model.gain:.4f} C/s/cpu from {self.model.samples} samples "
                f"(rate fell {d_rate:.3f} C/s for {d_use:g} cpus)"
            )
        self.model.ignored += 1
        return False, (
            f"sample ignored: use fell {d_use:g} cpus, rate fell {d_rate:+.3f} C/s "
            f"(needs >= {MIN_DELTA_USE:g} cpus and a fall above the noise {need:.3f} C/s; "
            f"{self.model.ignored} ignored)"
        )

    def _basis(self) -> str:
        m = self.model
        if m.gain is not None and m.samples >= 1:
            return f"gain {m.gain:.4f} C/s/cpu from {m.samples} samples"
        return "cold start: 25% of use"

    def _tighten(self, use: float, error: float | None, why: str) -> Step:
        if error is not None and self.model.gain is not None and self.model.samples >= 1:
            cut = error / self.model.gain
        else:
            cut = max(1.0, COLD_START_SHARE * use)
        cut = min(max(cut, 1.0), max(1.0, MAX_STEP_SHARE * use))
        new = round(max(self.min_cpus, use - cut), 2)
        if self.cap is not None:
            new = min(new, self.cap)
        if new == self.cap:
            where = "at the floor min_cpus" if new <= self.min_cpus else "at the current cap"
            return Step("hold", self.cap, self.cap, use, f"{why}; {where}")
        prev, self.cap, self.adjusted_at = self.cap, new, self._now
        return Step("tighten", new, prev, use, why)

    def _release(self, host_cpus: int, why: str) -> Step:
        if self.cap is None:
            return Step("hold", None, None, self.last_use, why)
        if self.adjusted_at is not None and self._now - self.adjusted_at < 2 * self.interval_s:
            return Step("hold", self.cap, self.cap, self.last_use, f"{why}; release paced")
        prev = self.cap
        new: float | None = round(prev + max(1.0, RELEASE_SHARE * host_cpus), 2)
        if new >= host_cpus:
            new = None
            why += ": unit's own quota restored"
        self.cap, self.adjusted_at = new, self._now
        return Step("release", new, prev, self.last_use, why)

    # -- the step -------------------------------------------------------------
    def step(
        self,
        level: str,
        rate: float | None,
        use: float | None,
        host_cpus: int,
        now: float,
        *,
        learn: bool = True,
        noise: float | None = None,
    ) -> Step:
        """One evaluation. ``rate`` in C/s (None = sensor lost); ``use`` in CPUs
        over the last interval (None = not measured yet / unreadable); ``noise``
        the standard error of ``rate`` (it sets the deadband)."""
        if not self.due(now):
            return Step("wait", self.cap, self.cap, self.last_use, "between evaluations")
        self._now = now
        self.evaluated_at = now
        learned, lesson = self._learn(rate, use, noise)
        if use is not None:
            self.last_use = use
        s = self._decide(level, rate, use, host_cpus, self.band(noise))
        if s.action == "tighten" and learn and rate is not None and use is not None:
            self.pending = (now, use, rate, noise or 0.0)
        if lesson is not None:
            s = Step(s.action, s.cap, s.prev, s.use, s.reason, learned, lesson)
        return s

    def _decide(
        self, level: str, rate: float | None, use: float | None, host: int, band: float = DEADBAND
    ) -> Step:
        engaged = LEVELS.index(level) >= LEVELS.index(self.engage)
        if rate is None:
            return self._decide_blind(engaged, use)
        if not engaged:
            return self._release(host, f"level {level}, below engage {self.engage}")
        error = rate - self.target_c_s
        head = f"rate {rate:+.3f} C/s, target {self.target_c_s:+.3f} C/s, noise band {band:.3f}"
        if error > band:
            return self._decide_hot(use, error, host, head)
        slack = use is not None and self.cap is not None and use < BINDING_SHARE * self.cap
        if slack:
            return self._release(host, f"{head}; use {use:g} of cap {self.cap:g}: cap not binding")
        if error < -band and level == self.engage:
            return self._release(host, f"{head}, cooling faster than needed")
        return Step("hold", self.cap, self.cap, use, f"{head}, within the noise band")

    def _decide_blind(self, engaged: bool, use: float | None) -> Step:
        """Sensor lost: never release blind; tighten from use once it is known."""
        if not engaged:
            return Step("hold", self.cap, self.cap, use, "sensor lost: never release blind")
        if use is None:
            return Step("hold", self.cap, self.cap, None, "sensor lost; measuring use")
        return self._tighten(use, None, f"use {use:g}, sensor lost: cold-start step")

    def _decide_hot(self, use: float | None, error: float, host: int, head: str) -> Step:
        """Heating faster than the target (beyond the noise band)."""
        if use is None:
            return Step("hold", self.cap, self.cap, None, f"{head}; measuring use")
        if use < IDLE_USE:
            # the heat is not this unit's: it is not using the CPU
            if self.cap is not None:
                return self._release(host, f"{head}; use {use:g}: not the heat source")
            return Step("hold", None, None, use, f"{head}; use {use:g}: not the heat source")
        return self._tighten(use, error, f"use {use:g}, {head}, {self._basis()}")


class Trend:
    """The temperature trend the loop acts on: the least-squares slope (C/s)
    of every governing reading since the last evaluation. The controller's own
    ``rate_c_s`` spans ``rate_window_s`` (2 s on the Spark, whose hot zone jumps
    2-5 C per second): one such reading per ``interval_s`` is mostly noise, and
    a gain learned from noise would size every step wrongly. ``None`` with fewer
    than three readings or a sensor loss in the window."""

    MIN_POINTS = 3

    def __init__(self) -> None:
        self.points: list[tuple[float, float]] = []
        self.blind = False
        self.noise: float | None = None  # standard error of the last slope taken

    def add(self, now: float, temp: float | None) -> None:
        if temp is None:
            self.blind = True
        else:
            self.points.append((now, temp))

    def take(self) -> float | None:
        """The slope over the readings so far, then start a new window."""
        pts, blind = self.points, self.blind
        self.points, self.blind, self.noise = [], False, None
        if blind or len(pts) < self.MIN_POINTS:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mc = sum(c for _, c in pts) / n
        var = sum((t - mt) ** 2 for t, _ in pts)
        if var <= 0:
            return None
        slope = sum((t - mt) * (c - mc) for t, c in pts) / var
        resid = sum((c - mc - slope * (t - mt)) ** 2 for t, c in pts)
        self.noise = round((resid / (n - 2) / var) ** 0.5, 5) if n > 2 else None
        return round(slope, 5)


class UsageMeter:
    """CPUs a unit used between two samples: delta cumulative CPU time over
    delta wall time. ``read(unit)`` returns usage_usec or None."""

    def __init__(self, read: Callable[[str], "int | None"]) -> None:
        self.read = read
        self._last: dict[str, tuple[float, int]] = {}

    def sample(self, unit: str, now: float) -> float | None:
        usec = self.read(unit)
        if usec is None:
            self._last.pop(unit, None)
            return None
        last = self._last.get(unit)
        self._last[unit] = (now, usec)
        if last is None or now <= last[0] or usec < last[1]:
            return None  # baseline, or the counter reset (the unit restarted)
        return round((usec - last[1]) / ((now - last[0]) * 1_000_000), 3)

    def forget(self, unit: str) -> None:
        self._last.pop(unit, None)


class ModelStore:
    """``{"version": 1, "platforms": {platform: {unit: model}}}``, written
    atomically like the ledger. A corrupt file is logged and replaced."""

    VERSION = 1

    def __init__(self, path: Path, log: Log) -> None:
        self.path = path
        self.log = log
        self.models: dict[str, dict[str, Model]] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            text = self.path.read_text()
        except OSError:
            return
        try:
            data = json.loads(text)
            self.models = {
                plat: {unit: Model.from_dict(m) for unit, m in units.items()}
                for plat, units in data["platforms"].items()
            }
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.models = {}
            self.log(f"adaptive: {self.path} is unreadable ({exc!r}): starting fresh")

    def model(self, platform: str, unit: str) -> Model:
        return self.models.setdefault(platform, {}).setdefault(unit, Model())

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            doc = {
                "version": self.VERSION,
                "platforms": {
                    p: {u: m.as_dict() for u, m in units.items()}
                    for p, units in self.models.items()
                },
            }
            tmp.write_text(json.dumps(doc, indent=1, sort_keys=True))
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)

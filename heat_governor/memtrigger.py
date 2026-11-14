"""Memory-pressure trigger: pure, no I/O.

Watches ``MemAvailable`` and fires when memory is about to run out. Two reasons:

- ``floor``: available memory is below ``floor_pct`` of total.
- ``horizon``: available memory is below ``soft_pct`` of total *and* the fill rate says it
  runs out in under ``horizon_s`` seconds (``avail / rate < horizon_s``).

The fill rate is the least-squares slope of the samples in the last ``window_s`` seconds, so
one noisy sample moves it only a little. A condition must also hold on two consecutive
samples before it fires. After a fire the trigger is disarmed until available memory
recovers above the soft threshold, so one wave of pressure fires once.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable, Iterator

MIN_SAMPLES = 3  # a slope needs at least this many samples in the window
CONFIRM = 2  # consecutive samples a condition must hold for before it fires


@dataclass(frozen=True)
class Fire:
    t: float
    reason: str  # "floor" or "horizon"
    avail_mib: float
    rate_mib_s: float  # fill rate (positive = memory being consumed); 0.0 if unknown


def fill_rate(samples: Iterable[tuple[float, float]]) -> float:
    """Least-squares fill rate in MiB/s (positive when avail is falling); 0.0 if undefined."""
    pts = list(samples)
    n = len(pts)
    if n < MIN_SAMPLES:
        return 0.0
    tm = sum(t for t, _ in pts) / n
    am = sum(a for _, a in pts) / n
    sxx = sum((t - tm) ** 2 for t, _ in pts)
    if sxx <= 0.0:
        return 0.0
    sxy = sum((t - tm) * (a - am) for t, a in pts)
    return -sxy / sxx


class Trigger:
    def __init__(
        self,
        total_mib: float,
        floor_pct: float = 5.0,
        horizon_s: float = 10.0,
        soft_pct: float = 20.0,
        window_s: float = 10.0,
    ) -> None:
        if total_mib <= 0:
            raise ValueError("total_mib must be positive")
        self.total_mib = float(total_mib)
        self.floor_mib = self.total_mib * floor_pct / 100.0
        self.soft_mib = self.total_mib * soft_pct / 100.0
        self.horizon_s = float(horizon_s)
        self.window_s = float(window_s)
        self._win: deque[tuple[float, float]] = deque()
        self._armed = True
        self._streak = 0

    def step(self, t: float, avail_mib: float) -> Fire | None:
        win = self._win
        win.append((t, float(avail_mib)))
        while win and win[0][0] < t - self.window_s:
            win.popleft()
        rate = fill_rate(win)
        if avail_mib >= self.soft_mib:
            self._armed = True
        reason = self._reason(avail_mib, rate)
        self._streak = self._streak + 1 if reason else 0
        if not (reason and self._armed and self._streak >= CONFIRM):
            return None
        self._armed = False
        return Fire(t=t, reason=reason, avail_mib=float(avail_mib), rate_mib_s=max(rate, 0.0))

    def _reason(self, avail: float, rate: float) -> str | None:
        if avail < self.floor_mib:
            return "floor"
        if avail < self.soft_mib and rate > 0.0 and avail / rate < self.horizon_s:
            return "horizon"
        return None


def replay(rows: Iterable[tuple[float, float]], total_mib: float, **params) -> Iterator[Fire]:
    """Run a fresh Trigger over ``(t, avail_mib)`` rows, yielding each fire."""
    trig = Trigger(total_mib, **params)
    for t, avail in rows:
        fire = trig.step(t, avail)
        if fire is not None:
            yield fire

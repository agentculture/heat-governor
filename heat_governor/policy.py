"""The pure controller: temperature in, decision out. No I/O, no clock of its own.

Per step (about once a second) the controller takes the governing temperature
(the hottest watched zone, or ``None`` when every zone is unreadable) and the
time, and returns a :class:`Decision`:

* ``effective_c`` = temp + ``lookahead_s`` x the rising rate over the last
  ``rate_window_s`` seconds. The Spark's hot zone moves 2-5 C per second, so a
  controller that only sees the present temperature acts a second or two late.
* ``level`` -- ``cool`` < warm_c <= ``warm`` < hot_c <= ``hot`` < crit_c <=
  ``critical``. Levels RISE at once on the effective temperature and FALL only
  after the real temperature has stayed ``hysteresis_c`` below the threshold for
  ``dwell_s`` seconds.
* ``admit`` / ``concurrency_factor`` -- the load-side signal for clients:
  cool 1.0, warm 0.5, hot and critical 0 (hold new work; running work finishes).
* ``duty`` -- the fraction of each duty period a *batch* container is paused,
  from the profile's curve on the effective temperature. It rises at once and
  falls at most ``duty_decay_per_s`` per second, so it does not flap.
* ``cpus`` -- the CPU cap from the bands; a drop is immediate, a rise moves one
  band per ``ramp_s`` (the gradual warm-up the operator asked for: 4 -> 8 -> 12,
  never 4 -> 12 in one step).

Sensor loss (``temp_c is None``) is treated as **hot**: hold new work, the
duty curve's value at hot_c, the lowest CPU band. It is never treated as cool.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass

from heat_governor.platforms import Curve, Limits, Profile

LEVELS = ("cool", "warm", "hot", "critical")


@dataclass(frozen=True)
class Decision:
    level: str
    admit: bool
    concurrency_factor: float
    duty: float
    cpus: int | None  # None = no cap (restore the container's own limit)
    temp_c: float | None
    effective_c: float | None
    rate_c_s: float
    reason: str
    gpu_mhz: int | None = None  # GPU clock ceiling; None = no ceiling
    source: str = "board"  # which input governs: "board" or "gpu"
    gpu_temp_c: float | None = None  # the GPU's own reading (before mapping)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def interp(curve: Curve, x: float) -> float:
    """Piecewise-linear, clamped at both ends."""
    if not curve:
        return 0.0
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y1
    return curve[-1][1]


def gpu_on_board_scale(gpu_c: float, gpu: Limits, board: Limits) -> float:
    """Map a GPU temperature onto the board's scale so one set of bands serves
    both: GPU warm/hot/crit land on board warm/hot/crit, linear between them,
    slope 1 outside (5 C under GPU-warm reads 5 C under board-warm)."""
    pts = ((gpu.warm_c, board.warm_c), (gpu.hot_c, board.hot_c), (gpu.crit_c, board.crit_c))
    if gpu_c <= pts[0][0]:
        return board.warm_c - (gpu.warm_c - gpu_c)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if gpu_c <= x1:
            return y0 + (y1 - y0) * (gpu_c - x0) / (x1 - x0)
    return board.crit_c + (gpu_c - gpu.crit_c)


def level_for(temp: float, profile: Profile) -> str:
    lim = profile.limits
    if temp >= lim.crit_c:
        return "critical"
    if temp >= lim.hot_c:
        return "hot"
    if temp >= lim.warm_c:
        return "warm"
    return "cool"


def _threshold(level: str, profile: Profile) -> float:
    lim = profile.limits
    return {"warm": lim.warm_c, "hot": lim.hot_c, "critical": lim.crit_c}.get(level, -1e9)


def band_cpus(temp: float, profile: Profile) -> int | None:
    return _band(temp, profile.cpu_bands)


def _band(temp: float, bands) -> int | None:
    cap: int | None = None
    for from_c, value in bands:
        if temp >= from_c:
            cap = value
    return cap


class _Ramp:
    """A ceiling that drops at once and rises one level per ``ramp_s``;
    ``None`` (no ceiling) is the top of the ladder. A fresh start applies the
    wanted level directly (the workload is already running at full speed)."""

    def __init__(self, levels: list[int]) -> None:
        self.levels = sorted(set(levels))
        self.cur: int | None = None
        self.at: float | None = None
        self.started = False

    @staticmethod
    def _rank(c: int | None) -> float:
        return float("inf") if c is None else float(c)

    def step(self, want: int | None, now: float, ramp_s: float) -> int | None:
        if not self.started:
            self.started, self.cur, self.at = True, want, now
            return self.cur
        if self._rank(want) <= self._rank(self.cur):
            if want != self.cur:
                self.cur, self.at = want, now
            return self.cur
        if self.at is not None and now - self.at < ramp_s:
            return self.cur
        higher = [c for c in self.levels if self._rank(c) > self._rank(self.cur)]
        nxt: int | None = higher[0] if higher else None
        self.cur = want if self._rank(want) <= self._rank(nxt) else nxt
        self.at = now
        return self.cur

    def force(self, value: int | None, now: float) -> int | None:
        self.started, self.cur, self.at = True, value, now
        return value


class Controller:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self._hist: deque[tuple[float, float]] = deque()
        self._ghist: deque[tuple[float, float]] = deque()
        self.level = "cool"
        self._below_since: float | None = None
        self.duty = 0.0
        self._duty_at: float | None = None
        self._cpu = _Ramp([c for _, c in profile.cpu_bands])
        self._gpu = _Ramp([m for _, m in profile.gpu_clock_bands])

    def set_profile(self, profile: Profile) -> None:
        """Swap the profile in place (a config reload), keeping the history, the
        current level, duty and CPU cap, so a reload causes no jump."""
        self.profile = profile
        self._cpu.levels = sorted({c for _, c in profile.cpu_bands})
        self._gpu.levels = sorted(m for _, m in profile.gpu_clock_bands)

    # -- pieces -------------------------------------------------------------
    def _rate(self, temp: float, now: float, hist: "deque | None" = None) -> float:
        hist = self._hist if hist is None else hist
        hist.append((now, temp))
        while hist and now - hist[0][0] > self.profile.rate_window_s:
            hist.popleft()
        t0, c0 = hist[0]
        # at least a 1 s base: two samples close together must not extrapolate a
        # sensor step into an absurd rate
        return (temp - c0) / max(now - t0, 1.0)

    def _level(self, temp: float, eff: float, now: float) -> str:
        want = level_for(eff, self.profile)
        if LEVELS.index(want) >= LEVELS.index(self.level):
            self.level = want
            self._below_since = None
            return self.level
        # falling: the REAL temperature must stay hysteresis below the current
        # level's threshold for dwell_s before stepping down one level
        lim = self.profile.limits
        if temp < _threshold(self.level, self.profile) - lim.hysteresis_c:
            if self._below_since is None:
                self._below_since = now
            elif now - self._below_since >= lim.dwell_s:
                self.level = LEVELS[LEVELS.index(self.level) - 1]
                self._below_since = now if self.level != want else None
        else:
            self._below_since = None
        return self.level

    def _duty(self, target: float, now: float) -> float:
        if self._duty_at is None or target >= self.duty:
            self.duty = target
        else:
            dt = now - self._duty_at
            self.duty = max(target, self.duty - self.profile.duty_decay_per_s * dt)
        self._duty_at = now
        return round(self.duty, 3)

    # -- the step -----------------------------------------------------------
    def step(self, temp: float | None, now: float, gpu_temp: float | None = None) -> Decision:
        p = self.profile
        if temp is None:
            self.level = "hot" if self.level != "critical" else "critical"
            self._below_since = None
            duty = self._duty(interp(p.duty_curve, p.limits.hot_c), now)
            lowest = self._cpu.force(self._cpu.levels[0] if self._cpu.levels else None, now)
            gpu_low = self._gpu.force(self._gpu.levels[0] if self._gpu.levels else None, now)
            return Decision(
                self.level, False, 0.0, duty, lowest, None, None, 0.0, "sensor_lost", gpu_low
            )
        rate = self._rate(temp, now)
        eff = temp + p.lookahead_s * max(rate, 0.0)
        source = "board"
        if gpu_temp is not None and p.gpu_temp_limits is not None:
            g = gpu_on_board_scale(gpu_temp, p.gpu_temp_limits, p.limits)
            g_rate = self._rate(g, now, self._ghist)
            g_eff = g + p.lookahead_s * max(g_rate, 0.0)
            if g_eff > eff:
                temp, eff, rate, source = max(temp, g), g_eff, g_rate, "gpu"
        level = self._level(temp, eff, now)
        duty_target = interp(p.duty_curve, eff)
        if level == "critical":
            duty_target = 1.0
        duty = self._duty(duty_target, now)
        cpus = self._cpu.step(band_cpus(eff, p), now, p.ramp_s)
        gpu_mhz = self._gpu.step(_band(eff, p.gpu_clock_bands), now, p.ramp_s)
        reason = "rising" if rate > 0.5 else "steady" if rate > -0.5 else "falling"
        return Decision(
            level,
            p.concurrency[LEVELS.index(level)] > 0,
            p.concurrency[LEVELS.index(level)],
            duty,
            cpus,
            round(temp, 2),
            round(eff, 2),
            round(rate, 3),
            reason,
            gpu_mhz,
            source,
            gpu_temp,
        )

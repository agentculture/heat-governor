"""The adaptive loop's temperature trend (PR #5 review): a board <-> gpu source
switch starts a new window, and a slow creep inside each interval's noise band
is caught by the longer window over the last intervals. Pure: plain numbers."""

from __future__ import annotations

from pathlib import Path

from heat_governor.adaptive import DEADBAND, AdaptiveLoop, Trend
from heat_governor.config import UnitRule
from heat_governor.policy import Decision

HOST = 20
JITTER = (0.2, -0.2, 0.0)  # +-0.2 C of zone jitter, the Orin's measured scale


def _loop(**kw) -> AdaptiveLoop:
    kw.setdefault("interval_s", 20.0)
    return AdaptiveLoop(**kw)


# -- 6: a source switch starts a new window ----------------------------------------


def test_a_source_switch_discards_the_old_sources_points() -> None:
    tr = Trend()
    for i in range(10):
        tr.add(float(i), 80.0, "board")
    for i in range(10, 12):
        tr.add(float(i), 88.0, "gpu")  # the gpu's mapped scale is ~8 C higher
    assert tr.take() is None  # two points of the new source: too few
    assert tr.switched


def test_after_a_switch_only_the_new_source_is_fitted() -> None:
    tr = Trend()
    for i in range(10):
        tr.add(float(i), 80.0, "board")
    for i in range(10, 20):
        tr.add(float(i), 88.0, "gpu")
    assert tr.take() == 0.0
    tr.add(20.0, 88.0, "gpu")
    tr.take()
    assert not tr.switched


def test_a_switch_window_makes_no_decision_and_teaches_nothing() -> None:
    loop = _loop()
    loop.step("warm", 0.05, 12.0, HOST, 0.0)  # tighten to 9, a sample armed
    assert loop.pending is not None
    s = loop.step("warm", None, 9.0, HOST, 20.0, switched=True)
    assert s.action == "hold"
    assert "source" in s.reason
    assert loop.cap == 9.0  # not the sensor-lost cold-start step
    assert loop.pending is None
    assert loop.model.samples == 0
    assert loop.model.ignored == 1


def test_daemon_does_not_tighten_on_a_source_switch_step(tmp_path: Path) -> None:
    from tests.fakes import FakeSystemctl
    from tests.test_adaptive import _daemon

    sc = FakeSystemctl({"app.slice": None})
    rule = UnitRule("app.slice", mode="adaptive", interval_s=20.0)
    d, _, _, logs = _daemon(tmp_path, sc, rule)

    def tick(now: float, temp: float, source: str) -> None:
        sc.usage["app.slice"] = sc.usage.get("app.slice", 0) + 16_000_000
        dec = Decision("warm", True, 0.5, 0.0, None, temp, temp, 0.0, "warm", source=source)
        d._unit_step(rule, dec, now)

    for i in range(12):
        tick(1000.0 + i, 89.0, "board")
    for i in range(12, 25):
        tick(1000.0 + i, 97.0, "gpu")  # flat on its own scale; evaluated at +20
    assert not [m for m in logs if "tighten" in m]


# -- 11: slow creep through the longer window ---------------------------------------


def _feed(tr: Trend, t0: float, rate: float, n: int = 20) -> None:
    for i in range(n):
        t = t0 + i
        tr.add(t, 61.2 + rate * t + JITTER[i % len(JITTER)], "board")


def _evaluate(rate_c_s: float, windows: int) -> list:
    loop, tr, steps = _loop(), Trend(), []
    for w in range(windows):
        _feed(tr, 20.0 * w, rate_c_s)
        rate = tr.take()
        steps.append((rate, tr.noise, loop.band(tr.noise)))
        s = loop.step("warm", rate, 12.0, HOST, 20.0 * (w + 1), noise=tr.noise, long=tr.long())
        steps[-1] += (s,)
        if s.changed:
            break
    return steps


def test_a_slow_creep_inside_each_intervals_noise_is_caught_by_the_long_window() -> None:
    steps = _evaluate(0.0065, 4)  # ~0.4 C/min, 61.2 -> 61.7 C over 80 s
    assert all(abs(rate - 0.0065) < band for rate, _, band, _ in steps)  # each inside its band
    last = steps[-1][3]
    assert last.action == "tighten"
    assert "long trend +0.006" in last.reason


def test_noise_alone_is_caught_by_neither_window() -> None:
    steps = _evaluate(0.0, 8)
    assert [s.action for *_, s in steps] == ["hold"] * 8


def test_the_long_window_needs_two_intervals_and_resets_on_switch_and_loss() -> None:
    tr = Trend()
    _feed(tr, 0.0, 0.0)
    tr.take()
    assert tr.long() is None
    _feed(tr, 20.0, 0.0)
    tr.take()
    long = tr.long()
    assert long is not None
    assert long[2] == 39.0  # the span in seconds
    tr.add(40.0, 70.0, "gpu")
    assert tr.long() is None
    _feed(tr, 41.0, 0.0)
    tr.take()
    tr.add(61.0, None, "board")
    tr.take()
    assert tr.long() is None


def test_the_long_window_floor_is_the_deadband() -> None:
    loop = _loop()
    s = loop.step("warm", 0.0, 12.0, HOST, 0.0, noise=0.01, long=(DEADBAND * 0.8, 0.0001, 80.0))
    assert s.action == "hold"

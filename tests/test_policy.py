"""The pure controller: levels, hysteresis, lookahead, duty, CPU ramp, sensor loss."""

from __future__ import annotations

from dataclasses import replace

from heat_governor.platforms import SPARK, Limits, Profile
from heat_governor.policy import Controller, band_cpus, interp, level_for

P = Profile(
    name="t",
    zones=(),
    limits=Limits(warm_c=80.0, hot_c=85.0, crit_c=90.0, hysteresis_c=2.0, dwell_s=5.0),
    duty_curve=((80.0, 0.0), (85.0, 0.4), (90.0, 1.0)),
    cpu_bands=((80.0, 12), (85.0, 8), (88.0, 4)),
    lookahead_s=0.0,
    rate_window_s=3.0,
    ramp_s=60.0,
    duty_decay_per_s=0.1,
)


def test_interp_clamps_and_interpolates() -> None:
    c = ((80.0, 0.0), (90.0, 1.0))
    assert interp(c, 70) == 0.0
    assert interp(c, 85) == 0.5
    assert interp(c, 99) == 1.0


def test_level_bands() -> None:
    assert [level_for(t, P) for t in (79, 80, 85, 90)] == ["cool", "warm", "hot", "critical"]


def test_cool_admits_everything() -> None:
    d = Controller(P).step(70.0, 0.0)
    assert (d.level, d.admit, d.concurrency_factor, d.duty, d.cpus) == (
        "cool",
        True,
        1.0,
        0.0,
        None,
    )


def test_warm_halves_concurrency_hot_holds() -> None:
    c = Controller(P)
    assert c.step(82.0, 0).concurrency_factor == 0.5
    d = c.step(86.0, 1)
    assert d.level == "hot"
    assert not d.admit
    assert d.concurrency_factor == 0.0


def test_level_falls_only_after_hysteresis_and_dwell() -> None:
    c = Controller(P)
    c.step(86.0, 0)  # hot
    assert c.step(84.0, 1).level == "hot"  # below 85 but not 2 C below
    assert c.step(82.9, 2).level == "hot"  # 2 C below: dwell starts
    assert c.step(82.9, 6).level == "hot"  # 4 s
    assert c.step(82.9, 7.1).level == "warm"  # 5 s of dwell
    assert c.step(83.0, 8).level == "warm"


def test_a_spike_back_up_resets_the_dwell() -> None:
    c = Controller(P)
    c.step(86.0, 0)
    c.step(82.0, 1)
    c.step(84.0, 3)  # back within the hysteresis band
    assert c.step(82.0, 5).level == "hot"
    assert c.step(82.0, 10.1).level == "warm"


def test_lookahead_raises_level_on_a_fast_rise() -> None:
    p = replace(P, lookahead_s=1.5)
    c = Controller(p)
    c.step(80.0, 0)
    d = c.step(83.0, 1)  # 3 C/s -> effective 87.5
    assert d.effective_c == 87.5
    assert d.level == "hot"
    assert not d.admit
    assert d.temp_c == 83.0


def test_falling_temperature_gets_no_lookahead_bonus() -> None:
    p = replace(P, lookahead_s=1.5)
    c = Controller(p)
    c.step(84.0, 0)
    d = c.step(82.0, 1)
    assert d.effective_c == 82.0


def test_duty_rises_at_once_and_decays_slowly() -> None:
    c = Controller(P)
    assert c.step(85.0, 0).duty == 0.4
    assert c.step(80.0, 1).duty == 0.3  # 0.1 per second
    assert c.step(80.0, 2).duty == 0.2
    assert c.step(87.5, 3).duty == 0.7


def test_critical_forces_full_pause() -> None:
    d = Controller(P).step(90.0, 0)
    assert d.level == "critical"
    assert d.duty == 1.0


def test_band_cpus() -> None:
    assert band_cpus(79, P) is None
    assert band_cpus(81, P) == 12
    assert band_cpus(86, P) == 8
    assert band_cpus(89, P) == 4


def test_cpu_drop_is_immediate_rise_ramps_one_band_per_interval() -> None:
    c = Controller(P)
    assert c.step(89.0, 0).cpus == 4  # fresh start applies the band directly
    assert c.step(70.0, 1).cpus == 4  # wants no cap; must ramp
    assert c.step(70.0, 59).cpus == 4
    assert c.step(70.0, 61).cpus == 8
    assert c.step(70.0, 100).cpus == 8
    assert c.step(70.0, 122).cpus == 12
    assert c.step(70.0, 183).cpus is None  # top of the ladder: the container's own limit
    assert c.step(89.0, 184).cpus == 4  # straight down


def test_fresh_start_hot_does_not_start_uncapped() -> None:
    assert Controller(P).step(86.0, 0).cpus == 8


def test_sensor_loss_is_hot_never_cool() -> None:
    c = Controller(P)
    c.step(70.0, 0)
    d = c.step(None, 1)
    assert (d.level, d.admit, d.reason) == ("hot", False, "sensor_lost")
    assert d.cpus == 4
    assert d.duty == 0.4


def test_sensor_loss_keeps_critical() -> None:
    c = Controller(P)
    c.step(95.0, 0)
    assert c.step(None, 1).level == "critical"


def test_spark_profile_is_internally_consistent() -> None:
    lim = SPARK.limits
    assert lim.warm_c < lim.hot_c < lim.crit_c < 94.5  # below the measured reset point
    assert interp(SPARK.duty_curve, lim.crit_c) == 1.0

"""memtrigger: floor + slope/horizon, and a replay over the recorded 2026-11-14 waves."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from heat_governor.memtrigger import Fire, Trigger, replay

TOTAL = 45939
IST = timezone(timedelta(hours=2))
CSV = Path(__file__).parent / "data" / "memory-2026-11-14.csv"
DANGER_MIB = 2048


def run(series, **kw):
    trig = Trigger(kw.pop("total", TOTAL), **kw)
    return [f for t, a in series if (f := trig.step(t, a)) is not None]


def test_floor_fires_below_five_percent():
    series = [(i, 2000) for i in range(5)]  # 2000 < 5% of 45939 (2296.95), flat
    fires = run(series)
    assert len(fires) == 1
    assert fires[0].reason == "floor"


def test_floor_does_not_fire_above():
    assert run([(i, 2400) for i in range(20)]) == []


def test_horizon_fires_when_runout_inside_horizon():
    # 8000 MiB falling 1000/s: 8 s left, below the 20% soft line (9187.8)
    series = [(i, 8800 - 1000 * i) for i in range(6)]
    fires = run(series)
    assert fires
    assert fires[0].reason == "horizon"
    assert fires[0].rate_mib_s == pytest.approx(1000.0)
    assert isinstance(fires[0], Fire)


def test_horizon_needs_soft_threshold():
    # fast fall but from far above 20%: still above the soft line while avail/rate < 10 s? no,
    # 30000 at 5000/s is 6 s, but above soft, so no fire until it gets below.
    series = [(i, 40000 - 5000 * i) for i in range(4)]  # 40000..25000, all > soft
    assert run(series) == []


def test_slow_fall_inside_soft_does_not_fire():
    # 8000 MiB falling 10 MiB/s: 800 s of runway
    assert run([(i, 8000 - 10 * i) for i in range(30)]) == []


@pytest.mark.parametrize(
    "series",
    [
        [(i, 6000) for i in range(30)],
        [(i, 6000 + 100 * i) for i in range(30)],
    ],
)
def test_flat_or_rising_never_fires(series):
    assert run(series) == []


def test_single_sample_dip_does_not_fire():
    series = [(i, 30000) for i in range(15)]
    series.append((15, 3000))  # one-sample dip (above floor, inside horizon if trusted)
    series += [(16 + i, 30000) for i in range(15)]
    assert run(series) == []
    series2 = [(i, 30000) for i in range(15)] + [(15, 500)] + [(16 + i, 30000) for i in range(15)]
    assert run(series2) == []  # even a dip below the floor, for one sample only


def test_fires_once_then_rearms_after_recovery():
    wave = lambda t0: [(t0 + i, 9000 - 1000 * i) for i in range(8)]  # noqa: E731
    calm = [(20 + i, 40000) for i in range(5)]
    fires = run(wave(0) + calm + wave(30))
    assert len(fires) == 2


def test_window_forgets_old_samples():
    # an old fall followed by a long gap and a flat low level must not fire on stale slope
    series = [(i, 30000 - 2000 * i) for i in range(5)] + [(100 + i, 8000) for i in range(10)]
    assert run(series) == []


def test_replay_yields_fires():
    series = [(i, 8800 - 1000 * i) for i in range(6)]
    assert [f.reason for f in replay(series, TOTAL)] == ["horizon"]


# ---- the recorded 2026-11-14 waves -----------------------------------------------------


def load():
    rows = []
    for line in CSV.read_text().splitlines():
        if line.startswith("#") or line.startswith("t,"):
            continue
        t, a = line.split(",")
        rows.append((float(t), float(a)))
    return rows


def local(t):
    return datetime.fromtimestamp(t, IST).strftime("%H:%M:%S")


def test_fixture_holds_only_t_and_avail():
    lines = CSV.read_text().splitlines()
    assert lines[0].startswith("# MemTotal_mib=45939")
    assert lines[1] == "t,mem:avail_mib"
    assert all(len(ln.split(",")) == 2 for ln in lines[1:])
    rows = load()
    assert local(rows[0][0]) >= "19:30:00"
    assert local(rows[-1][0]) < "19:56:00"


def crossings(rows):
    """First sample of each stretch below DANGER_MIB, one per wave (re-armed above 20%)."""
    out, armed = [], True
    for t, a in rows:
        if a >= TOTAL * 0.2:
            armed = True
        if armed and a < DANGER_MIB:
            out.append(t)
            armed = False
    return out


WAVE_STARTS = ["19:34", "19:36", "19:45", "19:49", "19:51"]


def test_replay_fires_once_per_wave():
    rows = load()
    fires = list(replay(rows, TOTAL))
    assert len(fires) == 5
    bad = crossings(rows)
    assert len(bad) == 5
    for fire, t_bad, start in zip(fires, bad, WAVE_STARTS):
        assert local(fire.t)[:5] >= start  # inside its own wave
        assert fire.t <= t_bad  # fired before memory hit the danger line


def test_replay_lead_time_before_danger():
    """Measured lead (s) between the fire and avail first dropping below 2048 MiB."""
    rows = load()
    fires = list(replay(rows, TOTAL))
    leads = [t_bad - f.t for f, t_bad in zip(fires, crossings(rows))]
    # Approved deviation d1 (devague, 2026-11-14): with the default horizon 10 s / soft 20% the
    # measured leads are 17.6/7/5/6/4 s with 5423-7097 MiB still available at the fire. The
    # original ">= 15 s" margin was unmeasured; the criterion is now ">= 4 s ahead of 2048 MiB
    # with >= 5 GB (5120 MiB) available at the fire" on every wave.
    assert len(leads) == 5, leads
    assert min(leads) >= 4, leads
    assert min(f.avail_mib for f in fires) >= 5120, [f.avail_mib for f in fires]


def test_wave_shapes_match_before_state():
    """Each wave: 95%-of-plateau to the danger line lasts 36-62 s; peak fall >= 1100 MiB/s."""
    rows = load()
    peak = 0.0
    for t_bad in crossings(rows):
        idx = next(i for i, (t, _) in enumerate(rows) if t == t_bad)
        before = [(t, a) for t, a in rows if t_bad - 120 <= t < t_bad]
        t_top, plateau = max(before, key=lambda r: r[1])
        start = next(t for t, a in before if t > t_top and a < 0.95 * plateau)
        assert 36 <= t_bad - start <= 62, (local(t_bad), t_bad - start)
        for (t0, a0), (t1, a1) in zip(
            rows[max(idx - 60, 0) : idx + 1], rows[max(idx - 59, 1) : idx + 1]
        ):
            if t1 - t0 <= 1.5:
                peak = max(peak, (a0 - a1) / (t1 - t0))
    assert peak >= 1100


# --- explicit re-arm after a fire that did not kill (review finding #2) -----------------------


def _fired(trig, t0=0.0, n=3, avail=2000):
    """Step a flat floor series until it fires; return the next t to use."""
    fires = []
    t = t0
    for _ in range(n):
        f = trig.step(t, avail)
        if f is not None:
            fires.append(f)
        t += 0.25
    assert len(fires) == 1
    return t


def test_armed_property_tracks_fire_and_recovery():
    trig = Trigger(TOTAL)
    assert trig.armed is True
    t = _fired(trig)
    assert trig.armed is False
    trig.step(t, 20_000)  # back above soft
    assert trig.armed is True


def test_rearm_needs_a_fresh_confirm_streak():
    trig = Trigger(TOTAL)
    t = _fired(trig)
    trig.rearm()
    assert trig.armed is True
    assert trig.step(t, 2000) is None  # streak restarts at 1: CONFIRM is 2
    f = trig.step(t + 0.25, 2000)
    assert f is not None
    assert f.reason == "floor"
    assert trig.armed is False


def test_rearm_not_before_holds_the_next_fire():
    trig = Trigger(TOTAL)
    t = _fired(trig)
    trig.rearm(not_before=t + 1.0)
    fires = []
    for i in range(8):  # 2 s of qualifying polls at 0.25 s
        if (f := trig.step(t + 0.25 * i, 2000)) is not None:
            fires.append(f)
    assert len(fires) == 1
    assert fires[0].t >= t + 1.0


def test_recovery_clears_a_rearm_hold():
    trig = Trigger(TOTAL)
    t = _fired(trig)
    trig.rearm(not_before=t + 100.0)
    trig.step(t, 20_000)  # recovered: a new wave owes nothing to the old hold
    assert trig.step(t + 0.25, 2000) is None
    assert trig.step(t + 0.5, 2000) is not None


def test_without_rearm_one_fire_per_wave_still_holds():
    assert len(run([(i * 0.25, 2000) for i in range(40)])) == 1

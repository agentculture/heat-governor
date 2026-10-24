"""The adaptive user-unit loop: the pure controller with plain numbers, the
learned-model store, the cgroup usage read, config validation and the daemon
wiring end to end. No test touches a real unit or a real cgroup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heat_governor.adaptive import (
    ALPHA,
    DEADBAND,
    GAIN_MAX,
    GAIN_MIN,
    AdaptiveLoop,
    Model,
    ModelStore,
    UsageMeter,
)
from heat_governor.config import UnitRule, load, template, validate
from heat_governor.platforms import SPARK

HOST = 20


def _loop(**kw) -> AdaptiveLoop:
    kw.setdefault("min_cpus", 1.0)
    kw.setdefault("engage", "warm")
    kw.setdefault("interval_s", 20.0)
    kw.setdefault("target_rate_c_min", 0.0)
    return AdaptiveLoop(**kw)


def _step(loop, now, *, level="warm", rate=0.03, use=12.0, learn=True):
    return loop.step(level, rate, use, HOST, now, learn=learn)


# -- tighten ---------------------------------------------------------------------


def test_cold_start_tightens_by_a_quarter_of_measured_use() -> None:
    loop = _loop()
    s = _step(loop, 0.0, use=12.0)
    assert s.action == "tighten"
    assert s.cap == 9.0  # 12 - max(1, 25% of 12)
    assert loop.cap == 9.0
    assert "cold start" in s.reason


def test_cold_start_step_is_at_least_one_cpu() -> None:
    s = _step(_loop(), 0.0, use=2.0)
    assert s.cap == 1.0  # 2 - max(1, 0.5)


def test_a_learned_gain_sizes_the_step() -> None:
    loop = _loop(model=Model(gain=0.005, samples=3))
    s = _step(loop, 0.0, rate=0.02, use=12.0)  # error 0.02 / 0.005 = 4 CPUs
    assert s.cap == 8.0
    assert "gain 0.0050 C/s/cpu from 3 samples" in s.reason


def test_the_step_is_clamped_to_half_of_use_and_at_least_one_cpu() -> None:
    big = _step(_loop(model=Model(gain=GAIN_MIN, samples=1)), 0.0, rate=1.0, use=12.0)
    assert big.cap == 6.0  # 50% of 12, not 1.0 / 1e-4 CPUs
    small = _step(_loop(model=Model(gain=GAIN_MAX, samples=1)), 0.0, rate=0.006, use=12.0)
    assert small.cap == 11.0  # 0.001/0.05 = 0.02 CPU -> raised to 1


def test_min_cpus_is_the_floor() -> None:
    loop = _loop(min_cpus=4.0)
    assert _step(loop, 0.0, use=5.0).cap == 4.0
    s = _step(loop, 20.0, use=4.0)
    assert s.action == "hold" and loop.cap == 4.0
    assert "floor" in s.reason


def test_a_new_cap_is_never_above_the_current_one() -> None:
    loop = _loop()
    _step(loop, 0.0, use=12.0)  # cap 9
    s = _step(loop, 20.0, use=16.0)  # 16 - 4 = 12 > 9: stays at most 9
    assert loop.cap == 9.0
    assert s.action == "hold"


def test_the_cap_is_based_on_measured_use_not_on_a_far_higher_cap() -> None:
    loop = _loop()
    loop.cap = 18.0
    s = _step(loop, 0.0, use=6.0)
    assert s.cap == 4.5  # 6 - 1.5, not 18 - something
    assert s.prev == 18.0


def test_target_rate_is_in_degrees_per_minute() -> None:
    loop = _loop(target_rate_c_min=1.2)  # 0.02 C/s
    assert _step(loop, 0.0, rate=0.022).action == "hold"  # error 0.002 < deadband
    assert _step(loop, 20.0, rate=0.03).action == "tighten"


def test_deadband_holds() -> None:
    loop = _loop()
    loop.cap = 8.0
    for i, rate in enumerate((0.004, -0.004, 0.0)):
        s = _step(loop, 40.0 * i, rate=rate)
        assert s.action == "hold"
        assert loop.cap == 8.0


def test_evaluated_at_most_once_per_interval() -> None:
    loop = _loop()
    assert loop.due(0.0)
    _step(loop, 0.0)
    assert not loop.due(10.0)
    s = _step(loop, 10.0)
    assert s.action == "wait" and loop.cap == 9.0
    assert loop.due(20.0)


def test_unknown_use_holds_and_measures() -> None:
    loop = _loop()
    s = loop.step("hot", 0.05, None, HOST, 0.0)
    assert s.action == "hold" and loop.cap is None
    assert "measuring" in s.reason


# -- release ---------------------------------------------------------------------


def test_cooling_faster_than_needed_at_the_engage_level_releases_one_step() -> None:
    loop = _loop()
    _step(loop, 0.0, use=12.0)  # cap 9 at t=0
    assert _step(loop, 20.0, rate=-0.02, use=9.0).action == "hold"  # < 2 x interval
    s = _step(loop, 40.0, rate=-0.02, use=9.0)
    assert s.action == "release" and s.cap == 11.0  # + max(1, 10% of 20)


def test_no_release_while_above_the_engage_level() -> None:
    loop = _loop()
    _step(loop, 0.0, use=12.0)
    s = _step(loop, 40.0, level="hot", rate=-0.02, use=9.0)
    assert s.action == "hold" and loop.cap == 9.0


def test_below_engage_releases_one_step_per_two_intervals_then_removes() -> None:
    loop = _loop()
    _step(loop, 0.0, use=14.0)  # cap 10.5
    caps = []
    t = 0.0
    while loop.cap is not None and t < 1000:
        t += 20.0
        s = _step(loop, t, level="cool", rate=-0.01, use=10.0)
        if s.action in ("release", "remove"):
            caps.append(s.cap)
    assert caps == [12.5, 14.5, 16.5, 18.5, None]  # removed once it reaches 20
    assert t == 200.0  # one step per 40 s


def test_below_engage_without_a_cap_does_nothing() -> None:
    s = _step(_loop(), 0.0, level="cool")
    assert s.action == "hold" and s.cap is None


def test_engage_level_hot_leaves_warm_alone() -> None:
    loop = _loop(engage="hot")
    assert _step(loop, 0.0, level="warm", rate=0.05).action == "hold"
    assert loop.cap is None
    assert _step(loop, 20.0, level="hot", rate=0.05).action == "tighten"


# -- sensor loss -----------------------------------------------------------------


def test_sensor_loss_tightens_by_the_cold_start_step_and_never_releases() -> None:
    loop = _loop(model=Model(gain=0.01, samples=4))
    s = loop.step("hot", None, 12.0, HOST, 0.0)
    assert s.action == "tighten" and s.cap == 9.0
    assert "sensor lost" in s.reason
    assert loop.pending is None  # no rate to learn from
    hi = _loop(engage="critical")
    hi.cap = 6.0
    s = hi.step("hot", None, 6.0, HOST, 100.0)  # not engaged, but blind: hold
    assert s.action == "hold" and hi.cap == 6.0


# -- learning --------------------------------------------------------------------


def test_a_tighten_is_followed_by_one_sample() -> None:
    loop = _loop()
    _step(loop, 0.0, rate=0.03, use=12.0)
    assert loop.pending == (0.0, 12.0, 0.03, 0.0)
    s = _step(loop, 20.0, rate=0.01, use=8.0)  # 0.02 C/s for 4 CPUs
    assert s.learned
    assert s.lesson == (
        "learned gain 0.0050 C/s/cpu from 1 samples (rate fell 0.020 C/s for 4 cpus)"
    )
    assert loop.model.gain == pytest.approx(0.005)
    assert loop.model.samples == 1
    assert loop.model.updated == 20.0
    assert loop.pending is None or loop.pending[0] == 20.0  # only a new tighten re-arms


def test_a_sample_with_too_little_use_change_is_ignored() -> None:
    loop = _loop()
    _step(loop, 0.0, rate=0.03, use=12.0)
    s = _step(loop, 20.0, rate=0.01, use=11.8)
    assert not s.learned
    assert s.lesson.startswith("sample ignored: use fell 0.2 cpus")
    assert (loop.model.samples, loop.model.ignored) == (0, 1)


def test_a_sample_where_the_rate_did_not_fall_is_ignored() -> None:
    loop = _loop()
    _step(loop, 0.0, rate=0.03, use=12.0)
    _step(loop, 20.0, rate=0.04, use=8.0)
    assert (loop.model.samples, loop.model.ignored) == (0, 1)
    assert loop.model.gain is None


def test_no_learning_in_dry_run() -> None:
    loop = _loop()
    _step(loop, 0.0, learn=False)
    assert loop.pending is None
    _step(loop, 20.0, rate=0.0, use=8.0, learn=False)
    assert loop.model == Model()


def test_gain_is_an_ewma_and_clamped() -> None:
    m = Model()
    m.add(0.01, now=1.0)
    assert m.gain == pytest.approx(0.01)
    m.add(0.02, now=2.0)
    assert m.gain == pytest.approx(ALPHA * 0.02 + (1 - ALPHA) * 0.01)
    assert m.samples == 2
    hi = Model()
    hi.add(5.0, now=0.0)
    assert hi.gain == GAIN_MAX
    lo = Model()
    lo.add(1e-9, now=0.0)
    assert lo.gain == GAIN_MIN


def test_describe_formats_the_log_line() -> None:
    loop = _loop(model=Model(gain=0.004, samples=5))
    s = _step(loop, 0.0, rate=0.03, use=12.3)
    assert s.describe("app.slice") == (
        "adaptive app.slice: tighten 12.3 -> 6.15 cpus (use 12.3, rate +0.030 C/s, "
        "target +0.000 C/s, noise band 0.005, gain 0.0040 C/s/cpu from 5 samples)"
    )
    assert s.describe("app.slice", dry=True).startswith("adaptive app.slice: dry-run would tighten")


# -- persistence -----------------------------------------------------------------


def test_store_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "adaptive.json"
    store = ModelStore(path, [].append)
    m = store.model("spark", "app.slice")
    m.add(0.004, now=1761300000.0)
    store.save()
    again = ModelStore(path, [].append)
    got = again.model("spark", "app.slice")
    assert (got.gain, got.samples, got.ignored, got.updated) == (0.004, 1, 0, 1761300000.0)
    assert again.model("thor", "app.slice") == Model()  # per platform
    assert json.loads(path.read_text())["platforms"]["spark"]["app.slice"]["samples"] == 1
    assert not path.with_suffix(".tmp").exists()


def test_store_corrupt_file_starts_fresh_with_a_log_line(tmp_path: Path) -> None:
    path = tmp_path / "adaptive.json"
    path.write_text("{not json")
    logs: list[str] = []
    store = ModelStore(path, logs.append)
    assert store.model("spark", "app.slice") == Model()
    assert any("adaptive.json" in m and "starting fresh" in m for m in logs)
    path.write_text('{"platforms": {"spark": {"app.slice": {"gain": "x"}}}}')
    logs.clear()
    assert ModelStore(path, logs.append).model("spark", "app.slice") == Model()
    assert logs


def test_store_missing_file_is_silent(tmp_path: Path) -> None:
    logs: list[str] = []
    ModelStore(tmp_path / "none.json", logs.append)
    assert logs == []


# -- measuring use -----------------------------------------------------------------


def test_usage_meter_is_cpus_over_the_interval() -> None:
    usage = {"u": 1_000_000}
    meter = UsageMeter(lambda unit: usage[unit])
    assert meter.sample("u", 100.0) is None  # baseline
    usage["u"] += 30_000_000  # 30 CPU-seconds in 10 s
    assert meter.sample("u", 110.0) == pytest.approx(3.0)
    usage["u"] = 5  # counter reset (unit restarted): no reading, new baseline
    assert meter.sample("u", 120.0) is None
    meter2 = UsageMeter(lambda unit: None)
    assert meter2.sample("u", 0.0) is None


def test_systemctl_reads_cgroup_cpu_stat(tmp_path: Path) -> None:
    from heat_governor.units import Systemctl

    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"
    root = tmp_path / "cgroup"
    (root / cg.lstrip("/")).mkdir(parents=True)
    stat = root / cg.lstrip("/") / "cpu.stat"
    stat.write_text("usage_usec 123456\nuser_usec 100000\nsystem_usec 23456\n")
    calls: list[list[str]] = []

    def run(argv):
        calls.append(argv)
        return 0, f"ControlGroup={cg}\n", ""

    sc = Systemctl(run=run, cgroup_root=root)
    assert sc.cpu_usage_usec("app.slice") == 123456
    stat.write_text("usage_usec 200000\n")
    assert sc.cpu_usage_usec("app.slice") == 200000
    assert len(calls) == 1  # the path is cached
    assert calls[0][:3] == ["systemctl", "--user", "show"]
    assert calls[0][-2:] == ["--", "app.slice"] and "ControlGroup" in calls[0]
    stat.unlink()
    assert sc.cpu_usage_usec("app.slice") is None
    assert len(calls) == 2  # a vanished cgroup looks the path up again


def test_systemctl_usage_of_a_unit_without_a_cgroup(tmp_path: Path) -> None:
    from heat_governor.units import Systemctl

    sc = Systemctl(run=lambda argv: (0, "ControlGroup=\n", ""), cgroup_root=tmp_path)
    assert sc.cpu_usage_usec("x.scope") is None
    sc = Systemctl(run=lambda argv: (1, "", "Failed to connect to bus"), cgroup_root=tmp_path)
    assert sc.cpu_usage_usec("x.scope") is None
    sc = Systemctl(run=lambda argv: (0, "ControlGroup=/../../etc\n", ""), cgroup_root=tmp_path)
    assert sc.cpu_usage_usec("x.scope") is None  # never read outside the cgroup root


# -- config ----------------------------------------------------------------------


def test_adaptive_config_is_validated_strictly() -> None:
    ok = {
        "units": [
            {
                "name": "app.slice",
                "mode": "adaptive",
                "min_cpus": 2,
                "engage": "hot",
                "interval_s": 30,
                "target_rate_c_min": -0.5,
                "dry_run": True,
            }
        ]
    }
    assert validate(ok) == []
    assert validate({"units": [{"name": "app.slice", "mode": "adaptive"}]}) == []
    assert validate({"units": [{"name": "app.slice", "mode": "levels", "hot": 4}]}) == []
    bad = [
        ({"mode": "pid"}, "mode"),
        ({"mode": "adaptive", "hot": 4}, "not allowed with mode = adaptive"),
        ({"mode": "adaptive", "min_cpus": 0}, "min_cpus"),
        ({"mode": "adaptive", "min_cpus": True}, "min_cpus"),
        ({"mode": "adaptive", "engage": "cool"}, "engage"),
        ({"mode": "adaptive", "interval_s": 4}, "interval_s"),
        ({"mode": "adaptive", "target_rate_c_min": "0"}, "target_rate_c_min"),
        ({"min_cpus": 2}, "only with mode = adaptive"),
    ]
    for extra, needle in bad:
        problems = validate({"units": [{"name": "app.slice", **extra}]})
        assert any(needle in p for p in problems), (extra, problems)


def test_adaptive_config_loads_rules(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(
        '[[units]]\nname = "app.slice"\nmode = "adaptive"\nmin_cpus = 2\n'
        'engage = "hot"\ninterval_s = 30\ntarget_rate_c_min = 0.5\n'
        '\n[[units]]\nname = "x.scope"\nhot = 4\n'
    )
    a, b = load(p).units
    assert (a.mode, a.min_cpus, a.engage, a.interval_s, a.target_rate_c_min) == (
        "adaptive",
        2.0,
        "hot",
        30.0,
        0.5,
    )
    assert a.bands == ()
    assert (b.mode, b.min_cpus, b.engage, b.interval_s) == ("levels", 1.0, "warm", 20.0)


def test_template_documents_adaptive_mode() -> None:
    import tomllib

    text = template(SPARK)
    assert 'mode = "adaptive"' in text
    assert validate(tomllib.loads(text)) == []


# -- the daemon ----------------------------------------------------------------------


def _daemon(tmp_path: Path, sc, rule: UnitRule, *, apply=True):
    from heat_governor.daemon import Daemon, Options
    from heat_governor.sense import Sensor
    from tests.fakes import SPARK_ZONES, make_sys

    sysr = make_sys(tmp_path, SPARK_ZONES)
    logs: list[str] = []
    opts = Options(
        apply=apply,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        log=logs.append,
        units=(rule,),
        record=False,
    )
    t = [1000.0]
    d = Daemon(
        SPARK,
        opts,
        sensor=Sensor(SPARK.zones, sys_root=sysr),
        systemctl=sc,
        clock=lambda: t[0],
        host_cpus=HOST,
    )
    return d, sysr, t, logs


def _run(d, t, sysr, sc, *, temp_md, use_cpus, seconds, unit="app.slice"):
    """Tick once per second for ``seconds`` at ``temp_md`` millidegrees, with the
    unit burning ``min(use_cpus, its quota)`` CPUs."""
    from tests.fakes import set_temp

    for _ in range(int(seconds)):
        set_temp(sysr, 0, int(temp_md() if callable(temp_md) else temp_md))
        quota = sc.units.get(unit)
        burn = use_cpus if quota is None else min(use_cpus, quota / 1_000_000)
        sc.usage[unit] = sc.usage.get(unit, 0) + round(burn * 1_000_000)
        t[0] += 1.0
        d.tick()


def test_daemon_adaptive_engage_tighten_learn_cool_release_restore(tmp_path: Path) -> None:
    from heat_governor.state import read_state
    from tests.fakes import FakeSystemctl

    sc = FakeSystemctl({"app.slice": None})
    rule = UnitRule("app.slice", mode="adaptive", interval_s=5.0)
    d, sysr, t, logs = _daemon(tmp_path, sc, rule)
    d.start()
    _run(d, t, sysr, sc, temp_md=80000, use_cpus=16, seconds=6)  # cool: baseline only
    assert [c for c in sc.calls if c[0] == "set"] == []
    # warm and rising 0.05 C/s: the loop tightens from measured use
    temp = [88500.0]

    def rising():
        temp[0] += 50.0
        return temp[0]

    _run(d, t, sysr, sc, temp_md=rising, use_cpus=16, seconds=6)
    sets = [c for c in sc.calls if c[0] == "set"]
    assert sets and sets[0] == ("set", "app.slice", "1200")  # 16 - 25% of 16
    tighten = [m for m in logs if "adaptive app.slice: tighten" in m]
    assert tighten and "cold start" in tighten[0]
    # the rise slows after the cut: one sample learned and persisted
    _run(d, t, sysr, sc, temp_md=lambda: temp[0], use_cpus=16, seconds=5)
    model = d.adaptive_store.model("spark", "app.slice")
    assert model.samples + model.ignored == 1
    assert any(
        m.endswith(
            "adaptive app.slice: learned gain 0.0125 C/s/cpu from 1 samples "
            "(rate fell 0.050 C/s for 4 cpus)"
        )
        for m in logs
    )
    saved = json.loads((tmp_path / "data" / "adaptive.json").read_text())
    assert "app.slice" in saved["platforms"]["spark"]
    st = read_state(tmp_path / "state.json")
    (u,) = st["units"]
    assert u["mode"] == "adaptive" and u["cpus"] is not None
    assert u["use_cpus"] == pytest.approx(12.0, abs=0.5)
    assert u["samples"] == model.samples and "gain" in u
    # cool: released one step per 2 x interval, then removed and restored
    _run(d, t, sysr, sc, temp_md=70000, use_cpus=16, seconds=400)
    assert sc.units["app.slice"] is None
    assert any("adaptive app.slice: release" in m for m in logs)
    assert any("-> none" in m for m in logs)
    from heat_governor.actuators import Ledger

    assert Ledger(tmp_path / "data" / "ledger.json").unit_quota == {}
    d.shutdown()


def test_daemon_adaptive_dry_run_changes_nothing_and_learns_nothing(tmp_path: Path) -> None:
    from tests.fakes import FakeSystemctl

    sc = FakeSystemctl({"app.slice": None})
    rule = UnitRule("app.slice", mode="adaptive", interval_s=5.0, dry_run=True)
    d, sysr, t, logs = _daemon(tmp_path, sc, rule)
    d.start()
    temp = [88500.0]

    def rising():
        temp[0] += 50.0
        return temp[0]

    _run(d, t, sysr, sc, temp_md=rising, use_cpus=16, seconds=30)
    assert [c for c in sc.calls if c[0] == "set"] == []
    assert any("adaptive app.slice: dry-run would tighten" in m for m in logs)
    assert d.adaptive_store.model("spark", "app.slice") == Model()
    assert not (tmp_path / "data" / "adaptive.json").exists()
    d.shutdown()


def test_daemon_adaptive_model_survives_a_restart(tmp_path: Path) -> None:
    from tests.fakes import FakeSystemctl

    path = tmp_path / "data" / "adaptive.json"
    store = ModelStore(path, [].append)
    store.model("spark", "app.slice").add(0.004, now=1.0)
    store.save()
    sc = FakeSystemctl({"app.slice": None})
    d, _, _, _ = _daemon(tmp_path, sc, UnitRule("app.slice", mode="adaptive"))
    assert d.adaptive["app.slice"].model.gain == 0.004


def test_levels_units_report_mode_and_use(tmp_path: Path) -> None:
    from heat_governor.state import read_state
    from tests.fakes import FakeSystemctl

    sc = FakeSystemctl({"app.slice": None})
    d, sysr, t, _ = _daemon(tmp_path, sc, UnitRule("app.slice"))
    _run(d, t, sysr, sc, temp_md=80000, use_cpus=3, seconds=3)
    (u,) = read_state(tmp_path / "state.json")["units"]
    assert u["mode"] == "levels"
    assert u["use_cpus"] == pytest.approx(3.0)
    assert "gain" not in u


def test_a_reload_keeps_the_loops_cap_and_model_and_drops_removed_loops(tmp_path: Path) -> None:
    from tests.fakes import FakeSystemctl

    sc = FakeSystemctl({"app.slice": None})
    d, _, _, _ = _daemon(tmp_path, sc, UnitRule("app.slice", mode="adaptive"))
    loop = d.adaptive["app.slice"]
    loop.cap = 7.0
    loop.model.add(0.01, now=1.0)
    d._set_units((UnitRule("app.slice", mode="adaptive", interval_s=40.0, min_cpus=2.0),))
    assert d.adaptive["app.slice"] is loop
    assert (loop.cap, loop.interval_s, loop.min_cpus, loop.model.samples) == (7.0, 40.0, 2.0, 1)
    d._set_units((UnitRule("app.slice"),))  # back to levels
    assert d.adaptive == {}


def test_run_once_reports_an_adaptive_unit(tmp_path: Path, monkeypatch, capsys) -> None:
    from heat_governor.cli import main
    from tests.fakes import FakeSystemctl

    sc = FakeSystemctl({"app.slice": None})
    monkeypatch.setattr("heat_governor.daemon.Systemctl", lambda: sc)
    cfg = tmp_path / "c.toml"
    cfg.write_text('platform = "spark"\n[[units]]\nname = "app.slice"\nmode = "adaptive"\n')
    rc = main(
        [
            "run",
            "--once",
            "--no-docker",
            "--config",
            str(cfg),
            "--data-dir",
            str(tmp_path / "d"),
            "--state",
            str(tmp_path / "s.json"),
            "--json",
        ]
    )
    assert rc in (0, None)
    doc = json.loads(capsys.readouterr().out)
    assert doc["units"] == [{"name": "app.slice", "mode": "adaptive", "cpus": None}]
    assert [c for c in sc.calls if c[0] == "set"] == []


# -- the trend the loop acts on ----------------------------------------------


def test_trend_is_the_least_squares_slope_over_the_window() -> None:
    from heat_governor.adaptive import Trend

    tr = Trend()
    for i in range(21):  # +0.05 C/s with +-2 C of zone jitter on alternate seconds
        tr.add(float(i), 80.0 + 0.05 * i + (2.0 if i % 2 else -2.0))
    assert abs(tr.take() - 0.05) < 0.02
    assert tr.take() is None  # a new window starts empty


def test_trend_is_unknown_when_blind_or_too_short() -> None:
    from heat_governor.adaptive import Trend

    tr = Trend()
    tr.add(0.0, 80.0)
    tr.add(1.0, 81.0)
    assert tr.take() is None
    for i in range(5):
        tr.add(float(i), None if i == 2 else 80.0)
    assert tr.take() is None


# -- conditions from the data: noise band, significance, idle units ------------


def test_the_deadband_is_the_measured_trend_noise() -> None:
    loop = _loop()
    assert loop.band(None) == DEADBAND
    assert loop.band(0.0001) == DEADBAND
    assert loop.band(0.05) == 0.1
    s = loop.step("warm", 0.06, 12.0, 20, 0.0, noise=0.05)  # rising, but inside its noise
    assert s.action == "hold" and "noise band 0.100" in s.reason


def test_a_change_inside_the_noise_teaches_nothing() -> None:
    loop = _loop()
    loop.step("warm", 0.20, 12.0, 20, 0.0, noise=0.05)  # tighten, armed with noise 0.05
    s = loop.step("warm", 0.10, 9.0, 20, 20.0, noise=0.05)  # fell 0.10 < 2 x 0.071
    assert not s.learned and loop.model.samples == 0 and loop.model.ignored == 1
    assert "above the noise" in s.lesson


def test_an_idle_unit_is_never_cut_and_its_cap_is_released() -> None:
    loop = _loop()
    s = loop.step("warm", 0.2, 0.1, 20, 0.0)
    assert s.action == "hold" and loop.cap is None and "not the heat source" in s.reason
    loop.cap = 4.0
    s = loop.step("warm", 0.2, 0.0, 20, 20.0)
    assert s.action == "release" and loop.cap == 6.0


def test_a_cap_that_is_not_binding_is_released_when_not_rising() -> None:
    loop = _loop()
    loop.cap = 10.0
    s = loop.step("warm", 0.0, 3.0, 20, 0.0)
    assert s.action == "release" and "not binding" in s.reason

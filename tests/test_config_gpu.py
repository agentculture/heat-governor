"""Configuration (validation, live reload, template), concurrency, and the GPU
clock ceiling (ramp, ledger, recovery, dry run)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from heat_governor import config as cfgmod
from heat_governor import platforms
from heat_governor.actuators import CpuCapper, Ledger
from heat_governor.cli import main
from heat_governor.daemon import Daemon, Options
from heat_governor.platforms import SPARK
from heat_governor.policy import Controller
from heat_governor.sense import Sensor
from heat_governor.state import read_state
from tests.fakes import SPARK_ZONES, FakeDocker, container, make_sys, set_temp


class FakeGpu:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.reason = "ok" if available else "helper not installed"
        self.ceiling_mhz = None
        self.calls: list[tuple] = []

    def probe(self) -> bool:
        return self.available

    def lock(self, mhz: int):
        self.calls.append(("lock", mhz))
        self.ceiling_mhz = mhz
        return True, str(mhz)

    def reset(self):
        self.calls.append(("reset",))
        self.ceiling_mhz = None
        return True, "reset"


# -- validation ------------------------------------------------------------------


def test_valid_config_round_trips_through_the_template() -> None:
    import tomllib

    data = tomllib.loads(cfgmod.template(SPARK))
    assert cfgmod.validate(data) == []
    again = platforms.resolve(config=data)
    for attr in (
        "limits",
        "duty_curve",
        "cpu_bands",
        "gpu_clock_bands",
        "concurrency",
        "gpu_temp_limits",
    ):
        assert getattr(again, attr) == getattr(SPARK, attr)


@pytest.mark.parametrize(
    "doc,needle",
    [
        ({"profile": {"hot_C": 90}}, "unknown key profile.'hot_C'"),
        ({"bogus": 1}, "unknown top-level key"),
        ({"profile": {"duty_curve": [[90, 0.5], [88, 0.1]]}}, "must ascend"),
        ({"profile": {"duty_curve": [[90, 1.5]]}}, "between 0 and 1"),
        ({"profile": {"cpu_bands": [[90, 0.5]]}}, "whole number"),
        ({"profile": {"gpu_clock_bands": [[90, 50]]}}, "outside 100-5000"),
        ({"profile": {"concurrency": {"tepid": 1}}}, "unknown level"),
        ({"daemon": {"poll_s": 60}}, "between 0.1 and 10"),
        ({"daemon": {"apply": "yes"}}, "wrong type"),
        ({"containers": [{"name": "x", "role": "boss"}]}, "role must be batch or serving"),
        ({"containers": [{"name": "x", "role": "batch"}, {"name": "x", "role": "batch"}]}, "twice"),
    ],
)
def test_validation_catches(doc: dict, needle: str) -> None:
    problems = cfgmod.validate(doc)
    assert any(needle in p for p in problems), problems


def test_limits_out_of_order_rejected_at_resolve() -> None:
    with pytest.raises(ValueError):
        platforms.resolve("spark", config={"profile": {"hot_c": 99.0}})


def test_concurrency_override_changes_admission() -> None:
    p = platforms.resolve("spark", config={"profile": {"concurrency": {"hot": 0.25}}})
    c = Controller(p)
    d = c.step(92.0, 0)
    assert d.level == "hot" and d.admit and d.concurrency_factor == 0.25


def test_default_path_env(tmp_path: Path) -> None:
    assert cfgmod.default_config_path({"HEAT_GOVERNOR_CONFIG": "/x.toml"}) == Path("/x.toml")
    assert cfgmod.default_config_path({"XDG_CONFIG_HOME": "/c"}) == Path(
        "/c/heat-governor/config.toml"
    )


# -- live reload -----------------------------------------------------------------


def _daemon(tmp_path: Path, cfg_text: str, *, apply=True, gpu=None, docker=None, **kw):
    sysr = make_sys(tmp_path, SPARK_ZONES)
    path = tmp_path / "config.toml"
    path.write_text(cfg_text)
    cfg = cfgmod.load(path)
    prof = platforms.resolve("spark", config=cfg.data)
    logs: list[str] = []
    opts = Options(
        apply=apply,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        config=cfg,
        platform="spark",
        reload_every_s=0.0,
        gpu_clock=bool(cfg.daemon.get("gpu_clock", False)),
        containers=tuple(cfg.containers),
        log=logs.append,
        **kw,
    )
    d = Daemon(prof, opts, sensor=Sensor(prof.zones, sys_root=sysr), docker=docker, gpuclock=gpu)
    return d, sysr, path, logs


def _touch(path: Path, text: str) -> None:
    path.write_text(text)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))  # make the change visible to mtime


def test_reload_applies_a_valid_change(tmp_path: Path) -> None:
    d, sysr, path, logs = _daemon(tmp_path, "[profile]\nhot_c = 91.5\n")
    set_temp(sysr, 0, 89000)
    assert d.tick().level == "warm"
    _touch(path, "[profile]\nwarm_c = 85.0\nhot_c = 88.5\ncrit_c = 92.0\n")
    assert d.tick().level == "hot"
    assert d.profile.limits.hot_c == 88.5
    assert any("config reloaded" in m and "hot_c: 91.5 -> 88.5" in m for m in logs)


def test_reload_rejects_an_invalid_change_and_keeps_the_last_good(tmp_path: Path) -> None:
    d, sysr, path, logs = _daemon(tmp_path, "[profile]\nhot_c = 91.5\n")
    d.tick()
    _touch(path, "[profile]\nhot_c = 'warm'\n")
    d.tick()
    assert d.profile.limits.hot_c == 91.5
    assert any("rejected" in m for m in logs)
    assert read_state(tmp_path / "state.json")["config_error"]


def test_reload_picks_up_new_container_rules(tmp_path: Path) -> None:
    dk = FakeDocker(container("s1", "lane", None, nano=0))
    d, sysr, path, _ = _daemon(tmp_path, "", docker=dk)
    d.tick()
    assert d.containers == {}
    _touch(path, '[[containers]]\nname = "lane"\nrole = "serving"\ncpu_cap = true\n')
    set_temp(sysr, 0, 91000)
    d.tick()
    assert [c.name for c in d.capped()] == ["lane"]
    assert dk.c["s1"].nano_cpus == 4_000_000_000
    d.shutdown()
    assert dk.c["s1"].nano_cpus == (os.cpu_count() or 1) * 1_000_000_000


# -- GPU clock ceiling -------------------------------------------------------------


def test_gpu_ramp_drops_at_once_and_rises_slowly() -> None:
    from dataclasses import replace

    c = Controller(replace(SPARK, lookahead_s=0.0))  # isolate the ramp from the lookahead
    assert c.step(80.0, 0).gpu_mhz is None
    assert c.step(91.0, 1).gpu_mhz == 2200
    assert c.step(93.5, 2).gpu_mhz == 1400
    assert c.step(80.0, 3).gpu_mhz == 1400
    assert c.step(80.0, 64).gpu_mhz == 1800
    assert c.step(80.0, 125).gpu_mhz == 2200
    assert c.step(80.0, 186).gpu_mhz is None


def test_gpu_sensor_loss_goes_to_the_lowest_ceiling() -> None:
    c = Controller(SPARK)
    c.step(80.0, 0)
    assert c.step(None, 1).gpu_mhz == 1400


def test_daemon_locks_records_first_and_resets_on_stop(tmp_path: Path) -> None:
    gpu = FakeGpu()
    d, sysr, _, logs = _daemon(tmp_path, "[daemon]\ngpu_clock = true\n", gpu=gpu)
    set_temp(sysr, 0, 92000)
    d.tick()
    assert gpu.calls == [("lock", 1800)]
    assert json.loads((tmp_path / "data" / "ledger.json").read_text())["gpu_locked"] is True
    assert read_state(tmp_path / "state.json")["gpu_ceiling_mhz"] == 1800
    d.shutdown()
    assert gpu.calls[-1] == ("reset",)
    assert json.loads((tmp_path / "data" / "ledger.json").read_text())["gpu_locked"] is False


def test_gpu_dry_run_changes_nothing(tmp_path: Path) -> None:
    gpu = FakeGpu()
    d, sysr, _, logs = _daemon(tmp_path, "[daemon]\ngpu_clock = true\n", gpu=gpu, apply=False)
    set_temp(sysr, 0, 92000)
    d.tick()
    assert gpu.calls == []
    assert any("dry-run would set gpu ceiling 1800" in m for m in logs)


def test_gpu_unavailable_is_reported_not_simulated(tmp_path: Path) -> None:
    gpu = FakeGpu(available=False)
    d, sysr, _, _ = _daemon(tmp_path, "[daemon]\ngpu_clock = true\n", gpu=gpu)
    set_temp(sysr, 0, 92000)
    d.tick()
    assert gpu.calls == []
    assert "not installed" in read_state(tmp_path / "state.json")["gpu_clock"]


def test_gpu_off_by_default(tmp_path: Path) -> None:
    gpu = FakeGpu()
    d, sysr, _, _ = _daemon(tmp_path, "", gpu=gpu)
    set_temp(sysr, 0, 93500)
    d.tick()
    assert gpu.calls == [] and read_state(tmp_path / "state.json")["gpu_clock"] == "off"


def test_turning_gpu_clock_off_by_reload_releases_the_ceiling(tmp_path: Path) -> None:
    gpu = FakeGpu()
    d, sysr, path, _ = _daemon(tmp_path, "[daemon]\ngpu_clock = true\n", gpu=gpu)
    set_temp(sysr, 0, 92000)
    d.tick()
    _touch(path, "[daemon]\ngpu_clock = false\n")
    d.tick()
    assert gpu.calls == [("lock", 1800), ("reset",)]


def test_recover_resets_a_ceiling_left_by_a_crash(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.json")
    led.gpu_locked = True
    led.save()
    gpu = FakeGpu()
    Ledger(tmp_path / "l.json").recover(None, [].append, gpu=gpu)
    assert gpu.calls == [("reset",)]
    assert Ledger(tmp_path / "l.json").gpu_locked is False


# -- CPU limit drift ----------------------------------------------------------------


def test_cpu_cap_is_reasserted_after_an_outside_change(tmp_path: Path) -> None:
    dk = FakeDocker(container("s1", "lane", "serving", cap=True, nano=0))
    cap = CpuCapper(dk, Ledger(tmp_path / "l.json"), [].append, apply=True)
    cap.set(dk.c["s1"], 4)
    dk.set_nano_cpus("s1", 16_000_000_000)  # someone restores it behind our back
    cap.set(dk.c["s1"], 4, observed_at=time.time() - 60)  # a stale snapshot: no action
    assert dk.c["s1"].nano_cpus == 16_000_000_000
    cap.set(dk.c["s1"], 4, observed_at=time.time() + 1)  # a fresh one: re-assert
    assert dk.c["s1"].nano_cpus == 4_000_000_000


# -- CLI ---------------------------------------------------------------------------


def _run(capsys, *argv):
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_config_init_check_show(tmp_path: Path, capsys) -> None:
    path = tmp_path / "c.toml"
    rc, out, _ = _run(
        capsys, "config", "init", "--platform", "spark", "--config", str(path), "--write", "--json"
    )
    assert rc == 0 and json.loads(out)["written"] and path.exists()
    rc, _, err = _run(
        capsys, "config", "init", "--platform", "spark", "--config", str(path), "--write"
    )
    assert rc == 1 and "hint:" in err
    rc, out, _ = _run(capsys, "config", "check", str(path), "--json")
    assert rc == 0 and json.loads(out)["valid"]
    path.write_text("[profile]\nhot_C = 1\n")
    rc, out, _ = _run(capsys, "config", "check", str(path), "--json")
    assert rc == 1 and json.loads(out)["valid"] is False
    path.write_text("[profile]\nhot_c = 91.0\n")
    rc, out, _ = _run(
        capsys, "config", "show", "--platform", "spark", "--config", str(path), "--json"
    )
    assert json.loads(out)["profile"]["limits"]["hot_c"] == 91.0
    rc, out, _ = _run(capsys, "config", "overview", "--config", str(path), "--json")
    assert json.loads(out)["valid"] is True


def test_cli_invalid_config_is_a_user_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / "c.toml"
    path.write_text("[daemon]\npoll_s = 0\n")
    rc, _, err = _run(capsys, "sense", "--platform", "spark", "--config", str(path))
    assert rc == 1 and "poll_s" in err and "hint:" in err


def test_cli_gpu_clock_helper_prints_script_and_sudoers(capsys) -> None:
    rc, out, _ = _run(capsys, "gpu-clock", "helper", "--json")
    doc = json.loads(out)
    assert rc == 0 and doc["script"].startswith("#!/bin/bash")
    assert doc["sudoers"].endswith("NOPASSWD: /usr/local/sbin/heat-governor-gpuclock")


# -- GPU temperature as a second input ------------------------------------------

from heat_governor.policy import gpu_on_board_scale  # noqa: E402


def test_gpu_scale_maps_band_edges_and_extends_with_slope_one() -> None:
    g, b = SPARK.gpu_temp_limits, SPARK.limits
    assert gpu_on_board_scale(g.warm_c, g, b) == b.warm_c
    assert gpu_on_board_scale(g.hot_c, g, b) == b.hot_c
    assert gpu_on_board_scale(g.crit_c, g, b) == b.crit_c
    assert gpu_on_board_scale(g.warm_c - 5, g, b) == b.warm_c - 5
    assert gpu_on_board_scale(g.crit_c + 2, g, b) == b.crit_c + 2


def test_a_hot_gpu_governs_when_the_board_is_cool() -> None:
    from dataclasses import replace

    c = Controller(replace(SPARK, lookahead_s=0.0))
    d = c.step(80.0, 0, gpu_temp=88.0)  # GPU above its hot band, board cool
    assert d.source == "gpu" and d.level == "hot" and not d.admit
    assert d.gpu_temp_c == 88.0


def test_the_board_governs_when_it_is_closer_to_its_limit() -> None:
    from dataclasses import replace

    c = Controller(replace(SPARK, lookahead_s=0.0))
    d = c.step(92.0, 0, gpu_temp=75.0)
    assert d.source == "board" and d.level == "hot"


def test_gpu_input_off_when_not_declared() -> None:
    from heat_governor.platforms import THOR

    c = Controller(THOR)
    assert c.step(50.0, 0, gpu_temp=200.0).source == "board"
    p = platforms.resolve("spark", config={"profile": {"gpu_temp": False}})
    assert Controller(p).step(80.0, 0, gpu_temp=95.0).source == "board"


def test_gpu_temp_config_override_and_validation() -> None:
    p = platforms.resolve("spark", config={"profile": {"gpu_temp": {"hot_c": 85.0}}})
    assert p.gpu_temp_limits.hot_c == 85.0 and p.gpu_temp_limits.warm_c == 82.0
    assert any("gpu_temp" in x for x in cfgmod.validate({"profile": {"gpu_temp": {"x": 1}}}))
    with pytest.raises(ValueError):
        platforms.resolve("spark", config={"profile": {"gpu_temp": {"hot_c": 99.0}}})


def test_gpu_switching_source_does_not_fake_a_rise() -> None:
    c = Controller(SPARK)
    c.step(86.0, 0, gpu_temp=70.0)
    c.step(86.0, 1, gpu_temp=70.0)
    d = c.step(86.0, 2, gpu_temp=86.0)  # GPU jumps; its own rate drives its lookahead only
    assert d.source == "gpu"
    assert c.step(86.0, 3, gpu_temp=86.0).rate_c_s < 10


def test_daemon_publishes_the_gpu_reading_and_source(tmp_path: Path) -> None:
    class GpuSensor(Sensor):
        def read(self):
            r = super().read()
            r.gpu = {"temp_c": 89.0, "tlimit_c": 5.0, "limit_c": 94.0}
            return r

    sysr = make_sys(tmp_path, SPARK_ZONES)
    opts = Options(state_path=tmp_path / "s.json", data_dir=tmp_path / "d", log=[].append)
    d = Daemon(SPARK, opts, sensor=GpuSensor(SPARK.zones, sys_root=sysr), docker=None)
    d.tick()
    st = read_state(tmp_path / "s.json")
    assert st["source"] == "gpu" and st["gpu_temp_c"] == 89.0 and st["gpu_limit_c"] == 94.0
    assert st["level"] in ("hot", "critical")

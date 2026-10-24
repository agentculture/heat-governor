"""The operator's GPU ceiling is law (operator rule, 2026-10-24).

"governor hand ceiling is law. request to pass it is logged, so we can tell that
it would have happened". Live incident behind it (Jetson AGX Orin, 2026-10-24):
during a hand clock sweep (`sudo heat-governor-gpuclock lock <MHz>`) the running
daemon, with gpu_clock = true, wrote its own band ceiling at the warm level and
later removed it, silently overriding the operator for the whole sweep.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from heat_governor import config as cfgmod
from heat_governor import operator_ceiling as opc
from heat_governor import recorder
from heat_governor.actuators import CpuCapper, Ledger
from heat_governor.cli import main
from heat_governor.daemon import Daemon, Options
from heat_governor.gpuclock import GpuClock, parse_ceiling
from heat_governor.platforms import SPARK
from heat_governor.sense import Sensor
from heat_governor.state import read_state
from tests.fakes import SPARK_ZONES, FakeDocker, FakeGpuClock, container, make_sys, set_temp

GPU_ON = "[daemon]\ngpu_clock = true\n"
COOL, WARM, HOT, CRIT = 83000, 90500, 92000, 93500  # Spark: warm 88 / hot 91.5 / crit 93
RESET = ("reset",)
GC, DATA, JSON = "gpu-clock", "--data-dir", "--json"
LOCK, HOLD, DATA_DIR = "lock", "hold", "data"
SOURCE, HELD, OP_MHZ = "source", "gpu_held_by_operator", "gpu_operator_mhz"
OP_FILE, LEDGER, MHZ_900 = "operator.json", "l.json", "900"
OP_2000 = "gpu_ceiling_mhz = 2000\n"
KEY = "gpu_ceiling_mhz"
WOULD_8 = "would raise lane to 8 cpus -- its own limit 4 holds"
OBSERVED = "observed an outside"
HELD_WARM_2200 = (
    "gpu: would raise the ceiling to 2200 (policy: level warm) -- operator ceiling 2000 MHz holds"
)
HELD_COOL_2000 = (
    "gpu: would raise the ceiling to none (policy: level cool) -- operator ceiling 2000 MHz holds"
)


def _daemon(tmp_path: Path, cfg_text: str = "", *, gpu=None, apply: bool = True):
    sysr = make_sys(tmp_path, SPARK_ZONES)
    path = tmp_path / "config.toml"
    path.write_text(cfg_text)
    cfg = cfgmod.load(path)
    now = [1000.0]
    logs: list[str] = []
    opts = Options(
        apply=apply,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / DATA_DIR,
        config=cfg,
        platform="spark",
        reload_every_s=0.0,
        gpu_clock=bool(cfg.daemon.get("gpu_clock", False)),
        gpu_ceiling_mhz=cfg.daemon.get(KEY),
        log=logs.append,
    )
    gpu = gpu if gpu is not None else FakeGpuClock()
    d = Daemon(
        SPARK,
        opts,
        sensor=Sensor(SPARK.zones, sys_root=sysr),
        gpuclock=gpu,
        clock=lambda: now[0],
    )
    d.gpu.OBSERVE_S = 0.0
    return SimpleNamespace(d=d, sysr=sysr, path=path, logs=logs, now=now, gpu=gpu, tmp=tmp_path)


def _tick(h, millideg: int | None = None):
    if millideg is not None:
        set_temp(h.sysr, 0, millideg)
    h.now[0] += 100.0  # far apart: no rising-rate lookahead, every ramp step allowed
    return h.d.tick()


def _state(h) -> dict:
    return read_state(h.tmp / "state.json")


def _touch(path: Path, text: str) -> None:
    import os

    path.write_text(text)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


def _hold(h, mhz: int) -> None:
    opc.write_hold(opc.hold_path(h.tmp / DATA_DIR), mhz, HOLD, h.now[0])
    p = opc.hold_path(h.tmp / DATA_DIR)
    st = p.stat()
    import os

    os.utime(p, (st.st_atime, st.st_mtime + 5))  # mtime visibly newer than the daemon's read


def _release(h) -> None:
    opc.clear_hold(opc.hold_path(h.tmp / DATA_DIR))


# -- the law at every level --------------------------------------------------------


@pytest.mark.parametrize(
    "temp, wanted, written, held",
    [
        (COOL, None, 2000, True),  # policy: no ceiling -> the operator's holds
        (WARM, 2200, 2000, True),  # policy above the operator: the operator's holds
        (HOT, 1800, 1800, False),  # below the operator for heat: allowed
        (CRIT, 1400, 1400, False),
    ],
)
def test_the_written_ceiling_is_min_of_operator_and_band(
    tmp_path: Path, temp: int, wanted, written: int, held: bool
) -> None:
    h = _daemon(tmp_path, GPU_ON + OP_2000)
    _tick(h, temp)
    assert h.gpu.calls == [(LOCK, written)]
    st = _state(h)
    assert st[OP_MHZ] == 2000
    assert st["gpu_wanted_mhz"] == wanted
    assert st[HELD] is held


def test_heat_steps_go_below_the_operator_and_come_back_to_it_not_above(tmp_path: Path) -> None:
    h = _daemon(tmp_path, GPU_ON + OP_2000)
    for t in (COOL, HOT, CRIT, HOT, WARM, COOL, COOL):
        _tick(h, t)
    assert RESET not in h.gpu.calls
    assert max(m for _, m in h.gpu.calls) == 2000
    assert h.gpu.calls[-1] == (LOCK, 2000)


def test_would_raise_is_logged_once_per_change_of_the_wanted_value(tmp_path: Path) -> None:
    h = _daemon(tmp_path, GPU_ON + OP_2000)
    for _ in range(3):
        _tick(h, COOL)
    _tick(h, WARM)
    _tick(h, WARM)
    held = [m for m in h.logs if "would raise the ceiling" in m]
    assert len(held) == 2
    assert held[0].endswith(HELD_COOL_2000)
    assert held[1].endswith(HELD_WARM_2200)


def test_the_per_tick_record_carries_the_hold_and_sensors_counts_it(tmp_path: Path) -> None:
    h = _daemon(tmp_path, GPU_ON + OP_2000)
    for t in (COOL, COOL, HOT):
        _tick(h, t)
    h.d.recorder.close()
    recs = [r for r in recorder.iter_records(tmp_path / DATA_DIR / "records") if "level" in r]
    assert [r[HELD] for r in recs] == [True, True, False]
    assert recs[0][OP_MHZ] == 2000
    assert recs[2]["gpu_wanted_mhz"] == 1800
    stats = recorder.sensor_stats(tmp_path / DATA_DIR / "records")
    assert stats["gpu_held_by_operator_ticks"] == 2


# -- never a reset while an operator ceiling is set ----------------------------------


def test_exit_restores_the_operator_ceiling_instead_of_resetting(tmp_path: Path) -> None:
    h = _daemon(tmp_path, GPU_ON + OP_2000)
    _tick(h, CRIT)
    h.d.shutdown()
    assert RESET not in h.gpu.calls
    assert h.gpu.calls[-1] == (LOCK, 2000)


def test_turning_gpu_clock_off_keeps_the_operator_ceiling(tmp_path: Path) -> None:
    h = _daemon(tmp_path, GPU_ON + OP_2000)
    _tick(h, CRIT)
    _touch(h.path, "[daemon]\ngpu_clock = false\ngpu_ceiling_mhz = 2000\n")
    _tick(h, CRIT)
    _tick(h, CRIT)
    assert RESET not in h.gpu.calls
    assert h.gpu.calls[-1] == (LOCK, 2000)


def test_with_gpu_clock_off_the_operator_ceiling_is_still_applied_and_kept(tmp_path: Path) -> None:
    h = _daemon(tmp_path, "[daemon]\ngpu_ceiling_mhz = 900\n")
    _tick(h, CRIT)
    _tick(h, COOL)
    h.d.shutdown()
    assert h.gpu.calls == [(LOCK, 900)]
    assert _state(h)[HELD] is False  # no policy drives the GPU: nothing held


def test_dry_run_only_logs_the_operator_ceiling(tmp_path: Path) -> None:
    h = _daemon(tmp_path, "[daemon]\ngpu_ceiling_mhz = 900\n", apply=False)
    _tick(h, COOL)
    h.d.shutdown()
    assert h.gpu.calls == []
    assert any("dry-run would set gpu ceiling 900" in m for m in h.logs)


def test_recover_restores_the_operator_ceiling_instead_of_resetting(tmp_path: Path) -> None:
    led = Ledger(tmp_path / LEDGER)
    led.gpu_locked = True
    led.save()
    gpu = FakeGpuClock()
    logs: list[str] = []
    Ledger(tmp_path / LEDGER).recover(None, logs.append, gpu=gpu, gpu_operator_mhz=900)
    assert gpu.calls == [(LOCK, 900)]
    assert logs == ["recover: gpu ceiling restored to the operator's 900 MHz"]


def test_daemon_recovery_reads_the_persisted_hold(tmp_path: Path) -> None:
    led = Ledger(tmp_path / DATA_DIR / "ledger.json")
    led.gpu_locked = True
    led.save()
    opc.write_hold(opc.hold_path(tmp_path / DATA_DIR), 900, HOLD, 0.0)
    h = _daemon(tmp_path, GPU_ON)
    h.d.start()
    h.d.shutdown()
    assert RESET not in h.gpu.calls
    assert h.gpu.calls[0] == (LOCK, 900)


# -- clearing the operator ceiling ---------------------------------------------------


def test_removing_the_config_key_restores_the_daemons_own_behaviour(tmp_path: Path) -> None:
    h = _daemon(tmp_path, GPU_ON + OP_2000)
    _tick(h, COOL)
    _touch(h.path, GPU_ON)
    _tick(h, COOL)
    assert h.gpu.calls == [(LOCK, 2000), RESET]
    assert _state(h)[OP_MHZ] is None


def test_release_with_gpu_clock_on_goes_back_to_the_band(tmp_path: Path) -> None:
    h = _daemon(tmp_path, GPU_ON)
    _tick(h, COOL)
    _hold(h, 1000)
    _tick(h, HOT)
    _release(h)
    _tick(h, HOT)
    assert h.gpu.calls == [(LOCK, 1000), (LOCK, 1800)]


def test_release_with_gpu_clock_off_resets_what_was_held_for_the_operator(tmp_path: Path) -> None:
    h = _daemon(tmp_path)
    _hold(h, 900)
    _tick(h, COOL)
    _release(h)
    _tick(h, COOL)
    _tick(h, COOL)
    assert h.gpu.calls == [(LOCK, 900), RESET]


def test_gpu_clock_off_and_no_operator_ceiling_never_touches_the_helper(tmp_path: Path) -> None:
    h = _daemon(tmp_path)
    _tick(h, CRIT)
    h.d.shutdown()
    assert h.gpu.calls == []
    assert h.gpu.reads == 0


# -- the runtime hold: CLI, persistence, live pickup ---------------------------------


def _cli(capsys, *argv):
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_cli_hold_persists_the_documented_shape(tmp_path: Path, capsys) -> None:
    rc, out, _ = _cli(capsys, GC, HOLD, MHZ_900, DATA, str(tmp_path), JSON)
    assert rc == 0
    doc = json.loads((tmp_path / OP_FILE).read_text())
    assert doc[KEY] == 900
    assert doc[SOURCE] == HOLD
    assert set(doc) == {KEY, SOURCE, "at", "at_utc"}
    assert json.loads(out)[KEY] == 900


def test_cli_release_clears_the_hold(tmp_path: Path, capsys) -> None:
    _cli(capsys, GC, HOLD, MHZ_900, DATA, str(tmp_path))
    rc, out, _ = _cli(capsys, GC, "release", DATA, str(tmp_path), JSON)
    assert rc == 0
    assert not (tmp_path / OP_FILE).exists()
    assert json.loads(out)["released"] == {KEY: 900, SOURCE: HOLD}


def test_cli_hold_refuses_a_value_outside_the_helper_range(tmp_path: Path, capsys) -> None:
    rc, _, err = _cli(capsys, GC, HOLD, "50", DATA, str(tmp_path))
    assert rc == 1
    assert "100-5000" in err


def test_cli_status_shows_the_operator_ceiling_and_its_source(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from heat_governor.cli._commands import tuning

    monkeypatch.setattr(tuning, "_clock", FakeGpuClock)  # never the real helper
    cfg = tmp_path / "c.toml"
    cfg.write_text("[daemon]\ngpu_ceiling_mhz = 1500\n")
    _cli(capsys, GC, HOLD, "1200", DATA, str(tmp_path))
    rc, out, _ = _cli(capsys, GC, "status", "--config", str(cfg), DATA, str(tmp_path), JSON)
    doc = json.loads(out)["operator_ceiling"]
    assert rc == 0
    assert doc["mhz"] == 1200
    assert doc[SOURCE] == HOLD
    assert doc["config_mhz"] == 1500
    assert json.loads(out)["hardware_ceiling_mhz"] == 1300


def test_cli_status_says_a_hand_lock_is_invisible_on_a_dgx_spark(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from heat_governor.cli._commands import tuning

    monkeypatch.setattr(tuning, "_clock", lambda: FakeGpuClock(visible=False))
    rc, out, _ = _cli(capsys, GC, "status", "--platform", "spark", DATA, str(tmp_path))
    assert rc == 0
    assert "is invisible here" in out


def test_a_running_daemon_picks_up_a_hold_and_a_release(tmp_path: Path) -> None:
    h = _daemon(tmp_path, GPU_ON)
    _tick(h, COOL)
    assert h.gpu.calls == []
    _hold(h, 900)
    _tick(h, COOL)
    assert h.gpu.calls == [(LOCK, 900)]
    _release(h)
    _tick(h, COOL)
    assert h.gpu.calls == [(LOCK, 900), RESET]


def test_a_hold_survives_a_restart(tmp_path: Path) -> None:
    opc.write_hold(opc.hold_path(tmp_path / DATA_DIR), 900, HOLD, 0.0)
    h = _daemon(tmp_path, GPU_ON)
    _tick(h, COOL)
    assert h.gpu.calls == [(LOCK, 900)]
    assert _state(h)["gpu_operator_source"] == HOLD


def test_the_lower_of_config_and_hold_is_the_law() -> None:
    hold = opc.Hold(1200, HOLD, 0.0)
    assert opc.effective(1500, hold) == (1200, "hold")
    assert opc.effective(1000, hold) == (1000, "config")
    assert opc.effective(None, None) == (None, None)


def test_an_unreadable_hold_file_is_no_hold(tmp_path: Path) -> None:
    p = tmp_path / OP_FILE
    p.write_text("{not json")
    assert opc.read_hold(p) is None


# -- config key ----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-5", "50", "6000", '"900"', "true", "900.5"])
def test_config_gpu_ceiling_is_validated(value: str) -> None:
    import tomllib

    problems = cfgmod.validate(tomllib.loads(f"[daemon]\ngpu_ceiling_mhz = {value}\n"))
    assert any(KEY in p for p in problems)


def test_config_gpu_ceiling_accepts_a_whole_mhz() -> None:
    assert cfgmod.validate({"daemon": {KEY: 900}}) == []


def test_config_gpu_ceiling_is_live_reloaded(tmp_path: Path) -> None:
    h = _daemon(tmp_path, GPU_ON)
    _tick(h, WARM)
    _touch(h.path, GPU_ON + "gpu_ceiling_mhz = 1000\n")
    _tick(h, WARM)
    assert h.gpu.calls == [(LOCK, 2200), (LOCK, 1000)]


# -- an undeclared hand ceiling, where the hardware shows it -------------------------

OBSERVED_714 = "gpu: observed an outside ceiling of 714 MHz -- treating it as the operator's (law)"


def test_a_hand_lock_seen_on_a_jetson_becomes_law(tmp_path: Path) -> None:
    gpu = FakeGpuClock(hw_max=1300)
    h = _daemon(tmp_path, GPU_ON, gpu=gpu)
    _tick(h, COOL)
    gpu.hand_lock(714)  # sudo heat-governor-gpuclock lock 714, behind the daemon's back
    _tick(h, WARM)  # band 2200 wanted: must not be written
    _tick(h, WARM)
    seen = [m for m in h.logs if OBSERVED in m]
    assert len(seen) == 1
    assert seen[0].endswith(OBSERVED_714)
    assert max(m for _, m in gpu.calls) <= 714
    assert opc.read_hold(opc.hold_path(tmp_path / DATA_DIR)).source == opc.OBSERVED


def test_an_observed_ceiling_is_held_until_released(tmp_path: Path) -> None:
    gpu = FakeGpuClock(hw_max=1300)
    h = _daemon(tmp_path, GPU_ON, gpu=gpu)
    _tick(h, COOL)
    gpu.hand_lock(714)
    for t in (WARM, COOL, COOL):
        _tick(h, t)
    assert RESET not in gpu.calls
    h.d.shutdown()
    assert RESET not in gpu.calls
    assert _state(h)["gpu_operator_source"] == opc.OBSERVED


def test_release_clears_an_observed_ceiling(tmp_path: Path, capsys) -> None:
    gpu = FakeGpuClock(hw_max=1300)
    h = _daemon(tmp_path, GPU_ON, gpu=gpu)
    _tick(h, COOL)
    gpu.hand_lock(714)
    _tick(h, COOL)
    _cli(capsys, GC, "release", DATA, str(tmp_path / DATA_DIR))
    _tick(h, COOL)
    _tick(h, COOL)
    assert gpu.calls[-1] == RESET
    assert len([m for m in h.logs if OBSERVED in m]) == 1


def test_the_daemons_own_lock_is_not_mistaken_for_a_hand_lock(tmp_path: Path) -> None:
    gpu = FakeGpuClock(hw_max=1300)
    h = _daemon(tmp_path, GPU_ON, gpu=gpu)
    _tick(h, COOL)
    _tick(h, CRIT)  # the daemon writes 1400; the hardware rounds down and shows 1300
    gpu.hw_max = 3000
    _tick(h, HOT)
    _tick(h, COOL)
    assert not any("observed an outside" in m for m in h.logs)


def test_a_hand_lock_is_invisible_on_a_dgx_spark(tmp_path: Path) -> None:
    """GB10: nvidia-smi exposes no -lgc lock (only current/max clocks), so a raw
    hand lock is not seen and is NOT law; declare it with the config key or
    `gpu-clock hold` instead (documented in docs/contracts/gpu-clock.md)."""
    gpu = FakeGpuClock(visible=False)
    h = _daemon(tmp_path, GPU_ON, gpu=gpu)
    _tick(h, COOL)
    gpu.hand_lock(714)
    _tick(h, WARM)
    assert gpu.calls == [(LOCK, 2200)]
    assert not any("observed" in m for m in h.logs)


def test_the_helper_status_parses_a_jetson_ceiling_and_not_gb10_clocks() -> None:
    assert parse_ceiling("714") == 714
    assert parse_ceiling("2405, 3003") is None
    g = GpuClock(run=lambda argv: (0, "2405, 3003"))
    assert g.read_ceiling() is None
    assert GpuClock(run=lambda argv: (0, "714\n")).read_ceiling() == 714
    assert GpuClock(run=lambda argv: (1, "denied")).read_ceiling() is None


# -- containers: a band never raises a container's own, tighter limit -----------------

LIMIT_4 = 4_000_000_000


def _capper(tmp_path: Path, *, apply: bool = True, nano: int = LIMIT_4):
    dk = FakeDocker(container("s1", "lane", "serving", cap=True, nano=nano))
    logs: list[str] = []
    cap = CpuCapper(dk, Ledger(tmp_path / LEDGER), logs.append, apply=apply)
    return dk, cap, logs


def test_a_band_above_a_containers_own_limit_never_raises_it(tmp_path: Path) -> None:
    dk, cap, logs = _capper(tmp_path)
    cap.set(dk.c["s1"], 8)
    cap.set(dk.c["s1"], 8)
    assert dk.c["s1"].nano_cpus == LIMIT_4
    assert [c for c in dk.calls if c[0] == "cpus"] == []
    assert logs == [WOULD_8]


def test_the_would_raise_log_repeats_only_when_the_band_changes(tmp_path: Path) -> None:
    dk, cap, logs = _capper(tmp_path)
    for cpus in (8, 8, 12, 12):
        cap.set(dk.c["s1"], cpus)
    assert logs == [
        WOULD_8,
        "would raise lane to 12 cpus -- its own limit 4 holds",
    ]


def test_cooling_from_a_tighter_cap_restores_the_own_limit_not_the_band(tmp_path: Path) -> None:
    dk, cap, logs = _capper(tmp_path)
    cap.set(dk.c["s1"], 2)
    cap.set(dk.c["s1"], 8)
    assert dk.c["s1"].nano_cpus == LIMIT_4
    assert logs[-1] == WOULD_8


def test_a_held_own_limit_is_not_reasserted_to_the_band(tmp_path: Path) -> None:
    import time

    dk, cap, _ = _capper(tmp_path)
    cap.set(dk.c["s1"], 8)
    cap.set(dk.c["s1"], 8, observed_at=time.time() + 1)
    assert dk.c["s1"].nano_cpus == LIMIT_4


def test_dry_run_logs_the_would_raise_and_writes_nothing(tmp_path: Path) -> None:
    dk, cap, logs = _capper(tmp_path, apply=False)
    cap.set(dk.c["s1"], 8)
    assert dk.calls == []
    assert logs == [WOULD_8]


def test_an_unlimited_container_is_capped_by_the_band(tmp_path: Path) -> None:
    dk, cap, logs = _capper(tmp_path, nano=0)
    cap.set(dk.c["s1"], 8)
    assert dk.c["s1"].nano_cpus == 8_000_000_000


# -- a hand lock through the CLI is law: `gpu-clock lock --apply` records a hold --------


def _hold_file(tmp_path: Path) -> Path:
    from heat_governor import operator_ceiling as opc

    return opc.hold_path(tmp_path)


def test_cli_lock_apply_records_a_hold(tmp_path: Path, capsys, monkeypatch) -> None:
    from heat_governor.cli._commands import tuning

    clock = FakeGpuClock(visible=False)  # the DGX Spark: the hardware shows no lock
    monkeypatch.setattr(tuning, "_clock", lambda: clock)
    rc, _, _ = _cli(capsys, GC, LOCK, "2200", "--apply", DATA, str(tmp_path))
    doc = json.loads(_hold_file(tmp_path).read_text())
    assert rc == 0
    assert clock.calls == [(LOCK, 2200)]
    assert doc["gpu_ceiling_mhz"] == 2200
    assert doc[SOURCE] == HOLD


def test_cli_lock_dry_run_records_nothing(tmp_path: Path, capsys, monkeypatch) -> None:
    from heat_governor.cli._commands import tuning

    monkeypatch.setattr(tuning, "_clock", lambda: FakeGpuClock(visible=False))
    rc, out, _ = _cli(capsys, GC, LOCK, "2200", DATA, str(tmp_path))
    assert rc == 0
    assert "dry run" in out
    assert not _hold_file(tmp_path).exists()


def test_cli_lock_that_fails_records_nothing(tmp_path: Path, capsys, monkeypatch) -> None:
    from heat_governor.cli._commands import tuning

    clock = FakeGpuClock(visible=False)
    monkeypatch.setattr(clock, "lock", lambda mhz: (False, "denied"))
    monkeypatch.setattr(tuning, "_clock", lambda: clock)
    rc, _, _ = _cli(capsys, GC, LOCK, "2200", "--apply", DATA, str(tmp_path))
    assert rc != 0
    assert not _hold_file(tmp_path).exists()


def test_cli_reset_apply_releases_the_hold(tmp_path: Path, capsys, monkeypatch) -> None:
    from heat_governor.cli._commands import tuning

    clock = FakeGpuClock(visible=False)
    monkeypatch.setattr(tuning, "_clock", lambda: clock)
    _cli(capsys, GC, LOCK, "2200", "--apply", DATA, str(tmp_path))
    rc, _, _ = _cli(capsys, GC, "reset", "--apply", DATA, str(tmp_path))
    assert rc == 0
    assert not _hold_file(tmp_path).exists()


def test_orin_gpu_steps_are_the_measured_devfreq_steps() -> None:
    from heat_governor.platforms import ORIN

    assert ORIN.gpu_clock_bands == ((85.0, 1122), (88.0, 918))

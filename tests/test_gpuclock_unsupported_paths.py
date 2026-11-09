"""Every path to the GPU root helper stays shut on a platform with no supported GPU
clock control (the VENTUNO Q): a live config reload, crash recovery of a persisted
``gpu_locked`` ledger entry, and ``gpu-clock hold``. Spark keeps its behaviour.

The helper is made to look installed (and sudo present), so a path that is NOT
guarded really spawns -- the Spark controls prove the fixture would catch it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from heat_governor import gpuclock, platforms
from heat_governor.actuators import Ledger
from heat_governor.cli import main
from heat_governor.cli._commands import tuning
from heat_governor.config import load
from heat_governor.daemon import Daemon, Options
from heat_governor.sense import Sensor
from tests.fakes import SPARK_ZONES, VENTUNOQ_ZONES, make_sys


@pytest.fixture
def spawned(monkeypatch, tmp_path) -> list:
    """Records every helper spawn; the helper looks installed and sudo present."""
    calls: list = []

    def fake_run(argv, *a, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="2000", stderr="")

    helper = tmp_path / "heat-governor-gpuclock"
    helper.write_text("")
    real_init = gpuclock.GpuClock.__init__

    def init(self, *a, **kw):
        kw.setdefault("helper", str(helper))
        real_init(self, *a, **kw)

    monkeypatch.setattr(gpuclock.GpuClock, "__init__", init)
    monkeypatch.setattr(gpuclock.subprocess, "run", fake_run)
    monkeypatch.setattr(gpuclock.shutil, "which", lambda _n: "/usr/bin/sudo")
    return calls


def _daemon(tmp_path: Path, profile, zones, *, config=None, logs=None, **kw) -> Daemon:
    sysr = make_sys(tmp_path, zones)
    opts = Options(
        apply=True,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        log=(logs if logs is not None else []).append,
        config=config,
        platform=profile.name,
        reload_every_s=0.0,
        record=False,
        listeners=False,
        **kw,
    )
    return Daemon(profile, opts, sensor=Sensor(profile.zones, sys_root=sysr), docker=None)


# -- finding 1: a live reload must not build an unrestricted clock -------------------

RELOADS = ["[daemon]\ngpu_clock = true\n", "[daemon]\ngpu_ceiling_mhz = 1500\n"]


def _reload(d: Daemon, path: Path, text: str) -> None:
    cfg = d.opts.config
    path.write_text(text)
    os.utime(path, (cfg.mtime + 5, cfg.mtime + 5))
    d.maybe_reload(d.clock())


@pytest.mark.parametrize("text", RELOADS)
def test_reload_enabling_gpu_on_ventunoq_never_spawns(tmp_path, spawned, text) -> None:
    path = tmp_path / "c.toml"
    path.write_text("")
    d = _daemon(tmp_path, platforms.VENTUNOQ, VENTUNOQ_ZONES, config=load(path))
    d.start()
    assert d.gpuclock is None  # both off at startup
    _reload(d, path, text)
    d.tick()
    d.tick()
    d.shutdown()
    assert d.gpuclock is not None and d.gpuclock.unsupported
    assert d.gpuclock.available is False
    assert spawned == []


def test_reload_enabling_gpu_on_spark_still_probes(tmp_path, spawned) -> None:
    path = tmp_path / "c.toml"
    path.write_text("")
    d = _daemon(tmp_path, platforms.SPARK, SPARK_ZONES, config=load(path))
    d.start()
    _reload(d, path, RELOADS[0])
    assert d.gpuclock is not None and not d.gpuclock.unsupported
    assert any(argv[-1] == "status" for argv in spawned)
    d.shutdown()


# -- finding 2: an unsupported clock refuses everything that spawns ------------------


def test_unsupported_clock_refuses_lock_reset_status_without_spawning(spawned) -> None:
    g = gpuclock.GpuClock(unsupported="unsupported platform: ventunoq")
    assert g.lock(2000) == (False, "unsupported platform: ventunoq")
    assert g.reset() == (False, "unsupported platform: ventunoq")
    assert g.read_ceiling() is None
    assert g.ceiling_mhz is None
    assert spawned == []


@pytest.mark.parametrize("ceiling", [None, 1500])
@pytest.mark.parametrize("gpu_clock", [False, True])
def test_ventunoq_start_with_persisted_gpu_lock_never_spawns(
    tmp_path, spawned, ceiling, gpu_clock
) -> None:
    led = Ledger(tmp_path / "data" / "ledger.json")
    led.gpu_locked = True
    led.save()
    logs: list[str] = []
    d = _daemon(
        tmp_path,
        platforms.VENTUNOQ,
        VENTUNOQ_ZONES,
        logs=logs,
        gpu_clock=gpu_clock,
        gpu_ceiling_mhz=ceiling,
    )
    d.start()  # must not raise
    d.tick()
    d.shutdown()
    assert spawned == []
    assert any("recover" in m and "unsupported platform" in m for m in logs), logs
    assert Ledger(tmp_path / "data" / "ledger.json").gpu_locked is False


def test_spark_start_with_persisted_gpu_lock_still_resets(tmp_path, spawned) -> None:
    led = Ledger(tmp_path / "data" / "ledger.json")
    led.gpu_locked = True
    led.save()
    d = _daemon(tmp_path, platforms.SPARK, SPARK_ZONES)
    d.start()
    assert any(argv[-1] == "reset" for argv in spawned)
    d.shutdown()


# -- finding 3: gpu-clock hold refuses an unenforceable ceiling ----------------------


def _cli(capsys, *argv):
    rc = main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_hold_on_ventunoq_refused_and_writes_nothing(
    capsys, monkeypatch, spawned, tmp_path, json_flag
) -> None:
    monkeypatch.setattr(tuning, "_profile", lambda args, cfg=None: platforms.VENTUNOQ)
    data = tmp_path / "hold-data"
    rc, out, err = _cli(capsys, "gpu-clock", "hold", "1500", "--data-dir", str(data), *json_flag)
    assert rc == 1
    assert "unsupported platform" in err and "ventunoq" in err
    assert out == ""
    assert not data.exists() or list(data.iterdir()) == []
    assert spawned == []


def test_hold_accepts_platform_flag(capsys, spawned, tmp_path) -> None:
    data = tmp_path / "hold-data"
    rc, _o, err = _cli(
        capsys, "gpu-clock", "hold", "1500", "--platform", "ventunoq", "--data-dir", str(data)
    )
    assert rc == 1, err
    assert "unsupported platform" in err


def test_hold_on_spark_unchanged(capsys, monkeypatch, spawned, tmp_path) -> None:
    monkeypatch.setattr(tuning, "_profile", lambda args, cfg=None: platforms.SPARK)
    data = tmp_path / "hold-data"
    rc, _o, err = _cli(capsys, "gpu-clock", "hold", "1500", "--data-dir", str(data), "--json")
    assert rc == 0, err
    assert list(data.iterdir())
    assert spawned == []


def test_release_on_ventunoq_is_harmless(capsys, monkeypatch, spawned, tmp_path) -> None:
    """Release only removes a file: clearing a stale hold is allowed on any board."""
    monkeypatch.setattr(tuning, "_profile", lambda args, cfg=None: platforms.VENTUNOQ)
    rc, _o, err = _cli(
        capsys, "gpu-clock", "release", "--data-dir", str(tmp_path / "d"), "--platform", "ventunoq"
    )
    assert rc == 0, err
    assert spawned == []

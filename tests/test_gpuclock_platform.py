"""gpu-clock refuses platforms with no supported GPU clock control (ventunoq)
before the root helper is probed or spawned; Spark/Thor/Orin are unchanged."""

from __future__ import annotations

import dataclasses
import json
import subprocess

import pytest

from heat_governor import gpuclock, platforms
from heat_governor.cli import main
from heat_governor.cli._commands import tuning


class Spawned(list):
    """Records every attempt to run a subprocess."""


@pytest.fixture
def spawned(monkeypatch) -> Spawned:
    calls = Spawned()

    def fake_run(argv, *a, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="2000", stderr="")

    monkeypatch.setattr(gpuclock.subprocess, "run", fake_run)
    monkeypatch.setattr(gpuclock.shutil, "which", lambda _n: "/usr/bin/sudo")
    return calls


def _use(monkeypatch, profile) -> None:
    monkeypatch.setattr(tuning, "_profile", lambda args, cfg: profile)


def _run(capsys, *argv):
    rc = main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


VENTUNOQ_VARIANTS = [
    platforms.VENTUNOQ,
    dataclasses.replace(platforms.VENTUNOQ, gpu="none"),
    dataclasses.replace(platforms.VENTUNOQ, gpu="devfreq"),
]


@pytest.mark.parametrize("prof", VENTUNOQ_VARIANTS)
@pytest.mark.parametrize("verb", [("lock", "2000"), ("reset",)])
@pytest.mark.parametrize("apply_", [False, True])
def test_ventunoq_refused_and_helper_never_spawned(
    capsys, monkeypatch, spawned, prof, verb, apply_
) -> None:
    _use(monkeypatch, prof)
    argv = ["gpu-clock", *verb, "--json"] + (["--apply"] if apply_ else [])
    rc, _out, err = _run(capsys, *argv)
    assert rc == 1
    assert "unsupported platform" in err
    assert "ventunoq" in err
    assert "hint" in err.lower() or "remediation" in err.lower()
    assert spawned == []


def test_devfreq_mode_alone_is_refused(capsys, monkeypatch, spawned) -> None:
    prof = dataclasses.replace(platforms.SPARK, name="odd", gpu="devfreq")
    _use(monkeypatch, prof)
    rc, _o, err = _run(capsys, "gpu-clock", "lock", "2000", "--apply")
    assert rc == 1 and "unsupported platform" in err
    assert spawned == []


def test_status_on_ventunoq_does_not_spawn(capsys, monkeypatch, spawned) -> None:
    _use(monkeypatch, platforms.VENTUNOQ)
    rc, out, _ = _run(capsys, "gpu-clock", "status", "--json")
    assert rc == 0
    assert json.loads(out)["available"] is False
    assert spawned == []


@pytest.mark.parametrize("prof", [platforms.SPARK, platforms.THOR, platforms.ORIN])
def test_other_platforms_unchanged(capsys, monkeypatch, spawned, tmp_path, prof) -> None:
    assert gpuclock.unsupported_reason(prof) is None
    helper = tmp_path / "helper"
    helper.write_text("")
    real_init = gpuclock.GpuClock.__init__

    def init(self, *a, **kw):
        kw.setdefault("helper", str(helper))
        real_init(self, *a, **kw)

    monkeypatch.setattr(gpuclock.GpuClock, "__init__", init)
    _use(monkeypatch, prof)
    rc, _o, _e = _run(
        capsys, "gpu-clock", "lock", "2000", "--apply", "--json", "--data-dir", str(tmp_path)
    )
    assert rc == 0, _e
    assert ["sudo", "-n", str(helper), "lock", "2000"] in spawned


def test_generic_profile_not_refused() -> None:
    gen = dataclasses.replace(platforms.SPARK, name="generic", gpu="nvidia-smi")
    assert gpuclock.unsupported_reason(gen) is None
    assert gpuclock.unsupported_reason(dataclasses.replace(gen, gpu="none")) is None


def test_gpuclock_probe_short_circuits(spawned) -> None:
    g = gpuclock.GpuClock(unsupported="no GPU clock control")
    assert g.probe() is False
    assert g.reason == "no GPU clock control"
    assert spawned == []


def test_daemon_gpu_clock_never_probes_helper_on_ventunoq(tmp_path, monkeypatch):
    """[daemon] gpu_clock = true on a VENTUNO Q must not probe or spawn the root helper."""
    from heat_governor import gpuclock as gc
    from heat_governor.daemon import Daemon, Options
    from heat_governor.platforms import VENTUNOQ
    from heat_governor.sense import Sensor
    from tests.fakes import VENTUNOQ_ZONES, make_sys

    spawned: list = []
    monkeypatch.setattr(gc.subprocess, "run", lambda *a, **k: spawned.append(a) or None)
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES)
    opts = Options(
        apply=False,
        gpu_clock=True,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        log=[].append,
    )
    d = Daemon(VENTUNOQ, opts, sensor=Sensor(VENTUNOQ.zones, sys_root=sysr), docker=None)
    clock = d._default_gpuclock(opts)
    assert clock is not None and clock.unsupported
    assert clock.probe() is False
    assert spawned == []

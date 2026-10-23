"""One test per finding of the PR #4 code review (2026-10-23)."""

from __future__ import annotations

import http.client
import json
from pathlib import Path

import pytest

from heat_governor import config as cfgmod
from heat_governor import platforms, sense
from heat_governor.actuators import DutyCycler, Ledger
from heat_governor.cli import main
from heat_governor.daemon import Daemon, Options
from heat_governor.docker import Docker, DockerError
from heat_governor.platforms import SPARK
from heat_governor.sense import Sensor
from heat_governor.state import read_state
from tests.fakes import SPARK_ZONES, FakeDocker, container, make_sys, set_temp


def _daemon(tmp_path: Path, docker=None, gpu=None, cfg_text: str = "", **kw):
    sysr = make_sys(tmp_path, SPARK_ZONES)
    path = tmp_path / "config.toml"
    path.write_text(cfg_text)
    cfg = cfgmod.load(path)
    logs: list[str] = []
    opts = Options(
        apply=True,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        config=cfg,
        platform="spark",
        reload_every_s=0.0,
        containers=tuple(cfg.containers),
        log=logs.append,
        **kw,
    )
    d = Daemon(SPARK, opts, sensor=Sensor(SPARK.zones, sys_root=sysr), docker=docker, gpuclock=gpu)
    return d, sysr, path, logs


def _touch(path: Path, text: str) -> None:
    import os

    path.write_text(text)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


# 1. run --once never acts
def test_run_once_is_always_a_dry_run(tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "run",
            "--once",
            "--apply",
            "--no-docker",
            "--platform",
            "spark",
            "--json",
            "--state",
            str(tmp_path / "s.json"),
            "--data-dir",
            str(tmp_path / "d"),
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


# 2. the state is published even when an actuator fails, and a failing GPU helper backs off
class BrokenGpu:
    available, reason, ceiling_mhz = True, "ok", None

    def __init__(self) -> None:
        self.calls = 0

    def probe(self) -> bool:
        return True

    def lock(self, mhz):
        self.calls += 1
        return False, "helper hung"

    def reset(self):
        return True, "reset"


def test_state_is_published_before_and_despite_actuator_failures(tmp_path: Path) -> None:
    class ExplodingDocker(FakeDocker):
        def set_nano_cpus(self, ref, nano):
            raise RuntimeError("unexpected")

    dk = ExplodingDocker(container("s1", "lane", "serving", cap=True))
    gpu = BrokenGpu()
    d, sysr, _, logs = _daemon(tmp_path, docker=dk, gpu=gpu, gpu_clock=True)
    set_temp(sysr, 0, 91800)
    d.tick()
    d.tick()
    st = read_state(tmp_path / "state.json")
    assert st["level"] in ("hot", "critical")
    assert st["admit"] is False
    assert any("cap lane" in m and "unexpected" in m for m in logs)
    assert gpu.calls == 1  # backed off instead of retrying every tick


# 3. recover keeps an entry it could not undo
def test_recover_keeps_failed_restores_on_record(tmp_path: Path) -> None:
    class DownDocker(FakeDocker):
        def set_nano_cpus(self, ref, nano):
            raise DockerError("daemon starting")

    led = Ledger(tmp_path / "l.json")
    led.nano_cpus["s1"] = 16_000_000_000
    led.save()
    Ledger(tmp_path / "l.json").recover(DownDocker(container("s1", "lane", None)), [].append)
    assert Ledger(tmp_path / "l.json").nano_cpus == {"s1": 16_000_000_000}


# 4. no pause starts after stop()
def test_no_pause_after_stop(tmp_path: Path) -> None:
    dk = FakeDocker(container("b1", "job", "batch"))
    cyc = DutyCycler(dk, Ledger(tmp_path / "l.json"), [].append, apply=True, sleep=lambda s: None)
    cyc.set(1.0, {"b1": dk.c["b1"]})
    cyc.stop()
    cyc.cycle()
    assert ("pause", "b1") not in dk.calls
    assert dk.c["b1"].state == "running"


# 5. a container leaving the governed set gets its CPU cap back
def test_leaving_the_set_restores_the_cap(tmp_path: Path) -> None:
    dk = FakeDocker(container("s1", "lane", None, nano=16_000_000_000))
    d, sysr, path, _ = _daemon(
        tmp_path,
        docker=dk,
        cfg_text='[[containers]]\nname = "lane"\nrole = "serving"\ncpu_cap = true\n',
    )
    set_temp(sysr, 0, 91000)
    d.tick()
    assert dk.c["s1"].nano_cpus == 4_000_000_000
    _touch(path, "")
    d.tick()
    assert dk.c["s1"].nano_cpus == 16_000_000_000


# 6. a foreign pause that ends is governed again
def test_foreign_mark_clears_when_the_owner_unpauses(tmp_path: Path) -> None:
    dk = FakeDocker(container("b1", "job", "batch", state="paused"))
    cyc = DutyCycler(dk, Ledger(tmp_path / "l.json"), [].append, apply=True, sleep=lambda s: None)
    cyc.set(1.0, {"b1": dk.c["b1"]})
    cyc.cycle()
    assert "b1" in cyc.foreign
    dk.unpause("b1")  # the owner releases it
    cyc.cycle()
    assert "b1" not in cyc.foreign
    assert dk.c["b1"].state == "paused"
    assert "b1" in cyc.held


# 6b. the ledger is written when cycling starts and ends, not on every pause
def test_ledger_not_rewritten_every_period(tmp_path: Path) -> None:
    dk = FakeDocker(container("b1", "job", "batch"))
    led = Ledger(tmp_path / "l.json")
    saves = []
    led.save = lambda: saves.append(1)  # type: ignore[method-assign]
    cyc = DutyCycler(dk, led, [].append, apply=True, sleep=lambda s: None)
    cyc.set(0.5, {"b1": dk.c["b1"]})
    for _ in range(5):
        cyc.cycle()
    assert len(saves) == 1
    assert dk.calls.count(("pause", "b1")) == 5
    cyc.set(0.0, {"b1": dk.c["b1"]})
    cyc.cycle()
    assert len(saves) == 2
    assert "b1" not in led.paused


# 7. reload updates the GPU sensor and names restart-only keys
def test_reload_updates_gpu_sensor_and_flags_restart_only_keys(tmp_path: Path) -> None:
    d, _, path, logs = _daemon(tmp_path)
    d.tick()
    _touch(path, '[profile]\ngpu = "none"\n[daemon]\nlisten = "127.0.0.1:9999"\n')
    d.tick()
    assert d.sensor.gpu == "none"
    assert any("only after a restart" in m and "listen" in m for m in logs)


# 8. a broken HTTP response is a DockerError, not an escape
def test_http_protocol_errors_become_docker_errors(tmp_path: Path, monkeypatch) -> None:
    import heat_governor.docker as dmod

    def boom(self):
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(dmod._UnixConnection, "connect", lambda self: None)
    monkeypatch.setattr(dmod._UnixConnection, "request", lambda self, *a, **k: None)
    monkeypatch.setattr(dmod._UnixConnection, "getresponse", boom)
    docker = Docker(str(tmp_path / "x.sock"))
    with pytest.raises(DockerError):
        docker.inspect("c")


# 9. a driver without tlimit is probed once, not twice per tick
def test_tlimit_fallback_is_remembered(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_smi(fields, timeout):
        calls.append(fields)
        return "" if "temperature.gpu.tlimit" in fields else "70, 40.0, 2400, 90, 0x0"

    monkeypatch.setattr(sense, "_smi", fake_smi)
    monkeypatch.setattr(sense, "_TLIMIT_OK", None)
    monkeypatch.setattr(sense.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    for _ in range(3):
        assert sense.read_gpu_nvidia_smi()["temp_c"] == 70.0
    assert len(calls) == 4  # one probe with tlimit, then one call per read


# 10. CPU and GPU ceilings share one ramp implementation
def test_cpu_and_gpu_ramps_share_the_state_machine() -> None:
    from heat_governor.policy import Controller, _Ramp

    c = Controller(platforms.resolve("spark"))
    assert isinstance(c._cpu, _Ramp)
    assert isinstance(c._gpu, _Ramp)
    assert not hasattr(c, "_cpus_at")

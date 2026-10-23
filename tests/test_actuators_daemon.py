"""Actuators under the own-pause rule, crash recovery, and one daemon tick."""

from __future__ import annotations

import os
from pathlib import Path

from heat_governor.actuators import CpuCapper, DutyCycler, Ledger
from heat_governor.daemon import Daemon, Options
from heat_governor.platforms import SPARK
from heat_governor.recorder import iter_records
from heat_governor.sense import Sensor
from heat_governor.state import read_state
from tests.fakes import SPARK_ZONES, FakeDocker, container, make_sys, set_temp


def _log(lines: list[str]):
    return lines.append


def test_duty_cycle_pauses_then_releases(tmp_path: Path) -> None:
    d = FakeDocker(container("b1", "job", "batch"))
    cyc = DutyCycler(d, Ledger(tmp_path / "l.json"), [].append, apply=True, sleep=lambda s: None)
    cyc.set(0.5, {"b1": d.c["b1"]})
    cyc.cycle()
    assert d.calls == [("pause", "b1"), ("unpause", "b1")]
    assert d.c["b1"].state == "running"
    assert cyc.cycles == 1


def test_full_duty_holds_paused_and_records_before_acting(tmp_path: Path) -> None:
    d = FakeDocker(container("b1", "job", "batch"))
    led = Ledger(tmp_path / "l.json")
    cyc = DutyCycler(d, led, [].append, apply=True, sleep=lambda s: None)
    cyc.set(1.0, {"b1": d.c["b1"]})
    cyc.cycle()
    cyc.cycle()
    assert d.c["b1"].state == "paused"
    assert d.calls.count(("pause", "b1")) == 1
    assert "b1" in Ledger(tmp_path / "l.json").paused  # on disk


def test_foreign_pause_is_left_alone(tmp_path: Path) -> None:
    d = FakeDocker(container("b1", "job", "batch", state="paused"))
    logs: list[str] = []
    cyc = DutyCycler(d, Ledger(tmp_path / "l.json"), logs.append, apply=True, sleep=lambda s: None)
    cyc.set(0.5, {"b1": d.c["b1"]})
    cyc.cycle()
    cyc.set(0.0, {"b1": d.c["b1"]})
    cyc.cycle()
    assert d.c["b1"].state == "paused"  # never unpaused: not ours
    assert ("unpause", "b1") not in d.calls
    assert any("foreign" in m for m in logs)


def test_container_leaving_the_set_is_released(tmp_path: Path) -> None:
    d = FakeDocker(container("b1", "job", "batch"))
    cyc = DutyCycler(d, Ledger(tmp_path / "l.json"), [].append, apply=True, sleep=lambda s: None)
    cyc.set(1.0, {"b1": d.c["b1"]})
    cyc.cycle()
    cyc.set(1.0, {})
    cyc.cycle()
    assert d.c["b1"].state == "running"


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    d = FakeDocker(container("b1", "job", "batch"))
    cyc = DutyCycler(d, Ledger(tmp_path / "l.json"), [].append, apply=False, sleep=lambda s: None)
    cyc.set(1.0, {"b1": d.c["b1"]})
    cyc.cycle()
    cap = CpuCapper(d, Ledger(tmp_path / "l2.json"), [].append, apply=False)
    cap.set(d.c["b1"], 4)
    assert d.calls == []


def test_recover_undoes_only_the_ledger(tmp_path: Path) -> None:
    d = FakeDocker(
        container("b1", "ours", "batch", state="paused", nano=4_000_000_000),
        container("b2", "theirs", "batch", state="paused"),
    )
    led = Ledger(tmp_path / "l.json")
    led.paused.add("b1")
    led.nano_cpus["b1"] = 0
    led.save()
    Ledger(tmp_path / "l.json").recover(d, [].append)
    assert d.c["b1"].state == "running"
    assert d.c["b1"].nano_cpus == (os.cpu_count() or 1) * 1_000_000_000
    assert d.c["b2"].state == "paused"
    assert Ledger(tmp_path / "l.json").paused == set()


def test_cpu_cap_remembers_and_restores_the_original(tmp_path: Path) -> None:
    d = FakeDocker(container("s1", "lane", "serving", cap=True, nano=16_000_000_000))
    led = Ledger(tmp_path / "l.json")
    cap = CpuCapper(d, led, [].append, apply=True)
    cap.set(d.c["s1"], 4)
    cap.set(d.c["s1"], 8)
    assert d.c["s1"].nano_cpus == 8_000_000_000
    assert led.nano_cpus == {"s1": 16_000_000_000}
    cap.set(d.c["s1"], None)
    assert d.c["s1"].nano_cpus == 16_000_000_000
    assert led.nano_cpus == {}


def _daemon(tmp_path: Path, docker: FakeDocker, *, apply: bool = True, **kw) -> tuple[Daemon, Path]:
    sysr = make_sys(tmp_path, SPARK_ZONES)
    opts = Options(
        apply=apply,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        log=[].append,
        **kw,
    )
    d = Daemon(SPARK, opts, sensor=Sensor(SPARK.zones, sys_root=sysr), docker=docker)
    return d, sysr


def test_daemon_tick_publishes_and_records(tmp_path: Path) -> None:
    dk = FakeDocker(container("b1", "job", "batch"), container("s1", "lane", "serving"))
    d, sysr = _daemon(tmp_path, dk)
    d.start()
    d.tick()
    st = read_state(tmp_path / "state.json")
    assert st["level"] == "cool"
    assert st["admit"] is True
    assert st["platform"] == "spark"
    # the state is published BEFORE the actuators run: containers show from the next tick
    d.tick()
    st = read_state(tmp_path / "state.json")
    assert {c["name"] for c in st["containers"]} == {"job", "lane"}
    set_temp(sysr, 0, 92800)
    d.tick()
    st = read_state(tmp_path / "state.json")
    assert st["level"] in ("hot", "critical")
    assert st["admit"] is False
    d.cycler.stop()
    recs = list(iter_records(tmp_path / "data" / "records"))
    assert recs[0]["event"] == "start"
    assert any(r.get("level") in ("hot", "critical") for r in recs)


def test_serving_container_is_never_paused_or_capped_without_opt_in(tmp_path: Path) -> None:
    dk = FakeDocker(container("s1", "lane", "serving"))
    d, sysr = _daemon(tmp_path, dk)
    set_temp(sysr, 0, 99000)
    d.discover()
    d.tick()
    d.cycler.cycle()
    assert d.batch() == {}
    assert d.capped() == []
    assert dk.calls == []


def test_named_serving_with_cpu_cap(tmp_path: Path) -> None:
    dk = FakeDocker(container("s1", "lane", None, nano=0))
    d, sysr = _daemon(tmp_path, dk, serving=("lane",), serving_cpu_cap=True, use_labels=False)
    set_temp(sysr, 0, 91000)
    d.discover()
    d.tick()
    assert dk.c["s1"].nano_cpus == 4_000_000_000
    assert ("pause", "s1") not in dk.calls
    d.shutdown()
    # an original of 0 (no limit) comes back as every host CPU: docker ignores 0
    assert dk.c["s1"].nano_cpus == (os.cpu_count() or 1) * 1_000_000_000


def test_daemon_without_docker_still_signals(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, SPARK_ZONES)
    opts = Options(state_path=tmp_path / "state.json", data_dir=tmp_path / "d", log=[].append)
    d = Daemon(SPARK, opts, sensor=Sensor(SPARK.zones, sys_root=sysr), docker=None)
    d.tick()
    assert read_state(tmp_path / "state.json")["admit"] is True

"""How often unit CPU use is read (PR #5 review): an unresolvable cgroup is
looked up again only every RETRY_S, and a levels-mode unit is sampled at most
every USE_SAMPLE_S. No test reads a real cgroup or calls a real systemctl."""

from __future__ import annotations

from pathlib import Path

import pytest

from heat_governor.config import UnitRule
from heat_governor.daemon import Daemon, Options
from heat_governor.platforms import SPARK
from heat_governor.sense import Sensor
from heat_governor.units import Systemctl
from tests.fakes import SPARK_ZONES, FakeSystemctl, make_sys

APP = "app.slice"


def _counting(out: str):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(argv)
        return 0, out, ""

    return calls, run


def test_a_unit_without_a_cgroup_is_looked_up_once_per_retry_s(tmp_path: Path) -> None:
    calls, run = _counting("ControlGroup=\n")
    t = [0.0]
    sc = Systemctl(run=run, cgroup_root=tmp_path, clock=lambda: t[0])
    for _ in range(10):
        assert sc.cpu_usage_usec("x.scope") is None
        t[0] += 1.0
    assert len(calls) == 1
    t[0] += Systemctl.RETRY_S
    sc.cpu_usage_usec("x.scope")
    assert len(calls) == 2


def test_an_unreadable_cpu_stat_is_looked_up_once_per_retry_s(tmp_path: Path) -> None:
    calls, run = _counting("ControlGroup=/app.slice\n")  # no cpu.stat under tmp_path
    t = [0.0]
    sc = Systemctl(run=run, cgroup_root=tmp_path, clock=lambda: t[0])
    for _ in range(5):
        sc.cpu_usage_usec(APP)
        t[0] += 1.0
    assert len(calls) == 1


def test_a_levels_unit_is_sampled_at_most_every_five_seconds(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, SPARK_ZONES)
    sc = FakeSystemctl({APP: None})
    t = [1000.0]
    opts = Options(
        apply=True,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        log=[].append,
        units=(UnitRule(APP),),
        record=False,
    )
    d = Daemon(
        SPARK, opts, sensor=Sensor(SPARK.zones, sys_root=sysr), systemctl=sc, clock=lambda: t[0]
    )
    for _ in range(11):
        sc.usage[APP] = sc.usage.get(APP, 0) + 2_000_000  # 2 CPUs
        t[0] += 1.0
        d.tick()
    assert sc.usage_reads == 3  # t+1, t+6, t+11
    assert d.unit_use[APP] == pytest.approx(2.0)

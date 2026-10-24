"""The recorded original CPU limit is never a value the governor wrote itself.

Live sequence (DGX Spark, 2026-10-23): restore writes the original back and
drops the ledger entry; a new cap follows before the next discovery, and the
discovery snapshot still shows the previous CAP -- which then became the
"original", so every later restore "restored" the cap (12 CPUs for ~27 h).
"""

from __future__ import annotations

import os
from pathlib import Path

from heat_governor.actuators import CpuCapper, Ledger
from heat_governor.docker import DockerError
from tests.fakes import FakeDocker, container

ALL = (os.cpu_count() or 1) * 1_000_000_000


def test_restore_then_cap_with_a_stale_snapshot_keeps_the_true_original(tmp_path: Path) -> None:
    d = FakeDocker(container("s1", "lane", "serving", cap=True, nano=20_000_000_000))
    led = Ledger(tmp_path / "l.json")
    logs: list[str] = []
    cap = CpuCapper(d, led, logs.append, apply=True)
    cap.set(d.c["s1"], 12)
    stale = d.c["s1"]  # the discovery snapshot taken while capped: NanoCpus=12e9
    assert stale.nano_cpus == 12_000_000_000
    cap.set(stale, None)  # restore 20
    assert d.c["s1"].nano_cpus == 20_000_000_000
    cap.set(stale, 12)  # one second later, before the next discovery
    assert led.nano_cpus == {"s1": 20_000_000_000}
    assert Ledger(tmp_path / "l.json").nano_cpus == {"s1": 20_000_000_000}  # on disk
    cap.set(stale, None)
    assert d.c["s1"].nano_cpus == 20_000_000_000
    assert logs[-1].endswith("restore lane cpus=20")


def test_an_unlimited_original_survives_a_restore_cap_cycle(tmp_path: Path) -> None:
    d = FakeDocker(container("s1", "lane", "serving", cap=True, nano=0))
    led = Ledger(tmp_path / "l.json")
    logs: list[str] = []
    cap = CpuCapper(d, led, logs.append, apply=True)
    cap.set(d.c["s1"], 4)
    cap.set(d.c["s1"], None)
    assert d.c["s1"].nano_cpus == ALL  # docker ignores 0, so every host CPU is written
    cap.set(d.c["s1"], 4)  # a fresh read now shows ALL: that is our own write, not the original
    assert led.nano_cpus == {"s1": 0}
    cap.set(d.c["s1"], None)
    assert logs[-1].endswith("restore lane cpus=unlimited")


def test_a_limit_changed_by_the_operator_after_a_restore_is_the_new_original(
    tmp_path: Path,
) -> None:
    d = FakeDocker(container("s1", "lane", "serving", cap=True, nano=20_000_000_000))
    led = Ledger(tmp_path / "l.json")
    cap = CpuCapper(d, led, [].append, apply=True)
    stale = d.c["s1"]
    cap.set(stale, 12)
    cap.set(stale, None)
    d.set_nano_cpus("s1", 10_000_000_000)  # the operator's own docker update
    cap.set(stale, 8)  # below the new limit (a band above it never raises it: see below)
    assert led.nano_cpus == {"s1": 10_000_000_000}


def test_no_cap_without_a_fresh_read_of_the_original(tmp_path: Path) -> None:
    class Unreadable(FakeDocker):
        def inspect(self, ref):
            raise DockerError("socket timeout")

    d = Unreadable(container("s1", "lane", "serving", cap=True, nano=12_000_000_000))
    led = Ledger(tmp_path / "l.json")
    cap = CpuCapper(d, led, [].append, apply=True)
    try:
        cap.set(d.c["s1"], 4)
    except DockerError:
        pass
    assert led.nano_cpus == {}
    assert ("cpus", "s1", 4_000_000_000) not in d.calls
    assert cap.current.get("s1", "unset") == "unset"  # retried on the next tick


def test_crash_recovery_of_an_unlimited_original_keeps_it_unlimited(tmp_path: Path) -> None:
    from heat_governor.daemon import Daemon, Options
    from heat_governor.platforms import SPARK
    from heat_governor.sense import Sensor
    from tests.fakes import SPARK_ZONES, make_sys

    led = Ledger(tmp_path / "data" / "ledger.json")
    led.nano_cpus = {"s1": 0}  # a crashed run capped an unlimited container
    led.save()
    d = FakeDocker(container("s1", "lane", "serving", cap=True, nano=4_000_000_000))
    sysr = make_sys(tmp_path, SPARK_ZONES)
    opts = Options(apply=True, data_dir=tmp_path / "data", log=[].append, record=False)
    gov = Daemon(SPARK, opts, sensor=Sensor(SPARK.zones, sys_root=sysr), docker=d)
    gov.start()
    assert d.c["s1"].nano_cpus == ALL  # recover wrote every host CPU for "no limit"
    gov.capper.set(d.c["s1"], 4)
    assert gov.ledger.nano_cpus == {"s1": 0}  # not ALL
    gov.shutdown()


def test_recover_hands_back_what_it_wrote(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.json")
    led.nano_cpus = {"s1": 0, "s2": 6_000_000_000}
    d = FakeDocker(
        container("s1", "a", "serving", nano=4_000_000_000),
        container("s2", "b", "serving", nano=4_000_000_000),
    )
    assert led.recover(d, [].append) == {"s1": (ALL, 0), "s2": (6_000_000_000, 6_000_000_000)}

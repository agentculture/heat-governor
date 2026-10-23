"""Sensing and platform detection against sysfs trees captured from the boards."""

from __future__ import annotations

from pathlib import Path

import pytest

from heat_governor import platforms
from heat_governor.platforms import ORIN, SPARK, THOR, Limits
from heat_governor.sense import Sensor, capabilities, read_fans, read_zones
from tests.fakes import ORIN_ZONES, SPARK_ZONES, THOR_ZONES, make_sys, set_temp


def test_spark_reading(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, SPARK_ZONES)
    r = Sensor(SPARK.zones, sys_root=sysr).read()
    assert r.temp_c == 83.8
    assert r.hottest.key == "0:acpitz"
    assert len(r.zones) == 7


def test_unreadable_zone_is_skipped_not_hot(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, THOR_ZONES)
    r = Sensor(THOR.zones, sys_root=sysr).read()
    gpu = [z for z in r.zones if z.type == "gpu-thermal"][0]
    assert gpu.temp_c is None
    assert r.temp_c == 47.875


def test_orin_enodata_zones_are_not_watched_and_not_counted(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, ORIN_ZONES)
    r = Sensor(ORIN.zones, sys_root=sysr).read()
    assert r.temp_c == 60.0
    assert not any(z.type.startswith("cv") for z in r.watched_zones())


def test_every_watched_zone_unreadable_is_a_lost_reading(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, [("acpitz", None, [])])
    assert Sensor(("acpitz",), sys_root=sysr).read().temp_c is None


def test_implausible_value_dropped(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, SPARK_ZONES)
    set_temp(sysr, 0, 250000)
    assert Sensor(SPARK.zones, sys_root=sysr).read().temp_c == 83.8


def test_trips_and_fans(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, ORIN_ZONES, fan=(85,))
    zones = read_zones(sysr, trips=True)
    assert ("passive", 99.0) in zones[0].trips
    assert read_fans(sysr) == {"pwmfan.pwm1": 85.0}


def test_capabilities_never_claims_unsupported_controls(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, THOR_ZONES, fan=(69,))
    caps = capabilities(sysr, tmp_path)
    assert caps["control"]["docker_pause"] is False  # no socket in the fake root
    assert caps["control"]["admission_signal"] is True
    assert "not driven" in caps["control"]["gpu_clock_lock"]


@pytest.mark.parametrize(
    "model,want",
    [
        ("NVIDIA Jetson AGX Thor Developer Kit", "thor"),
        ("NVIDIA Jetson AGX Orin Developer Kit", "orin"),
    ],
)
def test_detect_jetson(tmp_path: Path, model: str, want: str) -> None:
    make_sys(tmp_path, ORIN_ZONES, model=model)
    assert platforms.detect(tmp_path, nvidia_smi=False) == want


def test_detect_unknown_is_generic(tmp_path: Path) -> None:
    make_sys(tmp_path, SPARK_ZONES)
    assert platforms.detect(tmp_path, nvidia_smi=False) == "generic"


def test_generic_profile_from_trip_points(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, SPARK_ZONES)
    p = platforms.generic_profile(sysr)
    assert p.limits.crit_c == pytest.approx(89.8)
    assert p.zones == ()


def test_resolve_with_overrides() -> None:
    p = platforms.resolve("spark", config={"profile": {"hot_c": 91.0, "duty_period_s": 1.0}})
    assert p.limits.hot_c == 91.0
    assert p.duty_period_s == 1.0
    assert "overridden" in p.provenance


def test_resolve_unknown_name() -> None:
    with pytest.raises(ValueError):
        platforms.resolve("toaster")


def test_limits_must_ascend() -> None:
    with pytest.raises(ValueError):
        Limits(warm_c=90, hot_c=85, crit_c=95)


def test_zone_raising_eagain_reads_as_unreadable(tmp_path: Path, monkeypatch) -> None:
    import errno
    import os as _os

    sysr = make_sys(tmp_path, SPARK_ZONES)
    bad = str(sysr / "class" / "thermal" / "thermal_zone1" / "temp")
    real_read = _os.read
    opened: dict[int, str] = {}
    real_open = _os.open

    def fake_open(path, flags, *a):
        fd = real_open(path, flags, *a)
        opened[fd] = str(path)
        return fd

    def fake_read(fd, n):
        if opened.get(fd) == bad:
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
        return real_read(fd, n)

    monkeypatch.setattr("heat_governor.sense.os.open", fake_open)
    monkeypatch.setattr("heat_governor.sense.os.read", fake_read)
    zones = read_zones(sysr)
    assert zones[1].temp_c is None
    assert zones[0].temp_c == 83.8


def test_system_probe_cpu_memory_pressure_disks(tmp_path: Path) -> None:
    from heat_governor.sense import SystemProbe

    proc, sysr = tmp_path / "proc", tmp_path / "sys"
    (proc / "pressure").mkdir(parents=True)
    (sysr / "devices/system/cpu/cpu0/cpufreq").mkdir(parents=True)
    (sysr / "devices/system/cpu/online").write_text("0-19\n")
    (sysr / "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").write_text("2400000\n")
    (proc / "loadavg").write_text("5.70 5.6 5.5 2/900 123\n")
    (proc / "meminfo").write_text(
        "MemTotal: 1000000 kB\nMemAvailable: 250000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
    )
    (proc / "pressure/io").write_text(
        "some avg10=62.70 avg60=61.16 avg300=56.55 total=1\n"
        "full avg10=55.78 avg60=1 avg300=1 total=1\n"
    )
    clock = iter([0.0, 2.0])

    def stat(user, idle, iowait):
        return f"cpu  {user} 0 0 {idle} {iowait} 0 0 0 0 0\n"

    def disks(rd, wr, busy):
        return (
            f" 259 0 nvme0n1 1 0 {rd} 0 1 0 {wr} 0 0 {busy} 0 0 0 0 0 0 0\n"
            " 259 1 nvme0n1p1 1 0 999 0 1 0 999 0 0 999 0 0 0 0 0 0 0\n"
            "   7 0 loop0 1 0 999 0 0 0 0 0 0 9 0 0 0 0 0 0 0\n"
        )

    (proc / "stat").write_text(stat(100, 100, 0))
    (proc / "diskstats").write_text(disks(0, 0, 0))
    probe = SystemProbe(proc, sysr, clock=lambda: next(clock))
    first = probe.read()
    assert "cpu:busy_pct" not in first
    assert first["cpu:online"] == 20
    assert first["mem:used_pct"] == 75.0
    assert first["psi:io_some_avg10"] == 62.7
    (proc / "stat").write_text(stat(175, 100, 25))  # 75 busy + 25 iowait of 100 jiffies
    (proc / "diskstats").write_text(disks(4_000_000, 2_000_000, 1000))
    second = probe.read()
    assert second["cpu:busy_pct"] == 75.0
    assert second["cpu:iowait_pct"] == 25.0
    assert second["disk:nvme0n1:read_mbs"] == 1024.0  # 4e6 sectors * 512 B / 2 s
    assert second["disk:nvme0n1:busy_pct"] == 50.0
    assert not any(k.startswith(("disk:nvme0n1p1", "disk:loop")) for k in second)
    assert second["cpu:mhz_max"] == 2400
    assert second["cpu:load1"] == 5.7


def test_nvme_every_sensor_and_limits(tmp_path: Path) -> None:
    import os

    from heat_governor.recorder import _is_temp
    from heat_governor.sense import SystemProbe

    sysr = tmp_path / "sys"
    dev = sysr / "devices" / "nvme0"
    dev.mkdir(parents=True)
    hw = sysr / "class" / "hwmon" / "hwmon1"
    hw.mkdir(parents=True)
    (hw / "name").write_text("nvme\n")
    os.symlink(dev, hw / "device")
    for i, (label, temp, mx, crit) in enumerate(
        [("Composite", 58850, 82850, 84850), ("Sensor 1", 62850, 65261850, None)], start=1
    ):
        (hw / f"temp{i}_input").write_text(f"{temp}\n")
        (hw / f"temp{i}_label").write_text(label + "\n")
        (hw / f"temp{i}_max").write_text(f"{mx}\n")
        if crit:
            (hw / f"temp{i}_crit").write_text(f"{crit}\n")
    out = SystemProbe(tmp_path / "proc", sysr).read()
    assert out["nvme:nvme0:composite"] == 58.85
    assert out["nvme:nvme0:sensor_1"] == 62.85
    assert out["nvme:nvme0:composite_crit_c"] == 84.85
    assert "nvme:nvme0:sensor_1_max_c" not in out  # placeholder limit dropped
    assert _is_temp("nvme:nvme0:sensor_1")
    assert not _is_temp("nvme:nvme0:composite_crit_c")

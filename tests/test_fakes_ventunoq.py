"""The VENTUNO Q fake tree (fakes.make_sys keyword extras); never touches the real /sys."""

from __future__ import annotations

from tests.fakes import SPARK_ZONES, VENTUNOQ_ZONES, make_sys


def test_make_sys_defaults_write_no_extras(tmp_path):
    sysr = make_sys(tmp_path, SPARK_ZONES)
    assert not (tmp_path / "proc").exists()
    assert not (sysr / "class" / "devfreq").exists()
    assert not (sysr / "class" / "hwmon").exists()


def test_compatible_is_nul_separated(tmp_path):
    make_sys(tmp_path, VENTUNOQ_ZONES, compatible=["arduino,monza", "qcom,qcs8300"])
    raw = (tmp_path / "proc" / "device-tree" / "compatible").read_bytes()
    assert raw == b"arduino,monza\x00qcom,qcs8300\x00"


def test_compatible_and_model_share_device_tree(tmp_path):
    make_sys(tmp_path, VENTUNOQ_ZONES, model="Arduino VENTUNO Q", compatible=["arduino,monza"])
    dt = tmp_path / "proc" / "device-tree"
    assert (dt / "model").exists() and (dt / "compatible").exists()


def test_devfreq_files(tmp_path):
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES, devfreq=True)
    gpu = sysr / "class" / "devfreq" / "3d00000.gpu"
    assert (gpu / "cur_freq").read_text().strip() == "479000000"
    assert (gpu / "max_freq").read_text().strip() == "877000000"
    assert (gpu / "available_frequencies").read_text().split() == [
        "479000000",
        "599000000",
        "780000000",
        "877000000",
    ]


def test_ina232_hwmon(tmp_path):
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES, ina232=12_960_000)
    hw = [p for p in (sysr / "class" / "hwmon").iterdir() if (p / "name").read_text() == "ina232\n"]
    assert len(hw) == 1
    assert (hw[0] / "power1_input").read_text().strip() == "12960000"


def test_ina232_coexists_with_fan(tmp_path):
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES, fan=(100,), ina232=1)
    names = sorted((p / "name").read_text().strip() for p in (sysr / "class" / "hwmon").iterdir())
    assert names == ["ina232", "pwmfan"]


def test_ventunoq_trips():
    by = {z[0]: [(k, t // 1000) for k, t in z[2]] for z in VENTUNOQ_ZONES}
    cpu = [("passive", 135), ("passive", 126), ("passive", 130)]
    acc = [("passive", 135), ("passive", 115), ("passive", 130)]
    two = [("passive", 135), ("passive", 130)]
    pmic = [("passive", 105), ("critical", 125)]
    assert by["cpu-0-0-0-thermal"] == cpu
    assert by["gpuss-0-thermal"] == acc
    assert by["nsp-0-0-0-thermal"] == acc
    assert by["cpuss-0-0-thermal"] == [("passive", 135), ("passive", 130), ("active", 38)]
    for name in ("cpuss-1-0-thermal", "ddrss-0-thermal", "aoss-0-thermal", "pcie-0-thermal"):
        assert by[name] == two
    assert by["pmm8620au-0-therm"] == pmic
    assert by["pmm8650au-1-therm"] == pmic


def test_ventunoq_only_pmic_zones_are_critical():
    crit = {z[0]: t for z in VENTUNOQ_ZONES for k, t in z[2] if k == "critical"}
    assert crit == {"pmm8620au-0-therm": 125000, "pmm8650au-1-therm": 125000}
    assert all(k in ("passive", "critical", "active") for z in VENTUNOQ_ZONES for k, _ in z[2])


def test_ventunoq_idle_temps():
    temps = {z[0]: z[1] for z in VENTUNOQ_ZONES}
    assert max(temps, key=temps.get).startswith("pmm86")
    assert all(42000 <= t <= 48000 for t in temps.values())

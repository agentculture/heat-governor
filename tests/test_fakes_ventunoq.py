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
    by = {z[0]: z for z in VENTUNOQ_ZONES}
    trips = lambda name: [t for _, t in by[name][2]]  # noqa: E731
    assert trips("cpu-0-0-0-thermal") == [126000, 130000, 135000]
    assert trips("gpuss-0-thermal") == [115000, 130000, 135000]
    assert trips("nsp-0-0-0-thermal") == [115000, 130000, 135000]
    assert trips("ddrss-0-thermal") == [130000, 135000]
    for pm in ("pmm8620au-0-therm", "pmm8650au-1-therm"):
        assert by[pm][2] == [("passive", 105000), ("critical", 125000)]
    temps = {z[0]: z[1] for z in VENTUNOQ_ZONES}
    assert max(temps, key=temps.get).startswith("pmm86")
    assert {"aoss-0-thermal", "pcie-0-thermal"} <= set(temps)

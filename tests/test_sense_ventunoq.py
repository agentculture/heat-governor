"""VENTUNO Q sensing: Adreno GPU clock via devfreq, board power via ina232, fan ownership."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heat_governor import config, sense
from heat_governor.cli._commands.governing import gpu_text
from heat_governor.platforms import BUILTIN
from heat_governor.sense import Sensor, capabilities
from tests.fakes import ORIN_ZONES, SPARK_ZONES, THOR_ZONES, VENTUNOQ_ZONES, make_sys

GOLDEN = json.loads((Path(__file__).parent / "golden_sense_legacy.json").read_text())


def _cooling(sysr: Path, idx: int, ctype: str) -> None:
    cd = sysr / "class" / "thermal" / f"cooling_device{idx}"
    cd.mkdir(parents=True)
    (cd / "type").write_text(ctype + "\n")
    (cd / "cur_state").write_text("5\n")
    (cd / "max_state").write_text("10\n")


def _emc2305_fan(sysr: Path) -> None:
    hw = sysr / "class" / "hwmon" / "hwmon2"
    hw.mkdir(parents=True)
    (hw / "name").write_text("emc2305\n")
    (hw / "fan1_input").write_text("3200\n")


def test_devfreq_gpu_clock_and_board_power(tmp_path):
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES, devfreq=True, ina232=12_960_000)
    gpu = Sensor(("cpu",), sys_root=sysr, gpu="devfreq", clock=lambda: 1.0).read().as_dict()["gpu"]
    assert gpu == {"3d00000.gpu.mhz": 479.0, "board_power_w": 12.96}
    assert gpu_text(gpu) == "3d00000.gpu clock 479 MHz, board power 12.96 W"


def test_devfreq_without_ina232_reports_clock_only(tmp_path):
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES, devfreq=True, fan=(100,))
    gpu = sense.read_gpu_devfreq(sysr)
    assert gpu == {"3d00000.gpu.mhz": 479.0}


def test_devfreq_missing_node_is_empty(tmp_path):
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES)
    assert sense.read_gpu_devfreq(sysr) == {}


def test_ventunoq_profile_uses_devfreq_and_config_accepts_it():
    assert BUILTIN["ventunoq"].gpu == "devfreq"
    problems: list[str] = []
    config._validate_profile({"gpu": "devfreq"}, problems)
    assert not any("profile.gpu" in p for p in problems)
    bad: list[str] = []
    config._validate_profile({"gpu": "bogus"}, bad)
    assert any("profile.gpu" in p and "devfreq" in p for p in bad)


def test_fan_is_kernel_owned_when_a_cooling_device_matches(tmp_path):
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES)
    _emc2305_fan(sysr)
    _cooling(sysr, 7, "emc2305_fan")
    ctl = capabilities(sysr, tmp_path)["control"]
    assert ctl["fan_pwm"] == "present, kernel-owned (thermal cooling device)"


def test_fan_without_cooling_device_keeps_firmware_text(tmp_path):
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES)
    _emc2305_fan(sysr)
    _cooling(sysr, 0, "Processor")
    ctl = capabilities(sysr, tmp_path)["control"]
    assert ctl["fan_pwm"] == "present, firmware/nvfancontrol owned"


@pytest.mark.parametrize(
    "name,zones,gpu,fan",
    [
        ("spark", SPARK_ZONES, "none", None),
        ("thor", THOR_ZONES, "jetson", (100,)),
        ("orin", ORIN_ZONES, "jetson", (100,)),
    ],
)
def test_legacy_platforms_output_is_unchanged(tmp_path, monkeypatch, name, zones, gpu, fan):
    monkeypatch.setattr(sense.shutil, "which", lambda _n: None)
    sysr = make_sys(tmp_path, zones, fan=fan)
    got = {
        "sense": Sensor(("x",), sys_root=sysr, gpu=gpu, clock=lambda: 1.0).read().as_dict(),
        "caps": capabilities(sysr, tmp_path),
    }
    assert json.loads(json.dumps(got, default=str)) == GOLDEN[name]

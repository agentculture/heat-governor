"""Human-readable ``sense`` output: every fan and GPU value carries a label and unit."""

from __future__ import annotations

from heat_governor.cli._commands.governing import fan_text, gpu_text

SPARK_GPU = {
    "temp_c": 71.0,
    "power_w": 33.19,
    "sm_mhz": 2184.0,
    "util_pct": 95.0,
    "events": "0x0000000000000000",
    "gr_mhz": 2184.0,
    "mem_mhz": None,
    "mem_util_pct": 0.0,
    "tlimit_c": 22.0,
    "limit_c": 93.0,
}


def test_fan_tach_reads_in_rpm() -> None:
    assert fan_text("acpi_fan.fan1_input", 3412.0) == "acpi_fan 3412 RPM"


def test_fan_pwm_reads_as_duty() -> None:
    assert fan_text("pwmfan.pwm1", 85.0) == "pwmfan duty 85/255 (33%)"


def test_unknown_fan_key_falls_back_to_raw() -> None:
    assert fan_text("odd", 1.0) == "odd=1"


def test_spark_gpu_line_is_labelled() -> None:
    assert gpu_text(SPARK_GPU) == (
        "temp 71 C, power 33.19 W, SM clock 2184 MHz, util 95%, "
        "throttle reasons 0x0000000000000000, graphics clock 2184 MHz, memory clock n/a, "
        "memory util 0%, headroom 22 C to the 93 C limit"
    )


def test_headroom_without_a_limit() -> None:
    assert gpu_text({"tlimit_c": 20.0}) == "headroom 20 C to its limit"


def test_jetson_gpu_line() -> None:
    gpu = {"util_pct": 12.5, "17000000.gpu.mhz": 1300.5}
    assert gpu_text(gpu) == "util 12.5%, 17000000.gpu clock 1300.5 MHz"


def test_unknown_gpu_key_falls_back_to_raw() -> None:
    assert gpu_text({"x": 1}) == "x=1"

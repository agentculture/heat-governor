"""VENTUNO Q detection (device-tree compatible) and its declared built-in profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from heat_governor import platforms
from heat_governor.platforms import BUILTIN, VENTUNOQ
from heat_governor.sense import Sensor
from tests.fakes import ORIN_ZONES, SPARK_ZONES, THOR_ZONES, VENTUNOQ_ZONES, make_sys

MONZA = ["arduino,monza", "qcom,monaco-monza", "qcom,qcs8300"]
IMOLA = ["arduino,imola", "qcom,qrb2210"]


def _detect(root: Path, **kw) -> str:
    return platforms.detect(root, nvidia_smi=False, **kw)


def test_monza_compatible_is_ventunoq(tmp_path: Path) -> None:
    make_sys(
        tmp_path,
        VENTUNOQ_ZONES,
        model="Qualcomm Technologies, Inc. Monaco Monza addons",
        compatible=MONZA,
    )
    assert _detect(tmp_path) == "ventunoq"


@pytest.mark.parametrize("compat", [["qcom,qcs8300"], ["foo", "arduino,monza"]])
def test_either_exact_token_matches(tmp_path: Path, compat: list[str]) -> None:
    make_sys(tmp_path, VENTUNOQ_ZONES, compatible=compat)
    assert _detect(tmp_path) == "ventunoq"


@pytest.mark.parametrize("compat", [["arduino,monza2"], ["xarduino,monza"], ["qcom,qcs8300-x"]])
def test_tokens_are_matched_exactly(tmp_path: Path, compat: list[str]) -> None:
    make_sys(tmp_path, VENTUNOQ_ZONES, compatible=compat)
    assert _detect(tmp_path) == "generic"


def test_uno_q_resolves_to_generic(tmp_path: Path) -> None:
    make_sys(tmp_path, VENTUNOQ_ZONES, compatible=IMOLA)
    assert _detect(tmp_path) == "generic"
    assert platforms.resolve(root=tmp_path).name == "generic"


def test_jetsons_still_detected(tmp_path: Path) -> None:
    thor = tmp_path / "thor"
    make_sys(
        thor,
        THOR_ZONES,
        model="NVIDIA Jetson AGX Thor Developer Kit",
        compatible=["nvidia,p4071-0000+p3834-0008", "nvidia,tegra264"],
    )
    assert _detect(thor) == "thor"
    orin = tmp_path / "orin"
    make_sys(
        orin,
        ORIN_ZONES,
        model="NVIDIA Jetson AGX Orin Developer Kit",
        compatible=["nvidia,p3737-0000+p3701-0005", "nvidia,tegra234"],
    )
    assert _detect(orin) == "orin"


def test_spark_still_detected_by_gb10(tmp_path: Path, monkeypatch) -> None:
    make_sys(tmp_path, SPARK_ZONES)

    class R:
        stdout = "NVIDIA GB10\n"

    monkeypatch.setattr(platforms.shutil, "which", lambda _n: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(platforms.subprocess, "run", lambda *a, **k: R())
    assert platforms.detect(tmp_path) == "spark"


def test_compatible_checked_before_nvidia_smi(tmp_path: Path, monkeypatch) -> None:
    make_sys(tmp_path, VENTUNOQ_ZONES, compatible=MONZA)

    def boom(*a, **k):
        raise AssertionError("nvidia-smi probed")

    monkeypatch.setattr(platforms.subprocess, "run", boom)
    assert platforms.detect(tmp_path) == "ventunoq"


def test_resolve_ventunoq(tmp_path: Path) -> None:
    make_sys(tmp_path, VENTUNOQ_ZONES, compatible=MONZA)
    assert platforms.resolve(root=tmp_path).name == "ventunoq"
    assert platforms.resolve("ventunoq") is VENTUNOQ
    assert BUILTIN["ventunoq"] is VENTUNOQ


def test_unknown_platform_error_lists_ventunoq() -> None:
    with pytest.raises(ValueError, match="ventunoq"):
        platforms.resolve("nope")


def test_profile_watches_only_the_five_families() -> None:
    families = ("cpu-", "cpuss-", "gpuss-", "nsp-", "ddrss-")
    assert VENTUNOQ.zones
    assert all(z.startswith(families) and z.endswith("-thermal") for z in VENTUNOQ.zones)
    assert {z.split("-")[0] for z in VENTUNOQ.zones} == {"cpu", "cpuss", "gpuss", "nsp", "ddrss"}
    assert len(VENTUNOQ.zones) == 16 + 4 + 2 + 6 + 2
    assert not any(z.startswith(("pmm", "aoss", "pcie")) for z in VENTUNOQ.zones)


def test_sensor_ignores_pmic_zone(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, VENTUNOQ_ZONES)
    r = Sensor(VENTUNOQ.zones, sys_root=sysr).read()
    assert r.temp_c == 45.0  # ddrss; the 48 C PMIC zone is not watched
    assert not any(z.type.startswith("pmm") for z in r.watched_zones())


def test_limits_below_gpu_npu_passive_trip_and_declared() -> None:
    assert VENTUNOQ.limits.crit_c < 115.0
    assert VENTUNOQ.provenance.startswith("declared")
    assert "trip" in VENTUNOQ.provenance and "not measured" in VENTUNOQ.provenance
    assert VENTUNOQ.gpu == "devfreq"

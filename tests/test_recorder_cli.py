"""Records, reboot reports, trace replay and the governing CLI verbs."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from heat_governor.cli import main
from heat_governor.platforms import SPARK
from heat_governor.recorder import Recorder, replay, report
from heat_governor.state import write_state

T1S = """\
utc up_s z0_c z1_c nvme_c gpu_c
# START 2026-10-23T09:19:10Z boot_id=a zones=2
09:19:10 100.0 80.0 70.0 50.0 70
09:19:11 101.0 85.0 70.0 50.0 70
09:19:12 102.0 92.0 70.0 50.0 70
09:19:13 103.0 93.5 70.0 50.0 70
09:19:14 104.0 88.0 70.0 50.0 70
# START 2026-10-23T09:30:00Z boot_id=b zones=2
09:30:00 30.0 60.0 60.0 40.0 50
"""


def test_replay_t1s(tmp_path: Path) -> None:
    p = tmp_path / "t1s.log"
    p.write_text(T1S)
    res = replay(p, SPARK)
    assert res["samples"] == 6
    assert res["starts_in_trace"] == 2
    assert res["max_temp_c"] == 93.5
    assert res["seconds_by_level"]["critical"] >= 1
    assert len(res["before_each_restart"]) == 1


def test_report_finds_the_reboot(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, fsync=False)
    t = time.time()
    rec.write({"t": t, "event": "start", "boot_id": "a"})
    for i in range(5):
        rec.write({"t": t + 1 + i, "temp_c": 90.0 + i, "level": "hot", "duty": 0.5})
    rec.write({"t": t + 300, "event": "start", "boot_id": "b"})
    rec.write({"t": t + 301, "event": "start", "boot_id": "b"})  # a daemon restart, same boot
    rec.close()
    with open(next(tmp_path.glob("*.jsonl")), "a") as fh:
        fh.write('{"t": 1, "temp')  # a torn line after a power loss is skipped
    rows = report(tmp_path)
    assert len(rows) == 1
    assert rows[0]["max_temp_c"] == 94.0
    assert rows[0]["gap_s"] == pytest.approx(295, abs=1)


def _run(capsys, *argv):
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_state_and_wait(tmp_path: Path, capsys) -> None:
    p = tmp_path / "s.json"
    write_state(
        p,
        {
            "at": time.time(),
            "stale_after_s": 5,
            "level": "hot",
            "admit": False,
            "concurrency_factor": 0.0,
            "temp_c": 92.0,
        },
    )
    rc, out, _ = _run(capsys, "state", "--source", str(p), "--json")
    assert rc == 0
    assert json.loads(out)["level"] == "hot"
    rc, out, _ = _run(
        capsys, "wait", "--source", str(p), "--timeout", "0.1", "--poll", "0.05", "--json"
    )
    assert rc == 3
    assert json.loads(out)["admit"] is False


def test_cli_replay_and_errors(tmp_path: Path, capsys) -> None:
    p = tmp_path / "t1s.log"
    p.write_text(T1S)
    rc, out, _ = _run(capsys, "replay", str(p), "--platform", "spark", "--json")
    assert rc == 0
    assert json.loads(out)["profile"] == "spark"
    rc, _, err = _run(capsys, "replay", str(tmp_path / "nope"), "--platform", "spark")
    assert rc == 1
    assert "hint:" in err


def test_cli_report_empty(tmp_path: Path, capsys) -> None:
    rc, out, _ = _run(capsys, "report", "--data-dir", str(tmp_path), "--json")
    assert rc == 0
    assert json.loads(out)["reboots"] == []


def test_cli_unit(capsys) -> None:
    rc, out, _ = _run(capsys, "unit", "--", "--apply", "--listen", "127.0.0.1:9476")
    assert rc == 0
    assert "--listen 127.0.0.1:9476" in out
    assert "Restart=always" in out


def test_cli_run_once_without_docker(tmp_path: Path, capsys) -> None:
    rc, out, _ = _run(
        capsys,
        "run",
        "--once",
        "--no-docker",
        "--platform",
        "spark",
        "--json",
        "--state",
        str(tmp_path / "s.json"),
        "--data-dir",
        str(tmp_path / "d"),
    )
    assert rc == 0
    doc = json.loads(out)
    assert doc["dry_run"] is True
    assert doc["level"] in ("cool", "warm", "hot", "critical")


def test_cli_batch_without_docker_is_env_error(tmp_path: Path, capsys) -> None:
    rc, _, err = _run(
        capsys,
        "run",
        "--once",
        "--no-docker",
        "--batch",
        "x",
        "--platform",
        "spark",
        "--data-dir",
        str(tmp_path),
    )
    assert rc == 2
    assert "hint:" in err


# -- every sensor separately ------------------------------------------------------


def test_sensor_stats_per_sensor_and_window(tmp_path: Path) -> None:
    from heat_governor.recorder import sensor_stats

    rec = Recorder(tmp_path, fsync=False)
    t0 = 1_761_235_200.0
    rec.write({"t": t0, "event": "start", "boot_id": "a"})
    for i in range(10):
        rec.write(
            {
                "t": t0 + i,
                "temp_c": 90.0,
                "zones": {"0:acpitz": 85.0 + i, "4:acpitz": 80.0},
                "gpu": {"temp_c": 70.0 + i, "power_w": 40.0, "events": "0x0"},
                "fans": {},
            }
        )
    rec.close()
    res = sensor_stats(tmp_path, since=t0 + 5, above_c=92)
    s = res["sensors"]
    assert s["zone:0:acpitz"]["samples"] == 5
    assert s["zone:0:acpitz"]["max"] == 94.0
    assert s["zone:0:acpitz"]["s_at_or_above"] == 3  # 92, 93, 94
    assert s["zone:4:acpitz"]["max"] == 80.0
    assert s["zone:4:acpitz"]["s_at_or_above"] == 0
    assert s["gpu:temp_c"]["mean"] == 77.0
    assert "s_at_or_above" not in s["gpu:power_w"]
    assert "gpu:events" not in s  # non-numeric


def test_parse_when_forms() -> None:
    from heat_governor.recorder import parse_when

    now = 1_761_235_200.0  # 2025-10-23T16:00:00Z
    assert parse_when("-30m", now=now) == now - 1800
    assert parse_when("1761235200") == now
    assert parse_when("2025-10-23T16:00:00Z") == now
    assert parse_when("16:00", now=now) == now


def test_cli_sensors_and_state_carries_every_sensor(tmp_path: Path, capsys) -> None:
    from heat_governor.daemon import Daemon, Options
    from heat_governor.platforms import SPARK
    from heat_governor.sense import Sensor
    from heat_governor.state import read_state
    from tests.fakes import SPARK_ZONES, make_sys

    sysr = make_sys(tmp_path, SPARK_ZONES)
    opts = Options(state_path=tmp_path / "s.json", data_dir=tmp_path / "d", log=[].append)
    d = Daemon(SPARK, opts, sensor=Sensor(SPARK.zones, sys_root=sysr), docker=None)
    d.tick()
    st = read_state(tmp_path / "s.json")
    assert {f"zone:{i}:acpitz" for i in range(7)} <= set(st["sensors"])
    rc, out, _ = _run(capsys, "sensors", "--data-dir", str(tmp_path / "d"), "--json")
    assert rc == 0
    assert "zone:0:acpitz" in json.loads(out)["sensors"]

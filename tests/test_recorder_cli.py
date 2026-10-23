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
    assert res["samples"] == 6 and res["starts_in_trace"] == 2
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
    assert rows[0]["max_temp_c"] == 94.0 and rows[0]["gap_s"] == pytest.approx(295, abs=1)


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
    assert rc == 0 and json.loads(out)["level"] == "hot"
    rc, out, _ = _run(
        capsys, "wait", "--source", str(p), "--timeout", "0.1", "--poll", "0.05", "--json"
    )
    assert rc == 3 and json.loads(out)["admit"] is False


def test_cli_replay_and_errors(tmp_path: Path, capsys) -> None:
    p = tmp_path / "t1s.log"
    p.write_text(T1S)
    rc, out, _ = _run(capsys, "replay", str(p), "--platform", "spark", "--json")
    assert rc == 0 and json.loads(out)["profile"] == "spark"
    rc, _, err = _run(capsys, "replay", str(tmp_path / "nope"), "--platform", "spark")
    assert rc == 1 and "hint:" in err


def test_cli_report_empty(tmp_path: Path, capsys) -> None:
    rc, out, _ = _run(capsys, "report", "--data-dir", str(tmp_path), "--json")
    assert rc == 0 and json.loads(out)["reboots"] == []


def test_cli_unit(capsys) -> None:
    rc, out, _ = _run(capsys, "unit", "--", "--apply", "--listen", "127.0.0.1:9476")
    assert rc == 0 and "--listen 127.0.0.1:9476" in out and "Restart=always" in out


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
    assert doc["dry_run"] is True and doc["level"] in ("cool", "warm", "hot", "critical")


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
    assert rc == 2 and "hint:" in err

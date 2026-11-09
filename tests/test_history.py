"""The record downsampler: HISTORY_SCRIPT run exactly as the boxes run it (python3 -c)."""

from __future__ import annotations

import ast
import json
import math
import subprocess  # nosec B404
import sys
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from heat_governor import history
from heat_governor.history import HISTORY_SCRIPT, LONG_RANGE_S, build_args, parse_output
from heat_governor.platforms import Profile
from heat_governor.policy import LEVELS

DAY = 86_400
# 2026-11-01 00:00:00 UTC
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()


def rec(t: float, temp: float, level: str = "cool", gpu=None, mem=None, **extra) -> dict:
    r = {
        "t": round(t, 3),
        "temp_c": temp,
        "board_temp_c": temp,
        "effective_c": temp,
        "rate_c_s": 0.0,
        "level": level,
        "duty": 0.0,
        "cpus": None,
        "gpu_mhz": None,
        "source": "board",
        "zones": {"0:cpu": temp},
        "fans": {},
        "gpu": {} if gpu is None else {"temp_c": gpu, "power_w": 10.0},
        "system": {} if mem is None else {"mem:used_pct": mem, "mem:avail_mib": 1000},
    }
    r.update(extra)
    return r


def day_file(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d") + ".jsonl"


def write(records_dir: Path, lines: list) -> None:
    """Append lines (dicts or raw strings) to their UTC day files, in order."""
    records_dir.mkdir(parents=True, exist_ok=True)
    for item in lines:
        if isinstance(item, dict):
            name, text = day_file(item["t"]), json.dumps(item, separators=(",", ":"))
        else:
            name, text = item
        with open(records_dir / name, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def run(state_home: Path, since: float, until: float, max_points: int) -> dict:
    proc = subprocess.run(  # nosec B603 - fixed argv, test-only
        [sys.executable, "-c", HISTORY_SCRIPT, *build_args(since, until, max_points)],
        capture_output=True,
        timeout=60,
        env={"XDG_STATE_HOME": str(state_home), "HOME": str(state_home / "home")},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return parse_output(proc.stdout)


@pytest.fixture
def home(tmp_path):
    return tmp_path / "xdg"


def records(home: Path) -> Path:
    return home / "heat-governor" / "records"


def test_buckets_keep_maxima_most_severe_level_min_cf_and_omit_empty(home):
    rd = records(home)
    write(
        rd,
        [
            {"t": T0 + 1, "event": "start", "boot_id": "x"},
            rec(T0 + 10, 50.0, "cool", gpu=45.0, mem=40.0),
            rec(T0 + 20, 72.0, "warm", gpu=60.0, mem=41.0),
            rec(T0 + 30, 61.0, "cool", gpu=None, mem=39.0),
            # bucket 2 (200..300) empty -> omitted
            rec(T0 + 350, 85.0, "hot", gpu=70.0, mem=90.0),
            rec(T0 + 360, 80.0, "warm", mem=None),
            {"t": T0 + 361, "event": "action", "what": "pause"},
            (day_file(T0), '{"t": 1794199531.8, "temp_c": 99'),  # torn line
            (day_file(T0), "garbage"),
            rec(T0 + 950, 55.0, "critical"),
        ],
    )
    out = run(home, T0, T0 + 1000, 10)
    s = out["samples"]
    assert [round(x["t"] - T0) for x in s] == [30, 360, 950]
    assert s[0] == {
        "t": T0 + 30,
        "temp_c": 72.0,
        "gpu_temp_c": 60.0,
        "level": "warm",
        "concurrency_factor": 0.5,
        "mem_used_pct": 41.0,
    }
    assert s[1]["temp_c"] == 85.0
    assert s[1]["level"] == "hot"
    assert s[1]["concurrency_factor"] == 0.0
    assert s[1]["mem_used_pct"] == 90.0
    assert s[1]["gpu_temp_c"] == 70.0
    assert s[2]["level"] == "critical"
    assert s[2]["gpu_temp_c"] is None
    assert s[2]["mem_used_pct"] is None
    assert out["schema"] == 1
    assert out["files"] == [day_file(T0)]
    assert out["lines_read"] >= 7


def test_only_overlapping_day_files_and_missing_days_are_skipped(home):
    rd = records(home)
    # days 0, 1 and 3 exist; day 2 is missing; a day-5 file must never be opened
    write(rd, [rec(T0 + d * DAY + 3600, 50.0 + d) for d in (0, 1, 3, 5)])
    out = run(home, T0 + DAY / 2, T0 + 3.5 * DAY, 300)
    assert out["files"] == [day_file(T0), day_file(T0 + DAY), day_file(T0 + 3 * DAY)]
    assert [x["temp_c"] for x in out["samples"]] == [51.0, 53.0]


def test_no_records_dir_is_an_empty_answer(home):
    out = run(home, T0, T0 + 3600, 300)
    assert out["samples"] == []
    assert out["files"] == []
    assert out["lines_read"] == 0


def test_long_range_parses_only_every_kth_tick_line(home, monkeypatch):
    rd = records(home)
    span = 3 * DAY  # > 26 h: sampling applies
    step = 2.0
    lines = []
    n = int(span / step)
    for i in range(n):
        t = T0 + i * step
        lines.append(rec(t, 40.0 + (i % 50) * 0.1, "cool", mem=50.0))
    write(rd, lines)
    out = run(home, T0, T0 + span, 300)
    assert out["stride"] > 1
    assert out["parsed"] <= history.MAX_PARSED + 300
    assert out["parsed"] < out["lines_read"]
    assert 250 <= len(out["samples"]) <= 300
    assert all(40.0 <= x["temp_c"] <= 44.9 for x in out["samples"])
    # a short range never samples
    short = run(home, T0, T0 + 3600, 300)
    assert short["stride"] == 1


def test_records_outside_the_range_are_dropped(home):
    rd = records(home)
    write(rd, [rec(T0 + 5, 90.0), rec(T0 + 100, 50.0), rec(T0 + 300, 95.0)])
    out = run(home, T0 + 50, T0 + 200, 10)
    assert [x["temp_c"] for x in out["samples"]] == [50.0]


def test_concurrency_factors_match_the_profile_default():
    default = next(f.default for f in fields(Profile) if f.name == "concurrency")
    assert history.CONCURRENCY == tuple(default)
    assert history.LEVELS == tuple(LEVELS)
    assert f"CF = {tuple(default)!r}" in HISTORY_SCRIPT
    line = next(ln for ln in HISTORY_SCRIPT.splitlines() if ln.startswith("LEVELS = "))
    assert ast.literal_eval(line.split("=", 1)[1].strip()) == tuple(LEVELS)


@pytest.mark.parametrize(
    "args",
    [
        ("1", 2.0, 300),
        (float("nan"), 2.0, 300),
        (1.0, float("inf"), 300),
        (5.0, 1.0, 300),  # until before since
        (-1.0, 2.0, 300),
        (1.0, 1.0 + 9 * DAY, 300),  # longer than the cap
        (1.0, 2.0, 0),
        (1.0, 2.0, 100_000),
        (1.0, 2.0, 1.5),
        (1.0, 2.0, True),
        (True, 2.0, 300),
    ],
)
def test_build_args_refuses_anything_but_sane_numbers(args):
    with pytest.raises(ValueError):
        build_args(*args)


def test_build_args_are_plain_numbers():
    argv = build_args(T0, T0 + 3600.25, 300)
    assert argv == [f"{T0:.3f}", f"{T0 + 3600.25:.3f}", "300"]
    for a in argv:
        assert math.isfinite(float(a))


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b'{"schema":2,"samples":[]}', b'{"schema":1,"samples":{}}'],
)
def test_parse_output_refuses_bad_documents(raw):
    with pytest.raises(ValueError):
        parse_output(raw)


def test_parse_output_drops_malformed_samples():
    raw = json.dumps(
        {
            "schema": 1,
            "samples": [
                {"t": 1.0, "temp_c": 50.0, "level": "cool"},
                {"t": "x"},
                "nope",
                {"t": 2.0, "temp_c": 51.0, "level": "bogus", "mem_used_pct": "x"},
            ],
            "lines_read": 3,
            "files": [],
        }
    ).encode()
    s = parse_output(raw)["samples"]
    assert [x["t"] for x in s] == [1.0, 2.0]
    assert s[1]["level"] is None
    assert s[1]["mem_used_pct"] is None
    assert set(s[0]) == {
        "t",
        "temp_c",
        "gpu_temp_c",
        "level",
        "concurrency_factor",
        "mem_used_pct",
    }


def test_timeouts_and_long_range_threshold():
    assert LONG_RANGE_S == 26 * 3600
    assert history.timeout_for(DAY) == 10.0
    assert history.timeout_for(7 * DAY) == 20.0
    # the script's own deadline stays inside the caller's
    assert "DEADLINE = 18.0 if span > LONG else 9.0" in HISTORY_SCRIPT


def test_script_is_plain_stdlib_and_has_no_single_quotes():
    imports = [ln for ln in HISTORY_SCRIPT.splitlines() if ln.startswith(("import", "from"))]
    assert imports
    assert all(
        set(ln.replace(",", " ").split()[1:]) <= {"json", "os", "sys", "time", "datetime"}
        or ln.startswith("from datetime import")
        for ln in imports
    )
    assert "'" not in HISTORY_SCRIPT  # keeps the shell-quoted remote command readable


def test_run_local_uses_the_same_script(home, monkeypatch):
    write(records(home), [rec(T0 + 10, 66.0, "warm")])
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    out = history.run_local(T0, T0 + 100, 10)
    assert [x["temp_c"] for x in out["samples"]] == [66.0]

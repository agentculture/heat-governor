"""``heat-governor records``: overview and prune (on tmp_path only)."""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from heat_governor import retention
from heat_governor.cli import main
from heat_governor.explain import known_paths


def run(capsys, *argv):
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("HEAT_GOVERNOR_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


def _day(offset: int) -> str:
    d = datetime.now(timezone.utc).date() - timedelta(days=offset)
    return d.strftime("%Y%m%d")


@pytest.fixture
def data(tmp_path) -> Path:
    rec = tmp_path / "data" / "records"
    rec.mkdir(parents=True)
    for off in (0, 1, 2, 3, 20, 30):
        (rec / f"{_day(off)}.jsonl").write_text('{"t":1}\n' * 50)
    with gzip.open(rec / f"{_day(25)}.jsonl.gz", "wb") as fh:
        fh.write(b'{"t":1}\n')
    return tmp_path / "data"


def _listing(d: Path):
    return sorted((p.name, p.stat().st_size) for p in (d / "records").iterdir())


def test_dry_run_changes_nothing_and_matches_real(capsys, data):
    before = _listing(data)
    rc, out, _ = run(capsys, "records", "prune", "--dry-run", "--data-dir", str(data), "--json")
    assert rc == 0
    dry = json.loads(out)
    assert _listing(data) == before
    assert dry["deleted"] and dry["compressed"]
    rc, out, _ = run(capsys, "records", "prune", "--data-dir", str(data), "--json")
    assert rc == 0
    real = json.loads(out)
    assert real["deleted"] == dry["deleted"]
    assert real["compressed"] == dry["compressed"]
    names = {n for n, _ in _listing(data)}
    assert not names & set(real["deleted"])
    for raw in real["compressed"]:
        assert raw not in names and raw + ".gz" in names


def test_json_matches_prune_pass(capsys, data):
    expect = retention.prune_pass(data / "records", __import__("time").time(), 7, dry_run=True)
    rc, out, _ = run(capsys, "records", "prune", "--dry-run", "--data-dir", str(data), "--json")
    assert rc == 0
    got = json.loads(out)
    for key in ("deleted", "compressed", "bytes_freed", "errors", "warnings"):
        assert got[key] == expect[key]
    assert got["dry_run"] is True and got["keep_days"] == 7


def test_text_output_dry_run(capsys, data):
    rc, out, _ = run(capsys, "records", "prune", "--dry-run", "--data-dir", str(data))
    assert rc == 0
    assert f"would delete {_day(30)}.jsonl" in out
    assert f"would compress {_day(3)}.jsonl" in out
    assert "MB" in out


def test_keep_days_from_config_and_override(capsys, data, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("[daemon]\nkeep_days = 40\n")
    rc, out, _ = run(
        capsys, "records", "prune", "--dry-run", "--json", "--config", str(cfg),
        "--data-dir", str(data),
    )  # fmt: skip
    got = json.loads(out)
    assert got["keep_days"] == 40 and got["deleted"] == []
    rc, out, _ = run(
        capsys, "records", "prune", "--dry-run", "--json", "--config", str(cfg),
        "--keep-days", "5", "--data-dir", str(data),
    )  # fmt: skip
    assert json.loads(out)["keep_days"] == 5


@pytest.mark.parametrize("bad", ["0", "-3"])
def test_bad_keep_days_exit_1(capsys, data, bad):
    rc, out, err = run(capsys, "records", "prune", "--keep-days", bad, "--data-dir", str(data))
    assert rc == 1 and out == "" and "keep_days" in err or "keep-days" in err


def test_missing_records_dir_exit_2(capsys, tmp_path):
    rc, out, err = run(capsys, "records", "prune", "--data-dir", str(tmp_path / "nope"))
    assert rc == 2 and out == "" and "error:" in err


def test_records_dir_is_a_file_exit_2(capsys, tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "records").write_text("x")
    rc, _, _ = run(capsys, "records", "prune", "--data-dir", str(tmp_path / "d"))
    assert rc == 2


def test_stale_clock_warning_on_stderr(capsys, tmp_path):
    rec = tmp_path / "d" / "records"
    rec.mkdir(parents=True)
    (rec / f"{_day(40)}.jsonl").write_text("x\n")
    rc, out, err = run(capsys, "records", "prune", "--data-dir", str(tmp_path / "d"))
    assert rc == 0 and (rec / f"{_day(40)}.jsonl").exists()
    assert "warning" in err
    rc, out, _ = run(capsys, "records", "prune", "--json", "--data-dir", str(tmp_path / "d"))
    assert json.loads(out)["warnings"]


def test_per_file_error_exit_2_after_result(capsys, data, monkeypatch):
    real = retention.prune_pass

    def failing(*a, **kw):
        res = real(*a, **kw)
        res["errors"].append("20200101.jsonl: disk says no")
        return res

    monkeypatch.setattr(retention, "prune_pass", failing)
    rc, out, _ = run(capsys, "records", "prune", "--json", "--data-dir", str(data))
    assert rc == 2
    assert json.loads(out)["errors"]


def test_overview_and_bare_noun(capsys, data):
    os.environ["XDG_STATE_HOME"] = str(data.parent / "xdg")
    for argv in (["records"], ["records", "overview"]):
        rc, out, _ = run(capsys, *argv)
        assert rc == 0 and "prune" in out and "keep" in out
    rc, out, _ = run(capsys, "records", "overview", "--data-dir", str(data), "--json")
    assert rc == 0
    doc = json.loads(out)
    assert json.dumps(doc).count("records") >= 1
    text = json.dumps(doc)
    assert "6 raw" in text and "1 gz" in text and str(data / "records") in text


def test_overview_read_only(capsys, data):
    before = _listing(data)
    run(capsys, "records", "overview", "--data-dir", str(data))
    assert _listing(data) == before


def test_explain_and_learn(capsys):
    for p in (("records",), ("records", "overview"), ("records", "prune")):
        assert p in known_paths()
        rc, out, _ = run(capsys, "explain", *p)
        assert rc == 0 and "records" in out
    rc, out, _ = run(capsys, "learn")
    assert rc == 0 and "records" in out
    rc, out, _ = run(capsys, "explain", "memory")
    assert ".local/share" not in out

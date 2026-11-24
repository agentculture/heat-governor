"""Records stored raw and as .jsonl.gz read identically."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from heat_governor import recorder
from heat_governor.platforms import generic_profile

T0 = 1_700_000_000.0


def _rows(boot: str, start: float, n: int = 30) -> list[dict]:
    rows = [{"event": "start", "t": start, "boot_id": boot}]
    rows += [
        {"t": start + i, "temp_c": 50.0 + i * 0.5, "zones": {"cpu": 50.0 + i * 0.5}}
        for i in range(1, n)
    ]
    return rows


def _text(rows: list[dict]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def _store(directory: Path, days: dict[str, list[dict]], gz: set[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for day, rows in days.items():
        if day in gz:
            with gzip.open(directory / f"{day}.jsonl.gz", "wt", encoding="utf-8") as fh:
                fh.write(_text(rows))
        else:
            (directory / f"{day}.jsonl").write_text(_text(rows), encoding="utf-8")


DAYS = {
    "20261110": _rows("boot-a", T0),
    "20261111": _rows("boot-b", T0 + 86400),
    "20261112": _rows("boot-c", T0 + 2 * 86400),
}


def test_iter_records_identical(tmp_path):
    _store(tmp_path / "raw", DAYS, set())
    _store(tmp_path / "gz", DAYS, set(DAYS))
    raw = list(recorder.iter_records(tmp_path / "raw"))
    assert len(raw) == 90
    assert list(recorder.iter_records(tmp_path / "gz")) == raw


def test_mixed_suffix_order_by_day(tmp_path):
    _store(tmp_path / "mix", DAYS, {"20261111"})
    got = [r["boot_id"] for r in recorder.iter_records(tmp_path / "mix") if "boot_id" in r]
    assert got == ["boot-a", "boot-b", "boot-c"]


def test_report_and_stats_identical(tmp_path):
    _store(tmp_path / "raw", DAYS, set())
    _store(tmp_path / "gz", DAYS, set(DAYS))
    rep = recorder.report(tmp_path / "raw")
    assert len(rep) == 2
    assert recorder.report(tmp_path / "gz") == rep
    stats = recorder.sensor_stats(tmp_path / "raw", above_c=55.0)
    assert stats["sensors"]
    assert recorder.sensor_stats(tmp_path / "gz", above_c=55.0) == stats


def test_replay_identical(tmp_path):
    rows = _rows("boot-a", T0, 120)
    raw = tmp_path / "20261110.jsonl"
    raw.write_text(_text(rows), encoding="utf-8")
    gz = tmp_path / "20261110.jsonl.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write(_text(rows))
    prof = generic_profile(tmp_path)
    res = recorder.replay(raw, prof)
    assert res["samples"] > 0
    assert recorder.replay(gz, prof) == res


def test_replay_t1s_log_still_works(tmp_path):
    p = tmp_path / "t1s-x.log"
    p.write_text("", encoding="utf-8")
    assert recorder.replay(p, generic_profile(tmp_path))["samples"] == 0


def test_open_day_text_and_errors(tmp_path):
    gz = tmp_path / "d.jsonl.gz"
    with gzip.open(gz, "wb") as fh:
        fh.write(b'{"a": "\xff"}\n')
    with recorder.open_day(gz, errors="replace") as fh:
        assert "�" in fh.read()


def test_truncated_gz_keeps_earlier_rows(tmp_path):
    _store(tmp_path, {"20261110": DAYS["20261110"]}, set())
    good = gzip.compress(_text(DAYS["20261111"]).encode())
    (tmp_path / "20261111.jsonl.gz").write_bytes(good[: len(good) // 2])
    _store(tmp_path, {"20261112": DAYS["20261112"]}, set())
    rows = list(recorder.iter_records(tmp_path))
    ids = [r["boot_id"] for r in rows if "boot_id" in r]
    assert ids[0] == "boot-a"
    assert ids[-1] == "boot-c"
    assert len(rows) >= 60


def test_a_corrupt_gz_day_is_skipped_not_fatal(tmp_path):
    """Review of PR 23, S1: a bad magic number or a CRC mismatch (gzip.BadGzipFile, an
    OSError) loses that day's unread rows, never the rest of the archive."""
    d = tmp_path / "records"
    d.mkdir()
    (d / "20261120.jsonl").write_text('{"t":1}\n')
    (d / "20261121.jsonl.gz").write_bytes(b"notgzip")
    good = gzip.compress(b'{"t":3}\n')
    bad_crc = good[:-8] + bytes([good[-8] ^ 0xFF]) + good[-7:]
    (d / "20261122.jsonl.gz").write_bytes(bad_crc)
    (d / "20261123.jsonl").write_text('{"t":4}\n')
    rows = [r["t"] for r in recorder.iter_records(d)]
    assert 1 in rows
    assert 4 in rows

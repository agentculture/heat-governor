"""Record retention (heat_governor.retention): every test runs on tmp_path with a fake clock."""

from __future__ import annotations

import errno
import gzip
import os
import shutil
import tracemalloc
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest

from heat_governor import retention
from heat_governor.retention import CHUNK, compress_day, gz_matches_raw, prune_pass

# fake clock: 2026-11-25 12:00:00 UTC
NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc).timestamp()
TODAY = date(2026, 11, 25)
RESULT_KEYS = {"deleted", "compressed", "bytes_freed", "errors", "warnings"}


def stem(d: date) -> str:
    return d.strftime("%Y%m%d")


def body(d: date, lines: int = 200) -> bytes:
    return b"".join(
        b'{"t":%d,"day":"%s","temp_c":%d}\n' % (i, stem(d).encode(), 40 + i % 30)
        for i in range(lines)
    )


def write_raw(records: Path, d: date, lines: int = 200) -> Path:
    path = records / f"{stem(d)}.jsonl"
    path.write_bytes(body(d, lines))
    return path


def write_gz(records: Path, d: date) -> Path:
    path = records / f"{stem(d)}.jsonl.gz"
    path.write_bytes(gzip.compress(body(d)))
    return path


def snapshot(root: Path) -> dict:
    """Every path under root (not following links) -> (bytes or link target, mtime_ns)."""
    out = {}
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        if path.is_symlink():
            out[str(path)] = ("link", os.readlink(path), st.st_mtime_ns)
        elif path.is_file():
            out[str(path)] = ("file", path.read_bytes(), st.st_mtime_ns)
        else:
            out[str(path)] = ("dir", None, None)
    return out


@pytest.fixture
def records(tmp_path) -> Path:
    d = tmp_path / "data" / "records"
    d.mkdir(parents=True)
    return d


def names(records: Path) -> set[str]:
    return {p.name for p in records.iterdir()}


# -- acceptance: 10 day files, keep_days 7 ------------------------------------


def test_ten_days_keep_seven_leaves_eight_files(records):
    for back in range(10):
        write_raw(records, TODAY - timedelta(days=back))
    result = prune_pass(records, NOW, 7)

    assert set(result) == RESULT_KEYS
    assert result["errors"] == [] and result["warnings"] == []
    left = names(records)
    assert len(left) == 8
    assert {"20261125.jsonl", "20261124.jsonl"} <= left
    gz = sorted(n for n in left if n.endswith(".jsonl.gz"))
    assert gz == [f"{stem(TODAY - timedelta(days=b))}.jsonl.gz" for b in range(7, 1, -1)]
    assert sorted(result["deleted"]) == ["20261116.jsonl", "20261117.jsonl"]
    assert len(result["compressed"]) == 6
    for name in gz:
        day = datetime.strptime(name[:8], "%Y%m%d").date()
        assert gzip.decompress((records / name).read_bytes()) == body(day)
    assert result["bytes_freed"] > 0


# -- o1: the window, for keep_days 1, 7 and 30 ---------------------------------


@pytest.mark.parametrize("keep_days", [1, 7, 30])
def test_window_boundary_raw_and_gz(records, keep_days):
    span = keep_days + 4
    for back in range(span):
        d = TODAY - timedelta(days=back)
        # older days already gzipped, the rest raw; one old day in both forms
        write_gz(records, d) if back % 2 else write_raw(records, d)
    oldest = TODAY - timedelta(days=span - 1)
    write_raw(records, oldest)
    write_gz(records, oldest)

    result = prune_pass(records, NOW, keep_days)

    assert result["errors"] == []
    left_days = {n[:8] for n in names(records)}
    expected = {stem(TODAY - timedelta(days=b)) for b in range(keep_days + 1)}
    assert left_days == expected
    cutoff = TODAY - timedelta(days=keep_days)
    for name in result["deleted"]:
        assert datetime.strptime(name[:8], "%Y%m%d").date() < cutoff
    assert f"{stem(oldest)}.jsonl" in result["deleted"]
    assert f"{stem(oldest)}.jsonl.gz" in result["deleted"]


# -- o2: compress_day ------------------------------------------------------------


def test_compress_day_roundtrip_and_raw_removed(records):
    raw = write_raw(records, date(2026, 11, 20), lines=5000)
    original = raw.read_bytes()
    os.chmod(raw, 0o640)
    size = compress_day(raw)
    gz = records / "20261120.jsonl.gz"
    assert not raw.exists()
    assert gz.stat().st_size == size
    assert gzip.decompress(gz.read_bytes()) == original
    assert gz.stat().st_mode & 0o777 == 0o640
    assert names(records) == {"20261120.jsonl.gz"}


def test_compress_day_order_fsync_then_rename_then_unlink(records, monkeypatch):
    raw = write_raw(records, date(2026, 11, 20))
    calls = []
    real_fsync, real_replace, real_unlink = os.fsync, os.replace, os.unlink

    def fsync(fd):
        calls.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
        return real_fsync(fd)

    def replace(src, dst, **kw):
        calls.append(("replace", str(src), str(dst)))
        return real_replace(src, dst, **kw)

    def unlink(path, **kw):
        calls.append(("unlink", str(path)))
        return real_unlink(path, **kw)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)
    compress_day(raw, pid=4242)

    tmp = str(records / "20261120.jsonl.gz.4242.tmp")
    kinds = [c[0] for c in calls]
    assert kinds == ["fsync", "replace", "fsync", "unlink"]
    assert calls[0][1] == tmp
    assert calls[1] == ("replace", tmp, str(records / "20261120.jsonl.gz"))
    assert calls[2][1] == str(records)  # the directory entry is durable before the unlink
    assert calls[3] == ("unlink", str(raw))


@pytest.mark.parametrize("stage", ["write", "fsync", "rename"])
def test_compress_day_failure_before_rename_keeps_raw(records, monkeypatch, stage):
    raw = write_raw(records, date(2026, 11, 20), lines=3000)
    original = raw.read_bytes()
    mtime = raw.stat().st_mtime_ns

    def boom(*a, **k):
        raise OSError(errno.EIO, f"injected at {stage}")

    if stage == "write":
        real_compressobj = retention.zlib.compressobj

        class Failing:
            def __init__(self, *a):
                self._c = real_compressobj(*a)
                self._n = 0

            def compress(self, data):
                self._n += 1
                if self._n > 1:
                    boom()
                return self._c.compress(data)

            def flush(self):
                boom()

        monkeypatch.setattr(retention.zlib, "compressobj", Failing)
    elif stage == "fsync":
        monkeypatch.setattr(os, "fsync", boom)
    else:
        monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="injected"):
        compress_day(raw)
    assert raw.read_bytes() == original
    assert raw.stat().st_mtime_ns == mtime
    assert names(records) == {"20261120.jsonl"}  # no .gz, no leftover .tmp


def test_compress_day_rejects_a_non_raw_name(records):
    gz = write_gz(records, date(2026, 11, 20))
    with pytest.raises(ValueError):
        compress_day(gz)


def test_compress_day_missing_raw_returns_none(records):
    assert compress_day(records / "20261120.jsonl") is None
    assert names(records) == set()


# -- o4: today and yesterday, around a fake midnight; keep_days < 1 ------------


@pytest.mark.parametrize("keep_days", [1, 7])
@pytest.mark.parametrize("offset_s", [-1, 0, 1])
def test_today_and_yesterday_never_touched_around_midnight(records, keep_days, offset_s):
    midnight = datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp()
    now = midnight + offset_s
    today = datetime.fromtimestamp(now, timezone.utc).date()
    for back in range(keep_days + 3):
        write_raw(records, today - timedelta(days=back))
    protected = {
        f"{stem(today)}.jsonl": (records / f"{stem(today)}.jsonl").read_bytes(),
        f"{stem(today - timedelta(days=1))}.jsonl": (
            records / f"{stem(today - timedelta(days=1))}.jsonl"
        ).read_bytes(),
    }
    result = prune_pass(records, now, keep_days)
    for name, data in protected.items():
        assert (records / name).read_bytes() == data
        assert name not in result["deleted"] and name not in result["compressed"]


@pytest.mark.parametrize("bad", [0, -1, -7, True, 1.5, "7"])
def test_keep_days_below_one_is_refused(records, bad):
    write_raw(records, TODAY - timedelta(days=30))
    with pytest.raises(ValueError):
        prune_pass(records, NOW, bad)
    assert names(records) == {f"{stem(TODAY - timedelta(days=30))}.jsonl"}


def test_records_dir_must_be_a_directory(tmp_path):
    with pytest.raises(NotADirectoryError):
        prune_pass(tmp_path / "missing", NOW, 7)
    (tmp_path / "file").write_text("x")
    with pytest.raises(NotADirectoryError):
        prune_pass(tmp_path / "file", NOW, 7)


# -- o5: siblings in the data dir --------------------------------------------------


def test_data_dir_siblings_untouched(tmp_path):
    data = tmp_path / "data"
    rec = data / "records"
    rec.mkdir(parents=True)
    (data / "boundaries-audit.jsonl").write_text('{"op":"add"}\n')
    (data / "boundaries-applied.json").write_text("{}")
    (data / "adaptive.json").write_text('{"cap":80}')
    (data / "ledger.json").write_text("[]")
    (data / "spool").mkdir()
    (data / "spool" / "20261101.jsonl").write_text("spooled\n")
    (data / "ssh").mkdir()
    (data / "ssh" / "known_hosts").write_text("host key\n")
    (data / "20261101.jsonl").write_text("a day-named file outside records/\n")
    old = NOW - 60 * 86_400
    for path in data.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))
    for back in range(12):
        write_raw(rec, TODAY - timedelta(days=back))
    before = {k: v for k, v in snapshot(data).items() if "/records" not in k}

    result = prune_pass(rec, NOW, 7)

    assert result["deleted"] and result["compressed"]
    after = {k: v for k, v in snapshot(data).items() if "/records" not in k}
    assert after == before


# -- o9: crashes, stray temp files, other names --------------------------------------


def test_crash_leftover_tmp_removed_only_after_an_hour(records):
    for back in range(3):
        write_raw(records, TODAY - timedelta(days=back))
    old_tmp = records / "20261123.jsonl.gz.111.tmp"
    old_tmp.write_bytes(b"\x1f\x8b partial")
    os.utime(old_tmp, (NOW - 3601, NOW - 3601))
    fresh_tmp = records / "20261123.jsonl.gz.222.tmp"
    fresh_tmp.write_bytes(b"\x1f\x8b in progress")
    os.utime(fresh_tmp, (NOW - 3599, NOW - 3599))

    result = prune_pass(records, NOW, 7)

    assert old_tmp.name in result["deleted"]
    assert not old_tmp.exists()
    assert fresh_tmp.read_bytes() == b"\x1f\x8b in progress"
    assert gzip.decompress((records / "20261123.jsonl.gz").read_bytes()) == body(
        TODAY - timedelta(days=2)
    )


def test_other_names_never_removed_even_when_old(records):
    for back in range(3):
        write_raw(records, TODAY - timedelta(days=back))
    others = [
        "notes.txt",
        "20261101.jsonl.bak",
        "20261101.jsonl.gz.tmp",
        "20261101.jsonl.gz.abc.tmp",
        "x20261101.jsonl",
        "2026110.jsonl",
        "20261101.json",
        "governor.log",
        "20261399.jsonl",  # matches the pattern but is no date
    ]
    old = NOW - 400 * 86_400
    for name in others:
        (records / name).write_text(name)
        os.utime(records / name, (old, old))

    result = prune_pass(records, NOW, 1)

    for name in others:
        assert (records / name).read_text() == name
    assert any("20261399.jsonl" in w for w in result["warnings"])


def test_crash_between_rename_and_unlink_is_finished_by_next_pass(records):
    for back in range(2):
        write_raw(records, TODAY - timedelta(days=back))
    d = TODAY - timedelta(days=3)
    raw = write_raw(records, d)
    raw_size = raw.stat().st_size
    write_gz(records, d)  # the state a crash after os.replace leaves

    result = prune_pass(records, NOW, 7)

    assert not raw.exists()
    assert gzip.decompress((records / f"{stem(d)}.jsonl.gz").read_bytes()) == body(d)
    assert result["compressed"] == [raw.name]
    assert result["bytes_freed"] == raw_size


def test_raw_and_mismatched_gz_both_left_with_warning(records):
    for back in range(2):
        write_raw(records, TODAY - timedelta(days=back))
    d = TODAY - timedelta(days=3)
    raw = write_raw(records, d)
    gz = records / f"{stem(d)}.jsonl.gz"
    full = gzip.compress(body(d))
    gz.write_bytes(full[: len(full) // 2])  # truncated

    result = prune_pass(records, NOW, 7)

    assert raw.read_bytes() == body(d)
    assert gz.read_bytes() == full[: len(full) // 2]
    assert raw.name not in result["compressed"]
    assert any("does not match" in w for w in result["warnings"])


def test_gz_matches_raw_cases(records):
    d = date(2026, 11, 20)
    raw = write_raw(records, d, lines=20_000)
    gz = records / "x.gz"
    gz.write_bytes(gzip.compress(raw.read_bytes()))
    assert gz_matches_raw(raw, gz)
    gz.write_bytes(gzip.compress(raw.read_bytes() + b"extra"))
    assert not gz_matches_raw(raw, gz)
    gz.write_bytes(gzip.compress(raw.read_bytes()[:-1]))
    assert not gz_matches_raw(raw, gz)
    gz.write_bytes(gzip.compress(raw.read_bytes()) + b"trailing")
    assert not gz_matches_raw(raw, gz)
    gz.write_bytes(b"not gzip at all")
    assert not gz_matches_raw(raw, gz)
    assert not gz_matches_raw(raw, records / "missing.gz")


# -- o10 (streaming): memory does not grow with the file -------------------------------


def test_compress_streams_in_one_mib_chunks(records, monkeypatch):
    raw = records / "20261120.jsonl"
    raw.write_bytes(os.urandom(12 * CHUNK))  # incompressible: output ~ input size
    original = raw.read_bytes()
    reads = []
    real_open_ro = retention._open_ro

    def spying_open_ro(path):
        fh = real_open_ro(path)
        real_read = fh.read

        class Spy:
            def __getattr__(self, name):
                return getattr(fh, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fh.close()

            def read(self, n=-1):
                reads.append(n)
                return real_read(n)

        return Spy()

    monkeypatch.setattr(retention, "_open_ro", spying_open_ro)
    tracemalloc.start()
    try:
        compress_day(raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert reads and all(0 < n <= CHUNK for n in reads)
    assert peak < 4 * CHUNK  # a 12 MiB file, a few MiB at most in flight
    del original


# -- o11: two concurrent passes ----------------------------------------------------------


def _populate(records: Path, days: int = 6) -> None:
    for back in range(days):
        write_raw(records, TODAY - timedelta(days=back))


def _assert_one_gz_per_day(records: Path, days: int = 6) -> None:
    for back in range(2, days):
        d = TODAY - timedelta(days=back)
        assert not (records / f"{stem(d)}.jsonl").exists()
        assert gzip.decompress((records / f"{stem(d)}.jsonl.gz").read_bytes()) == body(d)
    assert not [n for n in names(records) if n.endswith(".tmp")]


def test_concurrent_pass_runs_before_each_compress(records):
    _populate(records)
    other_results = []

    def interleaved(path):
        other_results.append(prune_pass(records, NOW, 7, compress=partial(compress_day, pid=2)))
        return compress_day(path, pid=1)

    result = prune_pass(records, NOW, 7, compress=interleaved)

    _assert_one_gz_per_day(records)
    assert result["errors"] == [] and all(r["errors"] == [] for r in other_results)


def test_concurrent_pass_runs_between_rename_and_unlink(records, monkeypatch):
    _populate(records)
    real_replace = os.replace
    state = {"inside": False, "other": []}

    def replace(src, dst, **kw):
        real_replace(src, dst, **kw)
        if not state["inside"]:
            state["inside"] = True
            try:
                state["other"].append(
                    prune_pass(records, NOW, 7, compress=partial(compress_day, pid=2))
                )
            finally:
                state["inside"] = False

    monkeypatch.setattr(os, "replace", replace)
    result = prune_pass(records, NOW, 7, compress=partial(compress_day, pid=1))

    _assert_one_gz_per_day(records)
    assert result["errors"] == []
    assert all(r["errors"] == [] for r in state["other"])


def test_concurrent_pass_keeps_the_other_pass_in_progress_tmp(records):
    _populate(records)
    seen = []

    def compress_while_other_writes(path):
        theirs = records / f"{path.name}.gz.999.tmp"
        theirs.write_bytes(b"in progress")
        os.utime(theirs, (NOW + 60, NOW + 60))  # fresh: the other pass is writing it now
        prune_pass(records, NOW + 120, 7, compress=lambda p: None)
        seen.append(theirs.exists())
        theirs.unlink()
        return compress_day(path)

    prune_pass(records, NOW, 7, compress=compress_while_other_writes)
    assert seen and all(seen)
    _assert_one_gz_per_day(records)


# -- o12: ordering and error handling ----------------------------------------------------


def test_expired_days_deleted_before_any_compression(records):
    for back in range(12):
        write_raw(records, TODAY - timedelta(days=back))
    expired = [records / f"{stem(TODAY - timedelta(days=b))}.jsonl" for b in range(8, 12)]
    checked = []

    def compress(path):
        checked.append(all(not p.exists() for p in expired))
        return compress_day(path)

    prune_pass(records, NOW, 7, compress=compress)
    assert checked and all(checked)


def test_enospc_keeps_raw_records_error_and_continues(records):
    _populate(records)
    victim = TODAY - timedelta(days=3)
    victim_path = records / f"{stem(victim)}.jsonl"
    original = victim_path.read_bytes()

    def compress(path):
        if path == victim_path:
            raise OSError(errno.ENOSPC, "No space left on device")
        return compress_day(path)

    result = prune_pass(records, NOW, 7, compress=compress)

    assert victim_path.read_bytes() == original
    assert len(result["errors"]) == 1 and victim_path.name in result["errors"][0]
    assert "No space left" in result["errors"][0]
    assert victim_path.name not in result["compressed"]
    for back in (2, 4, 5):
        assert (records / f"{stem(TODAY - timedelta(days=back))}.jsonl.gz").exists()


def test_enospc_inside_the_real_compressor(records, monkeypatch):
    _populate(records, days=4)
    real_fsync = os.fsync
    victim = f"{stem(TODAY - timedelta(days=2))}.jsonl.gz"

    def fsync(fd):
        if os.readlink(f"/proc/self/fd/{fd}").startswith(str(records / victim)):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    result = prune_pass(records, NOW, 7)

    assert (records / victim[:-3]).read_bytes() == body(TODAY - timedelta(days=2))
    assert not (records / victim).exists()
    assert not [n for n in names(records) if n.endswith(".tmp")]
    assert len(result["errors"]) == 1
    assert (records / f"{stem(TODAY - timedelta(days=3))}.jsonl.gz").exists()


def test_delete_failure_is_recorded_and_pass_continues(records, monkeypatch):
    for back in range(10):
        write_raw(records, TODAY - timedelta(days=back))
    stuck = records / f"{stem(TODAY - timedelta(days=9))}.jsonl"
    real_unlink = os.unlink

    def unlink(path, **kw):
        if Path(path) == stuck:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_unlink(path, **kw)

    monkeypatch.setattr(os, "unlink", unlink)
    result = prune_pass(records, NOW, 7)

    assert stuck.exists()
    assert any(stuck.name in e for e in result["errors"])
    assert result["deleted"] == [f"{stem(TODAY - timedelta(days=8))}.jsonl"]
    assert len(result["compressed"]) == 6


# -- o14: clock sanity -------------------------------------------------------------------------


def test_clock_a_year_ahead_deletes_nothing(records):
    for back in range(10):
        write_raw(records, TODAY - timedelta(days=back))
    stale = records / f"{stem(TODAY - timedelta(days=2))}.jsonl.gz.7.tmp"
    stale.write_bytes(b"x")
    before = snapshot(records)

    result = prune_pass(records, NOW + 365 * 86_400, 7)

    assert snapshot(records) == before
    assert result["deleted"] == [] and result["compressed"] == []
    assert any("clock" in w for w in result["warnings"])


def test_future_dated_day_file_deletes_nothing(records):
    for back in range(10):
        write_raw(records, TODAY - timedelta(days=back))
    write_raw(records, TODAY + timedelta(days=1))
    before = snapshot(records)

    result = prune_pass(records, NOW, 7)

    assert snapshot(records) == before
    assert result["deleted"] == [] and result["compressed"] == []
    assert any("clock" in w and "after today" in w for w in result["warnings"])


def test_newest_file_yesterday_is_not_a_clock_problem(records):
    for back in range(1, 11):
        write_raw(records, TODAY - timedelta(days=back))
    result = prune_pass(records, NOW, 7)
    assert result["warnings"] == []
    assert len(result["deleted"]) == 3


def test_empty_records_dir(records):
    assert prune_pass(records, NOW, 7) == {
        "deleted": [],
        "compressed": [],
        "bytes_freed": 0,
        "errors": [],
        "warnings": [],
    }


# -- o15: file selection -----------------------------------------------------------------------


def test_symlinks_not_followed_and_subdirs_not_entered(tmp_path, records):
    for back in range(3):
        write_raw(records, TODAY - timedelta(days=back))
    outside = tmp_path / "outside"
    outside.mkdir()
    old_target = outside / "precious-old.jsonl"
    old_target.write_bytes(b"old target\n")
    mid_target = outside / "precious-mid.jsonl"
    mid_target.write_bytes(b"mid target\n")
    expired_link = records / f"{stem(TODAY - timedelta(days=30))}.jsonl"
    expired_link.symlink_to(old_target)
    compressible_link = records / f"{stem(TODAY - timedelta(days=3))}.jsonl"
    compressible_link.symlink_to(mid_target)
    gz_link = records / f"{stem(TODAY - timedelta(days=40))}.jsonl.gz"
    gz_link.symlink_to(old_target)
    sub = records / "archive"
    sub.mkdir()
    write_raw(sub, TODAY - timedelta(days=50))
    dir_named_like_day = records / f"{stem(TODAY - timedelta(days=60))}.jsonl"
    dir_named_like_day.mkdir()
    before_outside = snapshot(outside)
    before_sub = snapshot(sub)

    result = prune_pass(records, NOW, 7)

    assert snapshot(outside) == before_outside
    assert snapshot(sub) == before_sub
    assert expired_link.is_symlink() and compressible_link.is_symlink() and gz_link.is_symlink()
    assert dir_named_like_day.is_dir()
    touched = result["deleted"] + result["compressed"]
    for p in (expired_link, compressible_link, gz_link, dir_named_like_day):
        assert p.name not in touched
    assert not (records / f"{compressible_link.name}.gz").exists()


def test_compress_day_refuses_a_symlink(tmp_path, records):
    target = tmp_path / "target.jsonl"
    target.write_bytes(b"target\n")
    link = records / "20261120.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        compress_day(link)
    assert target.read_bytes() == b"target\n"
    assert names(records) == {"20261120.jsonl"}


def test_compress_day_never_overwrites_an_existing_tmp(records):
    raw = write_raw(records, date(2026, 11, 20))
    theirs = records / "20261120.jsonl.gz.5.tmp"
    theirs.write_bytes(b"someone else's")
    with pytest.raises(FileExistsError):
        compress_day(raw, pid=5)
    assert theirs.read_bytes() == b"someone else's"
    assert raw.read_bytes() == body(date(2026, 11, 20))


# -- dry run -------------------------------------------------------------------------------------


def _dry_run_fixture(records: Path) -> None:
    for back in range(12):
        write_raw(records, TODAY - timedelta(days=back))
    write_gz(records, TODAY - timedelta(days=9))
    d = TODAY - timedelta(days=4)
    write_gz(records, d)  # raw + matching gz: finished on the real pass
    stale = records / f"{stem(TODAY - timedelta(days=3))}.jsonl.gz.77.tmp"
    stale.write_bytes(b"partial")
    os.utime(stale, (NOW - 7200, NOW - 7200))


def test_dry_run_changes_nothing_and_matches_real_pass(tmp_path, records):
    _dry_run_fixture(records)
    twin = tmp_path / "twin"
    shutil.copytree(records, twin)
    before = snapshot(records)

    planned = prune_pass(records, NOW, 7, dry_run=True)

    assert snapshot(records) == before
    assert set(planned) == RESULT_KEYS
    done = prune_pass(twin, NOW, 7)
    assert done["errors"] == [] and planned["errors"] == []
    assert planned["deleted"] == done["deleted"]
    assert planned["compressed"] == done["compressed"]
    assert planned["warnings"] == done["warnings"]
    # dry-run bytes_freed is a lower bound: deletions (and finished pairs) only
    assert 0 < planned["bytes_freed"] <= done["bytes_freed"]


def test_dry_run_never_calls_the_compressor(records):
    _populate(records)

    def compress(path):
        raise AssertionError("dry run must not compress")

    planned = prune_pass(records, NOW, 7, dry_run=True, compress=compress)
    assert len(planned["compressed"]) == 4

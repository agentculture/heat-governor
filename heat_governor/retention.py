"""Record retention: delete day files past the window, gzip the older ones in place.

The daemon writes one ``YYYYMMDD.jsonl`` per UTC day into ``<data_dir>/records``
(:class:`~heat_governor.recorder.Recorder`) and, before this module, nothing ever
removed them. :func:`prune_pass` keeps today plus ``keep_days`` complete earlier
days: today's and yesterday's files stay raw, older days in the window become
``YYYYMMDD.jsonl.gz`` and days before the window are deleted.

Safety rules, in the order a pass applies them:

1. **Clock sanity.** If the newest day file is more than one day older than
   today's UTC date (the clock jumped forward) or any day file is dated after
   today (the clock jumped back), the pass changes nothing and returns a warning.
2. **Delete expired days first**, raw or gzipped, so a nearly full disk gets
   space back before compression needs room for a ``.gz``.
3. **Remove stray temp files** (``YYYYMMDD.jsonl.gz.PID.tmp``) only once they are
   older than one hour, so a concurrent pass's in-progress file survives.
4. **Compress** raw days older than yesterday. A failed compress leaves the raw
   day intact, is recorded in ``errors`` and does not stop the rest of the pass.

Only regular files directly inside ``records_dir`` whose names match exactly are
ever touched: symlinks are not followed (``lstat`` / ``O_NOFOLLOW``),
subdirectories are not entered, and no other name is ever removed.

:func:`compress_day` streams the raw file through ``zlib`` in 1 MiB chunks
(gzip format, level 6) into a per-process temp file, fsyncs it, renames it over
``YYYYMMDD.jsonl.gz`` and only then unlinks the raw file, so a crash leaves
either the raw day or a complete ``.gz`` (plus at most one temp file).
"""

from __future__ import annotations

import os
import re
import stat
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Callable

# \Z, not $ ($ also matches before a trailing newline); ASCII digits only
DAY_RE = re.compile(r"^(\d{8})\.jsonl(\.gz)?\Z", re.ASCII)
TMP_RE = re.compile(r"^(\d{8})\.jsonl\.gz\.(\d+)\.tmp\Z", re.ASCII)
CHUNK = 1 << 20  # 1 MiB: peak extra memory does not grow with the day's size
TMP_MAX_AGE_S = 3600.0
MAX_KEEP_DAYS = 3650  # ten years; far larger values overflow the date arithmetic
GZIP_LEVEL = 6
GZIP_WBITS = 31  # 16 + 15: gzip container, 32 KiB window

Compressor = Callable[[Path], "int | None"]


@dataclass
class _Day:
    day: date
    raw: tuple[str, int] | None = None  # (file name, size in bytes)
    gz: tuple[str, int] | None = None


@dataclass
class _Scan:
    days: dict[date, _Day]
    tmps: list[tuple[str, float, int]]  # (file name, mtime, size)


# -- low-level file helpers ---------------------------------------------------


def _open_ro(path: Path) -> BinaryIO:
    """Open a regular file for reading without following a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _unlink_quiet(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_gz(src: BinaryIO, tmp_path: Path, gz_path: Path) -> int:
    """Stream ``src`` into ``tmp_path`` as gzip, fsync, rename onto ``gz_path``.

    The temp file is created exclusively, so another process's file of the same
    name is never truncated or removed; on any failure after creation the temp
    file is removed and the exception propagates.
    """
    st = os.fstat(src.fileno())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(tmp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), stat.S_IMODE(st.st_mode))
            comp = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, GZIP_WBITS)
            while chunk := src.read(CHUNK):
                out.write(comp.compress(chunk))
            out.write(comp.flush())
            out.flush()
            os.utime(out.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
            os.fsync(out.fileno())
            size = out.tell()
        os.replace(tmp_path, gz_path)
    except BaseException:
        _unlink_quiet(tmp_path)
        raise
    return size


def compress_day(path: Path | str, *, pid: int | None = None) -> int | None:
    """Gzip one raw day file in place; return the ``.gz`` size in bytes.

    Writes ``YYYYMMDD.jsonl.gz.<pid>.tmp`` (``pid`` defaults to
    :func:`os.getpid`), fsyncs it, renames it to ``YYYYMMDD.jsonl.gz``, fsyncs the
    directory, then unlinks the raw file (tolerating it already being gone).
    Returns ``None`` when the raw file is already gone (another pass finished it).
    A failure before the rename leaves the raw file untouched and no temp file.
    """
    path = Path(path)
    match = DAY_RE.match(path.name)
    if not match or match.group(2):
        raise ValueError(f"not a raw day file name: {path.name}")
    gz_path = path.with_name(path.name + ".gz")
    tmp_path = path.with_name(f"{path.name}.gz.{pid if pid is not None else os.getpid()}.tmp")
    try:
        src = _open_ro(path)
    except FileNotFoundError:
        return None
    with src:
        size = _write_gz(src, tmp_path, gz_path)
    _fsync_dir(path.parent)
    _unlink_quiet(path)
    return size


def gz_matches_raw(raw_path: Path, gz_path: Path) -> bool:
    """True when ``gz_path`` is one complete gzip member whose content is ``raw_path``.

    Streams both files with bounded memory (at most ``CHUNK`` decompressed bytes
    at a time). Any read or decode error counts as "does not match".
    """
    try:
        with _open_ro(raw_path) as raw, _open_ro(gz_path) as gz:
            return _streams_match(raw, gz)
    except (OSError, zlib.error):
        return False


def _streams_match(raw: BinaryIO, gz: BinaryIO) -> bool:
    dec = zlib.decompressobj(GZIP_WBITS)
    while not dec.eof:
        data = dec.unconsumed_tail or gz.read(CHUNK)
        if not data:
            return False  # truncated .gz
        out = dec.decompress(data, CHUNK)
        if raw.read(len(out)) != out:
            return False
    return not dec.unused_data and not gz.read(1) and not raw.read(1)


# -- the pass ------------------------------------------------------------------


def _new_result() -> dict:
    return {"deleted": [], "compressed": [], "bytes_freed": 0, "errors": [], "warnings": []}


def _check_args(records_dir: Path, keep_days: int) -> None:
    if isinstance(keep_days, bool) or not isinstance(keep_days, int):
        raise ValueError(f"keep_days must be an integer >= 1, got {keep_days!r}")
    if keep_days < 1:
        raise ValueError(
            f"keep_days must be >= 1 (today and yesterday are always kept), got {keep_days}"
        )
    if keep_days > MAX_KEEP_DAYS:
        raise ValueError(f"keep_days must be <= {MAX_KEEP_DAYS}, got {keep_days}")
    if not records_dir.is_dir():
        raise NotADirectoryError(f"records directory not found: {records_dir}")


def _parse_day(stem: str) -> date | None:
    try:
        return datetime.strptime(stem, "%Y%m%d").date()
    except ValueError:
        return None


def _scan(records_dir: Path, result: dict) -> _Scan:
    scan = _Scan(days={}, tmps=[])
    with os.scandir(records_dir) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue  # symlinks, directories, sockets: never touched
            st = entry.stat(follow_symlinks=False)
            _classify(entry.name, st, scan, result)
    return scan


def _classify(name: str, st: os.stat_result, scan: _Scan, result: dict) -> None:
    tmp = TMP_RE.match(name)
    if tmp:
        scan.tmps.append((name, st.st_mtime, st.st_size))
        return
    match = DAY_RE.match(name)
    if not match:
        return
    day = _parse_day(match.group(1))
    if day is None:
        result["warnings"].append(f"{name}: not a valid date, left alone")
        return
    entry = scan.days.setdefault(day, _Day(day))
    if match.group(2):
        entry.gz = (name, st.st_size)
    else:
        entry.raw = (name, st.st_size)


def _clock_problem(days: dict[date, _Day], today: date) -> str | None:
    if not days:
        return None
    newest = max(days)
    if newest > today:
        return (
            f"clock check: day file {newest:%Y%m%d} is dated after today ({today:%Y%m%d} UTC); "
            "nothing deleted or compressed"
        )
    if newest < today - timedelta(days=1):
        return (
            f"clock check: newest day file {newest:%Y%m%d} is more than 1 day older than today "
            f"({today:%Y%m%d} UTC); nothing deleted or compressed"
        )
    return None


def _gap_floor(days, keep_days: int) -> date | None:
    """The oldest day after the newest hole longer than ``keep_days`` between day files.

    A forward clock jump and a long downtime look the same from the files alone: real days,
    a hole, then the days written after it. Days before such a hole are never deleted by an
    ordinary pass (``allow_gap`` lifts it), because after a jump the daemon writes today's
    file before any pass, so the newest-file check in :func:`_clock_problem` cannot see it.
    None when there is no such hole."""
    ordered = sorted(days, reverse=True)
    for newer, older in zip(ordered, ordered[1:]):
        if (newer - older).days > keep_days:
            return newer
    return None


def _remove(records_dir: Path, name: str, size: int, dry_run: bool, result: dict) -> None:
    if not dry_run:
        try:
            os.unlink(records_dir / name)
        except FileNotFoundError:
            return  # a concurrent pass removed it first
        except OSError as exc:
            result["errors"].append(f"{name}: delete failed: {exc}")
            return
    result["deleted"].append(name)
    result["bytes_freed"] += size


def _delete_expired(
    records_dir: Path, scan: _Scan, cutoff: date, floor: date | None, dry_run: bool, result: dict
):
    expired = sorted(d for d in scan.days if d < cutoff)
    held = [d for d in expired if floor is not None and d < floor]
    if held:
        result["warnings"].append(
            f"gap check: no day files between {max(held):%Y%m%d} and {floor:%Y%m%d}, longer "
            f"than keep_days; {len(held)} day(s) before the gap kept (a forward clock jump "
            "looks the same as a long downtime); run 'records prune --allow-gap' once the "
            "clock is known to be right"
        )
    for day in expired:
        if day in held:
            continue
        entry = scan.days.pop(day)
        for item in (entry.raw, entry.gz):
            if item:
                _remove(records_dir, item[0], item[1], dry_run, result)


def _remove_stale_tmps(records_dir: Path, scan: _Scan, now: float, dry_run: bool, result: dict):
    for name, mtime, size in sorted(scan.tmps):
        if now - mtime > TMP_MAX_AGE_S:
            _remove(records_dir, name, size, dry_run, result)


def _compress_one(records_dir: Path, entry: _Day, compress: Compressor, result: dict) -> None:
    name, raw_size = entry.raw
    try:
        gz_size = compress(records_dir / name)
    except (OSError, ValueError) as exc:
        result["errors"].append(f"{name}: compress failed, raw day kept: {exc}")
        return
    if gz_size is None:
        return  # a concurrent pass compressed it first
    result["compressed"].append(name)
    result["bytes_freed"] += max(raw_size - gz_size, 0)


def _finish_pair(records_dir: Path, entry: _Day, dry_run: bool, result: dict) -> None:
    """Both raw and .gz exist (a pass stopped between rename and unlink, or is mid-way)."""
    raw_name, raw_size = entry.raw
    gz_name = entry.gz[0]
    if not gz_matches_raw(records_dir / raw_name, records_dir / gz_name):
        result["warnings"].append(
            f"{raw_name}: {gz_name} exists but does not match the raw day; both left alone"
        )
        return
    if not dry_run:
        try:
            os.unlink(records_dir / raw_name)
        except FileNotFoundError:
            return
        except OSError as exc:
            result["errors"].append(f"{raw_name}: removing the compressed raw day failed: {exc}")
            return
    result["compressed"].append(raw_name)
    result["bytes_freed"] += raw_size


def _compress_old(records_dir: Path, scan: _Scan, last: date, dry_run: bool, compress, result):
    for day in sorted(d for d in scan.days if d <= last):
        entry = scan.days[day]
        if entry.raw is None:
            continue
        if entry.gz is not None:
            _finish_pair(records_dir, entry, dry_run, result)
        elif dry_run:
            result["compressed"].append(entry.raw[0])
        else:
            _compress_one(records_dir, entry, compress, result)


def prune_pass(
    records_dir: Path | str,
    now: float,
    keep_days: int,
    *,
    dry_run: bool = False,
    compress: Compressor = compress_day,
    allow_gap: bool = False,
) -> dict:
    """Run one retention pass over ``records_dir`` at wall-clock ``now`` (epoch s).

    Keeps today (the UTC date of ``now``) plus ``keep_days`` complete earlier
    days; deletes day files dated before ``today - keep_days``; gzips raw days
    dated before yesterday. Returns::

        {"deleted": [names], "compressed": [raw names], "bytes_freed": int,
         "errors": [str], "warnings": [str]}

    ``deleted`` also lists stray temp files older than an hour. Per-file
    ``OSError``s never raise: they go to ``errors`` and the pass continues.
    With ``dry_run=True`` nothing on disk changes and the same lists are
    returned; ``bytes_freed`` then counts only the deletions (and raw days whose
    matching ``.gz`` already exists), because a ``.gz`` size is unknown until it
    is written -- it is a lower bound of what a real pass frees.

    Day files before a hole longer than ``keep_days`` are not deleted (a warning says so)
    unless ``allow_gap`` is true; they are still compressed.

    Raises ``ValueError`` for ``keep_days`` < 1 and ``NotADirectoryError`` when
    ``records_dir`` is not a directory.
    """
    records_dir = Path(records_dir)
    _check_args(records_dir, keep_days)
    result = _new_result()
    today = datetime.fromtimestamp(now, timezone.utc).date()
    scan = _scan(records_dir, result)
    problem = _clock_problem(scan.days, today)
    if problem:
        result["warnings"].append(problem)
        return result
    floor = None if allow_gap else _gap_floor(scan.days, keep_days)
    cutoff = today - timedelta(days=keep_days)
    _delete_expired(records_dir, scan, cutoff, floor, dry_run, result)
    _remove_stale_tmps(records_dir, scan, now, dry_run, result)
    _compress_old(records_dir, scan, today - timedelta(days=2), dry_run, compress, result)
    return result

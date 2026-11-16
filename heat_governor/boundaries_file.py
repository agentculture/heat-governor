"""``boundaries.toml``: the page-managed ``[[memory.allow]]`` rules, written safely.

The file sits beside the daemon's ``config.toml`` and holds only ``[[memory.allow]]`` tables.
Rules travel as plain dicts in the TOML shape (keys ``name``, ``comm``, ``cmdline``,
``cgroup``, ``max_mib``, ``scope``); ``rule_to_dict`` converts a config ``AllowRule``.

Public API:

- ``read(path) -> Snapshot``: rules, ``version`` (sha256 of the file bytes, or
  ``MISSING_VERSION`` when there is no file) and ``error`` (a message when the file on disk
  does not parse or validate; the rules are then empty).
- ``write(path, base_version, rules) -> WriteResult``: validates, then under an exclusive
  ``flock`` compares ``base_version`` with the current version. A mismatch writes nothing and
  returns the current rules (``conflict``). Otherwise it backs up the previous file, writes a
  temp file and ``os.replace``s it in. Creating a file passes ``MISSING_VERSION`` as base.
- ``emit(rules) -> str``: the TOML text; ``list_backups(path) -> list[Path]``, newest first.

Recovery: restore a backup by copying ``boundaries.toml.<stamp>.bak`` over ``boundaries.toml``
(the daemon reloads it); at most ``MAX_BACKUPS`` are kept. Deleting the file is a valid state.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from heat_governor import config

MISSING_VERSION = "missing"  # the version of a file that does not exist
MAX_BACKUPS = 5
FIELD_ORDER = ("name", "comm", "cmdline", "cgroup", "max_mib", "scope")
HEADER = "# Managed by heat-governor (boundaries tab / `heat-governor boundaries`).\n"


class BoundariesError(ValueError):
    """The rules are invalid; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Snapshot:
    rules: list[dict]
    version: str
    exists: bool
    error: str | None = None


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    version: str  # the file's version after the call (the new one on success)
    rules: list[dict] = field(default_factory=list)  # as now on disk
    conflict: bool = False


def rule_to_dict(rule) -> dict:
    """An ``AllowRule`` as a boundaries.toml rule dict (unset matchers omitted)."""
    out: dict = {"name": rule.name}
    if rule.comm:
        out["comm"] = list(rule.comm)
    for key in ("cmdline", "cgroup"):
        if getattr(rule, key) is not None:
            out[key] = getattr(rule, key)
    out["max_mib"] = rule.max_mib
    out["scope"] = rule.scope
    return out


def version_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate(rules: list[dict]) -> None:
    """Raise ``BoundariesError`` unless every rule passes config's allow validators."""
    problems: list[str] = []
    if not isinstance(rules, list):
        raise BoundariesError(["rules must be a list"])
    config._validate_allow(rules, problems)
    if problems:
        raise BoundariesError(problems)


def _quote(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _value(v) -> str:
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, list):
        return "[" + ", ".join(_quote(x) for x in v) + "]"
    return str(int(v))


def emit(rules: list[dict]) -> str:
    """The boundaries.toml text for ``rules`` (validated; raises ``BoundariesError``)."""
    validate(rules)
    chunks = [HEADER]
    for rule in rules:
        lines = ["", "[[memory.allow]]"]
        lines += [f"{k} = {_value(rule[k])}" for k in FIELD_ORDER if k in rule]
        chunks.append("\n".join(lines) + "\n")
    text = "".join(chunks)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as e:
        raise BoundariesError([f"a rule string is not valid text: {e.reason}"]) from e
    return text


def _parse(data: bytes) -> tuple[list[dict], str | None]:
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return [], f"boundaries.toml is not valid TOML: {e}"
    mem = doc.pop("memory", {})
    extra = [k for k in mem if k != "allow"] if isinstance(mem, dict) else ["memory"]
    if doc or extra:
        keys = sorted(list(doc) + [f"memory.{k}" for k in extra])
        return [], "boundaries.toml may hold only [[memory.allow]] rules; found " + ", ".join(keys)
    rules = mem.get("allow", [])
    try:
        validate(rules)
    except BoundariesError as e:
        return [], str(e)
    return rules, None


def _snapshot(path: Path) -> Snapshot:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Snapshot([], MISSING_VERSION, False)
    rules, error = _parse(data)
    return Snapshot(rules, version_of(data), True, error)


def read(path: str | os.PathLike) -> Snapshot:
    """The rules and version on disk (no lock: writes replace the file atomically)."""
    return _snapshot(Path(path))


def list_backups(path: str | os.PathLike) -> list[Path]:
    """Backups of ``path``, newest first."""
    p = Path(path)
    found = p.parent.glob(f"{p.name}.*.bak") if p.parent.is_dir() else []
    return sorted(found, key=lambda b: b.name, reverse=True)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock


def _backup(path: Path, data: bytes) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = path.with_name(f"{path.name}.{stamp}.bak")
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    for old in list_backups(path)[MAX_BACKUPS:]:
        old.unlink(missing_ok=True)


def _replace(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write(path: str | os.PathLike, base_version: str, rules: list[dict]) -> WriteResult:
    """Replace the rules if ``base_version`` is still the file's version, else report it.

    Raises ``BoundariesError`` for invalid rules (nothing is written) and ``OSError`` when
    the write fails (the previous file is left byte-identical)."""
    p = Path(path)
    text = emit(rules).encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    with _locked(p):
        current = _snapshot(p)
        if current.version != base_version:
            return WriteResult(False, current.version, current.rules, conflict=True)
        if current.exists:
            _backup(p, p.read_bytes())
        _replace(p, text)
    return WriteResult(True, version_of(text), [dict(r) for r in rules])

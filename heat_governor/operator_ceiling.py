"""The operator's GPU ceiling: law for the daemon.

Operator rule (2026-10-24): "governor hand ceiling is law. request to pass it is
logged, so we can tell that it would have happened". The ceiling actually written
is ``min(operator ceiling, policy band)``: the daemon may go BELOW it for heat,
never above it, and never removes it (no ``reset`` on exit, on turning
``gpu_clock`` off, or in crash recovery while an operator ceiling is set).

Three sources, the lowest wins (:func:`effective`):

* ``config`` -- ``[daemon] gpu_ceiling_mhz`` (live-reloaded);
* ``hold`` -- ``heat-governor gpu-clock hold <MHz>``, persisted in
  ``<data_dir>/operator.json`` so it survives restarts;
* ``observed`` -- a ceiling the hardware shows below what the daemon last
  wrote, made by someone else (Jetson only: the helper's ``status`` reads back
  devfreq ``max_freq``). Persisted in the same file.

``gpu-clock release`` removes the file (a hold or an observed ceiling).

``operator.json``::

    {"gpu_ceiling_mhz": 900, "source": "hold", "at": 1761300000.0,
     "at_utc": "2026-10-24T10:00:00Z"}
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

FILE_NAME = "operator.json"
HOLD, OBSERVED = "hold", "observed"
FILE_SOURCES = (HOLD, OBSERVED)
_MHZ, _SOURCE = "gpu_ceiling_mhz", "source"


@dataclass(frozen=True)
class Hold:
    mhz: int
    source: str  # hold | observed
    at: float


def hold_path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE_NAME


def read_hold(path: Path | None) -> Hold | None:
    """The persisted hold, or ``None`` (no file, or one that is not a valid hold)."""
    if path is None:
        return None
    try:
        doc = json.loads(Path(path).read_text())
        mhz, source = doc[_MHZ], doc[_SOURCE]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if isinstance(mhz, bool) or not isinstance(mhz, int) or mhz <= 0:
        return None
    if source not in FILE_SOURCES:
        return None
    at = doc.get("at")
    return Hold(mhz, source, float(at) if isinstance(at, (int, float)) else 0.0)


def write_hold(path: Path, mhz: int, source: str, now: float | None = None) -> Hold:
    """Persist a hold atomically (write a temp file, fsync, rename)."""
    now = time.time() if now is None else now
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        _MHZ: int(mhz),
        _SOURCE: source,
        "at": round(now, 3),
        "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc) + "\n")
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return Hold(int(mhz), source, round(now, 3))


def clear_hold(path: Path) -> Hold | None:
    """Remove the hold; returns what was there (``None`` if nothing)."""
    old = read_hold(path)
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    return old


def effective(config_mhz: int | None, hold: Hold | None) -> tuple[int | None, str | None]:
    """The law: the lower of the config ceiling and the persisted one, with its
    source (``config`` on a tie)."""
    cands: list[tuple[int, str]] = []
    if config_mhz:
        cands.append((int(config_mhz), "config"))
    if hold is not None:
        cands.append((hold.mhz, hold.source))
    if not cands:
        return None, None
    return min(cands, key=lambda c: c[0])


def _mtime(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


class OperatorCeiling:
    """The daemon's view: the config value plus ``operator.json``, re-read when
    the file's mtime changes (:meth:`poll`)."""

    def __init__(self, path: Path | None, config_mhz: int | None = None) -> None:
        self.path = path
        self.config_mhz = config_mhz
        self.hold = read_hold(path)
        self._mtime = _mtime(path)

    @property
    def mhz(self) -> int | None:
        return effective(self.config_mhz, self.hold)[0]

    @property
    def source(self) -> str | None:
        return effective(self.config_mhz, self.hold)[1]

    def poll(self) -> bool:
        """Re-read the file if it changed; ``True`` when the hold changed."""
        m = _mtime(self.path)
        if m == self._mtime:
            return False
        self._mtime = m
        new = read_hold(self.path)
        changed = new != self.hold
        self.hold = new
        return changed

    def observe(self, mhz: int, now: float) -> None:
        """Record an outside ceiling as the operator's (source ``observed``)."""
        if self.path is None:
            self.hold = Hold(int(mhz), OBSERVED, now)
            return
        self.hold = write_hold(self.path, mhz, OBSERVED, now)
        self._mtime = _mtime(self.path)

    def describe(self) -> str:
        mhz = self.mhz
        return "none" if mhz is None else f"{mhz} MHz ({self.source})"

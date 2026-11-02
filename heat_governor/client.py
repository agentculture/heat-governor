"""Client side of the admission signal -- what a benchmark, a batch job or a
gateway uses to hold new work while a host is hot.

Stdlib only, and this one module is self-contained, so a project that would
rather not depend on heat-governor can cite (copy) it. Three ways to use it::

    from heat_governor import client

    adm = client.read()                      # one look: adm.admit, adm.level
    client.wait(timeout=600)                 # block until new work is admitted
    gate = client.Gate(base_concurrency=4)   # cap in-flight work by the signal
    with gate.slot():
        run_one_request()

The source is the state file the daemon writes (``$HEAT_GOVERNOR_STATE`` or
the default path) or an ``http://host:port/state`` URL for a remote host.

For work that runs on a tensor-parallel pair (one model across two boxes),
pass ``pair=True``: the pair's own signal (the hotter of the two boxes, see
``docs/contracts/state.md``) is read instead of this box's alone; a daemon
without ``[pair]`` falls back to its host signal (reason ``no-pair``).

Missing or stale state means no governor is running there; the default is to
admit (``if_unknown="admit"``) so a job never hangs on a host that simply has
no governor. Pass ``if_unknown="hold"`` where running ungoverned is worse than
waiting.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

ENV_STATE = "HEAT_GOVERNOR_STATE"


def _default_source() -> str:
    if os.environ.get(ENV_STATE):
        return os.environ[ENV_STATE]
    if os.environ.get("XDG_RUNTIME_DIR"):
        return str(Path(os.environ["XDG_RUNTIME_DIR"]) / "heat-governor" / "state.json")
    return str(Path.home() / ".local" / "state" / "heat-governor" / "state.json")


@dataclass(frozen=True)
class Admission:
    admit: bool
    level: str  # cool | warm | hot | critical | unknown
    concurrency_factor: float
    reason: str
    temp_c: float | None
    state: dict | None

    def allowed(self, base_concurrency: int) -> int:
        """How many requests may be in flight: 0 while held, else at least 1."""
        if not self.admit:
            return 0
        return max(1, int(base_concurrency * self.concurrency_factor))


def _load(source: str, timeout: float) -> dict | None:
    try:
        if source.startswith(("http://", "https://")):
            with urllib.request.urlopen(source, timeout=timeout) as resp:  # nosec B310
                doc = json.load(resp)
        else:
            with open(source, encoding="utf-8") as fh:
                doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def read(
    source: str | None = None,
    *,
    if_unknown: str = "admit",
    now: float | None = None,
    timeout: float = 2.0,
    pair: bool = False,
) -> Admission:
    src = source or _default_source()
    doc = _load(src, timeout)
    unknown_admit = if_unknown != "hold"
    if doc is None:
        return Admission(
            unknown_admit, "unknown", 1.0 if unknown_admit else 0.0, "no-state", None, None
        )
    t = now if now is not None else time.time()
    stale_after = float(doc.get("stale_after_s", 10))
    if t - float(doc.get("at", 0)) > stale_after:
        return Admission(
            unknown_admit, "unknown", 1.0 if unknown_admit else 0.0, "stale", None, doc
        )
    reason = str(doc.get("reason", ""))
    sig = doc
    if pair:
        p = doc.get("pair")
        if isinstance(p, dict) and p.get("level") is not None:
            sig, reason = p, f"pair:{p.get('basis', '')}"
        else:
            reason = "no-pair"
    return Admission(
        bool(sig.get("admit", True)),
        str(sig.get("level", "unknown")),
        float(sig.get("concurrency_factor", 1.0)),
        reason,
        doc.get("temp_c"),
        doc,
    )


def wait(
    source: str | None = None,
    *,
    timeout: float | None = None,
    poll_s: float = 1.0,
    if_unknown: str = "admit",
    pair: bool = False,
) -> Admission:
    """Block until new work is admitted; raise ``TimeoutError`` after ``timeout``."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        adm = read(source, if_unknown=if_unknown, pair=pair)
        if adm.admit:
            return adm
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"still {adm.level} after {timeout:g}s ({adm.reason})")
        time.sleep(poll_s)


class Gate:
    """Limit in-flight work to ``Admission.allowed(base_concurrency)``.

    ``slot()`` blocks until the host admits work AND fewer than the allowed
    number of slots are taken. Work already running is never interrupted; when
    the host heats up, new slots simply stop being handed out."""

    def __init__(
        self,
        base_concurrency: int,
        source: str | None = None,
        *,
        poll_s: float = 1.0,
        if_unknown: str = "admit",
        pair: bool = False,
    ) -> None:
        self.base = base_concurrency
        self.pair = pair
        self.source = source
        self.poll_s = poll_s
        self.if_unknown = if_unknown
        self.in_flight = 0
        self._cv = threading.Condition()
        self.last: Admission | None = None

    def _allowed(self) -> int:
        self.last = read(self.source, if_unknown=self.if_unknown, pair=self.pair)
        return self.last.allowed(self.base)

    @contextmanager
    def slot(self) -> Iterator[Admission]:
        with self._cv:
            while self.in_flight >= self._allowed():
                self._cv.wait(self.poll_s)
            self.in_flight += 1
            adm = self.last
        try:
            yield adm  # type: ignore[misc]
        finally:
            with self._cv:
                self.in_flight -= 1
                self._cv.notify_all()

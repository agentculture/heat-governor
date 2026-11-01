"""A tensor-parallel pair: two boxes, one model, one shared thermal decision.

When one model runs across two boxes (sparkrun TP=2: one container, a "rank",
per box, in lockstep over NCCL), each box's daemon governs its own rank, but
the two ranks are one job:

* a CPU cap on one rank slows the whole pair (every token waits for the slower
  rank) and gives the cooler box nothing back;
* a thermal reset of either box kills the job on both, so the pair's risk is
  the hotter box's;
* pausing a rank hangs NCCL, so a pair container is always ``serving``.

So for the pair's containers only, each daemon reads the peer's published state
(``GET /state``) and uses the HOTTER of the two decisions. What is shared is
the decided **level** (for admission) and the decided **CPU cap** (the band
after the controller's lookahead, hysteresis and ramp), not a raw temperature:
both daemons then compute the same function -- ``min`` of the two caps, ``max``
of the two levels -- of the same two inputs, so they pick the same band within
one poll of each other. Re-deriving a band from a shared temperature would put
each box's own ramp state in the way and let them disagree for a ramp interval.

The peer's own top-level ``level`` and ``cpus`` are its LOCAL decision (the
pair result is published separately under ``pair``), so the two daemons never
feed each other's pair result back.

A peer that cannot be read, or whose document stops advancing for
``stale_after_s``, is treated as one band hotter than local: the cooler box
cannot know the hotter one is still cool.

Stdlib only. The fetch runs on a background thread; the control loop only ever
reads the last snapshot, so a slow or dead peer never delays a tick.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable

from heat_governor.config import PairRule

LEVELS = ("cool", "warm", "hot", "critical")
MAX_DOC_BYTES = 1 << 20  # a state document is a few KiB; refuse anything absurd

Fetch = Callable[[str, float], dict]


def fetch_state(peer: str, timeout: float) -> dict:
    """``GET http://<peer>/state``; raises ``OSError``/``ValueError`` on failure."""
    url = f"http://{peer}/state"  # the scheme is fixed: never file:// or a custom one
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec B310
        raw = resp.read(MAX_DOC_BYTES + 1)
    if len(raw) > MAX_DOC_BYTES:
        raise ValueError("state document too large")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("state document is not an object")
    return doc


@dataclass(frozen=True)
class PeerReading:
    """The peer's own (local) decision, as last read."""

    host: str
    level: str
    cpus: int | None
    effective_c: float | None
    temp_c: float | None
    at: float  # the peer's publish time (its clock)
    seen: float  # when this daemon first saw that ``at`` (our clock)
    gpu_operator_mhz: int | None
    gpu_ceiling_mhz: int | None
    has_gpu_operator: bool  # an older peer publishes no gpu_operator_mhz at all


def parse(doc: dict, seen: float) -> PeerReading:
    """Validate a peer document; ``ValueError`` when it is not a live decision."""
    level = doc.get("level")
    at = doc.get("at")
    if level not in LEVELS:
        raise ValueError(f"peer level {level!r} (daemon not ticking yet?)")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        raise ValueError("peer document has no 'at'")
    cpus = doc.get("cpus")
    if cpus is not None and (not isinstance(cpus, (int, float)) or cpus < 1):
        raise ValueError(f"peer cpus {cpus!r}")
    return PeerReading(
        host=str(doc.get("host", "?")),
        level=level,
        cpus=None if cpus is None else int(cpus),
        effective_c=doc.get("effective_c"),
        temp_c=doc.get("temp_c"),
        at=float(at),
        seen=seen,
        gpu_operator_mhz=doc.get("gpu_operator_mhz"),
        gpu_ceiling_mhz=doc.get("gpu_ceiling_mhz"),
        has_gpu_operator="gpu_operator_mhz" in doc,
    )


@dataclass(frozen=True)
class PeerStatus:
    status: str  # ok | stale | unreachable
    reading: PeerReading | None  # the last good reading, even when stale
    age_s: float | None  # seconds since the peer's document last advanced (our clock)
    error: str | None  # the last fetch or parse failure

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class PeerLink:
    """Reads the peer's state every ``poll_s`` on a background thread and keeps
    the last good reading. ``fetch`` and ``clock`` are injectable for tests;
    without :meth:`start` the caller drives :meth:`poll_once` itself."""

    def __init__(
        self,
        rule: PairRule,
        *,
        fetch: Fetch = fetch_state,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.rule = rule
        self.fetch = fetch
        self.clock = clock
        self._lock = threading.Lock()
        self._reading: PeerReading | None = None
        self._failing = False
        self._error: str | None = "not read yet"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def configure(self, rule: PairRule) -> None:
        """A reload: a new peer address forgets what the old one said."""
        with self._lock:
            if rule.peer != self.rule.peer:
                self._reading, self._failing, self._error = None, False, "not read yet"
            self.rule = rule

    def timeout(self) -> float:
        return min(1.0, self.rule.poll_s)

    def poll_once(self) -> None:
        rule = self.rule
        try:
            doc = self.fetch(rule.peer, self.timeout())
            now = self.clock()
            with self._lock:
                prev = self._reading
                seen = prev.seen if prev and prev.at == doc.get("at") else now
                self._reading = parse(doc, seen)
                self._failing, self._error = False, None
        except (OSError, ValueError) as exc:
            with self._lock:
                self._failing, self._error = True, _short(exc)

    def status(self, now: float) -> PeerStatus:
        with self._lock:
            r, failing, err = self._reading, self._failing, self._error
        age = None if r is None else round(max(0.0, now - r.seen), 1)
        if r is None:
            return PeerStatus("unreachable", None, None, err)
        if age is not None and age > self.rule.stale_after_s:
            return PeerStatus("unreachable" if failing else "stale", r, age, err)
        return PeerStatus("ok", r, age, err)

    # -- the background thread
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="heat-governor-pair", daemon=True)
        self._thread.start()

    def _loop(self) -> None:  # pragma: no cover - thread shell around poll_once
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.rule.poll_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout() + 1.0)
        self._thread = None


def _short(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)  # urllib.error.URLError
    text = str(reason if reason is not None else exc) or type(exc).__name__
    return text[:200]


# -- the shared decision (pure) -------------------------------------------------


def tighter(a: int | None, b: int | None) -> int | None:
    """The tighter of two CPU caps; ``None`` = no cap."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def hotter(a: str, b: str) -> str:
    return a if LEVELS.index(a) >= LEVELS.index(b) else b


def one_band_hotter(cpus: int | None, bands: list[int]) -> int | None:
    """The next band down from ``cpus`` (``None`` -> the loosest band); the
    lowest band stays. With no bands there is nothing tighter to choose."""
    if not bands:
        return cpus
    if cpus is None:
        return bands[-1]
    lower = [b for b in bands if b < cpus]
    return lower[-1] if lower else min(cpus, bands[0])


def one_level_hotter(level: str) -> str:
    return LEVELS[min(LEVELS.index(level) + 1, len(LEVELS) - 1)]


@dataclass(frozen=True)
class PairDecision:
    level: str
    admit: bool
    concurrency_factor: float
    cpus: int | None
    basis: str  # local | peer | equal | peer_lost


def decide(
    local_level: str,
    local_cpus: int | None,
    peer: PeerStatus,
    *,
    cpu_bands: list[int],
    concurrency: tuple[float, ...],
) -> PairDecision:
    """The pair's level and CPU cap: the hotter of the two boxes, or one band
    hotter than local when the peer is stale or unreachable."""
    if peer.ok and peer.reading is not None:
        p = peer.reading
        level = hotter(local_level, p.level)
        cpus = tighter(local_cpus, p.cpus)
        if (level, cpus) == (local_level, local_cpus) and (level, cpus) == (p.level, p.cpus):
            basis = "equal"
        elif (level, cpus) == (local_level, local_cpus):
            basis = "local"
        else:
            basis = "peer"
    else:
        level = one_level_hotter(local_level)
        cpus = one_band_hotter(local_cpus, cpu_bands)
        basis = "peer_lost"
    factor = concurrency[LEVELS.index(level)]
    return PairDecision(level, factor > 0, factor, cpus, basis)

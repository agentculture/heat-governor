"""The fleet reader: poll every machine's ``/state``, judge freshness, keep 24 h of history.

One reader serves the whole fleet view (``mesh serve`` embeds it; the CLI and the MCP query
it). It only ever calls ``transport.fetch()`` and ``transport.close()``: it never writes to
a box, and it imports nothing from the daemon.

Public API::

    reader = FleetReader(fleet, transport_factory=None, clock=time.monotonic,
                         wall=time.time, stale_after_s=None, state_dir=None)
    reader.start(); reader.stop(); reader.close()
    reader.poll_once(name)            # one read of one machine (the threads call this)
    reader.snapshot() -> dict         # JSON-serialisable, see below
    reader.history(name, since_s=86400, max_points=300) -> list[dict]

Threads: one daemon thread per machine (``fleet-reader:<name>``), so a hung or slow box
never delays the others. :meth:`stop` sets an event and joins with a bounded deadline; a
thread stuck in a fetch is left to finish on its own (it closes its transport when it does).

Freshness follows :class:`heat_governor.pair.PeerLink`: the age is measured on the
READER's clock from when the document's ``at`` last changed (so clock skew between boxes
does not matter, and a frozen box that still answers ages), and the window is the larger
of the reader's own (``2 * poll_s + timeout_s`` by default) and the box's published
``stale_after_s``, the latter honoured up to :data:`MAX_PEER_STALE_S`. On top of pair's
rules a box goes ``stale`` first and only ``unreachable`` after twice the window while it
is not answering (its last read failed, or a read has hung past ``timeout_s``); a box that
answers with a frozen document stays ``stale``. A box never read is ``unreachable``.

Admission is never reported for a box that is not ``ok``: its last known thermal fields are
kept (labelled by ``state`` and ``age_s``) but ``admit`` and ``concurrency_factor`` are
``null``. Do not use :func:`heat_governor.client.read` here: it admits on unknown.

History is compact: per box, a ring of ``(wall time, hottest temp, GPU temp, level index,
concurrency factor)`` in parallel ``array`` columns (33 bytes per sample), one sample per
new document, bounded to 24 h at the poll interval.
"""

from __future__ import annotations

import math
import threading
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from heat_governor.fleet import Fleet, Machine
from heat_governor.pair import MAX_PEER_STALE_S
from heat_governor.policy import LEVELS
from heat_governor.transport import GRACE_S, LocalTransport, SshTransport

SCHEMA = 1
HISTORY_S = 86_400
DEFAULT_MAX_POINTS = 300
STOP_JOIN_S = 1.0

# the /state fields a fleet view shows; everything else (sensors, units, ...) is dropped
THERMAL_FIELDS = (
    "host",
    "platform",
    "version",
    "at",
    "stale_after_s",
    "level",
    "admit",
    "concurrency_factor",
    "temp_c",
    "gpu_temp_c",
    "limits",
    "applying",
    "containers",
    "pair",
    "reason",
    "rate_c_s",
    "hottest_zone",
)


class Transport(Protocol):
    def fetch(self) -> dict: ...

    def close(self) -> None: ...


TransportFactory = Callable[[Machine], Transport]


def default_transport(fleet: Fleet, state_dir: Path | str | None = None) -> TransportFactory:
    """ssh machines over :class:`SshTransport`, the local one over :class:`LocalTransport`."""

    def make(m: Machine) -> Transport:
        if m.transport == "local":
            return LocalTransport(timeout_s=fleet.timeout_s)
        return SshTransport(m.host, timeout_s=fleet.timeout_s, state_dir=state_dir)

    return make


def _finite(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num(v: object) -> float | None:
    return float(v) if _finite(v) else None


def extract(doc: dict) -> dict:
    """The thermal fields of a /state document; ``ValueError`` when it is no live decision
    (the same test as :func:`heat_governor.pair.parse`: a known level and a finite ``at``)."""
    if doc.get("level") not in LEVELS:
        raise ValueError(f"level {doc.get('level')!r} (daemon not ticking yet?)")
    if not _finite(doc.get("at")):
        raise ValueError("document has no finite 'at'")
    out = {k: doc.get(k) for k in THERMAL_FIELDS}
    out["containers"] = out["containers"] if isinstance(out["containers"], list) else []
    if not isinstance(out["limits"], dict):
        out["limits"] = None
    return out


def classify(
    now: float, seen: float | None, window: float, failing: bool
) -> tuple[str, float | None]:
    """``(state, age_s)``: ok within ``window``; past it stale; past twice it unreachable
    while the box is not answering. Never read -> unreachable."""
    if seen is None:
        return "unreachable", None
    age = round(max(0.0, now - seen), 1)
    if age <= window:
        return "ok", age
    if failing and age > 2 * window:
        return "unreachable", age
    return "stale", age


# -- compact history --------------------------------------------------------------

Sample = tuple  # (t, temp_c | None, gpu_temp_c | None, level_index | None, cf | None)
_NAN = float("nan")


def _f(v: float | None) -> float:
    return _NAN if v is None else v


def _opt(v: float) -> float | None:
    return None if v != v else v  # NaN -> None


class Ring:
    """Fixed-capacity, array-backed ring of compact samples (oldest overwritten)."""

    __slots__ = ("cap", "_t", "_temp", "_gpu", "_cf", "_lvl", "_head")

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._t = array("d")
        self._temp = array("d")
        self._gpu = array("d")
        self._cf = array("d")
        self._lvl = array("b")
        self._head = 0  # next slot to overwrite once full

    def __len__(self) -> int:
        return len(self._t)

    def append(
        self, t: float, temp: float | None, gpu: float | None, lvl: int | None, cf: float | None
    ) -> None:
        row = (t, _f(temp), _f(gpu), _f(cf), -1 if lvl is None else lvl)
        if len(self._t) < self.cap:
            for col, v in zip((self._t, self._temp, self._gpu, self._cf, self._lvl), row):
                col.append(v)
            return
        h = self._head
        self._t[h], self._temp[h], self._gpu[h], self._cf[h], self._lvl[h] = row
        self._head = (h + 1) % self.cap

    def extend(self, t: array, temp: array, gpu: array, lvl: array, cf: array) -> None:
        """Bulk append parallel columns (NaN = unknown, level -1 = unknown), e.g. to restore
        a saved history. Input longer than the capacity keeps only its newest ``cap`` rows."""
        cols = (t, temp, gpu, cf, lvl)
        if len(t) >= self.cap:
            for mine, src in zip((self._t, self._temp, self._gpu, self._cf, self._lvl), cols):
                del mine[:]
                mine.extend(src[len(src) - self.cap :])
            self._head = 0
            return
        for i in range(len(t)):
            lv = lvl[i]
            self.append(t[i], _opt(temp[i]), _opt(gpu[i]), None if lv < 0 else lv, _opt(cf[i]))

    def samples(self, since: float | None = None) -> list[Sample]:
        n, h = len(self._t), self._head if len(self._t) == self.cap else 0
        out = []
        for k in range(n):
            i = (h + k) % n
            t = self._t[i]
            if since is not None and t < since:
                continue
            lvl = self._lvl[i]
            out.append(
                (
                    t,
                    _opt(self._temp[i]),
                    _opt(self._gpu[i]),
                    None if lvl < 0 else lvl,
                    _opt(self._cf[i]),
                )
            )
        return out


def _max(vals: list) -> Any:
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def _min(vals: list) -> Any:
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def downsample(rows: list[Sample], max_points: int) -> list[Sample]:
    """At most ``max_points`` buckets, conservatively: each keeps its last time, its
    hottest temperatures and level, and its lowest concurrency factor (so a peak is never
    averaged away)."""
    n = len(rows)
    if max_points < 1 or n <= max_points:
        return list(rows)
    out = []
    for k in range(max_points):
        b = rows[k * n // max_points : (k + 1) * n // max_points]
        cols = list(zip(*b))
        out.append((cols[0][-1], _max(cols[1]), _max(cols[2]), _max(cols[3]), _min(cols[4])))
    return out


def _sample_dict(s: Sample) -> dict:
    return {
        "t": s[0],
        "temp_c": s[1],
        "gpu_temp_c": s[2],
        "level": None if s[3] is None else LEVELS[s[3]],
        "concurrency_factor": s[4],
    }


# -- per-machine state ------------------------------------------------------------


@dataclass
class _Box:
    machine: Machine
    transport: Transport
    ring: Ring
    reading: dict | None = None
    seen: float | None = None  # reader clock when `at` last changed
    failing: bool = False
    error: str | None = None  # the last transport/parse failure (kept after recovery)
    error_at: float | None = None
    last_poll: float | None = None  # wall time of the last completed attempt
    last_ok: float | None = None
    inflight_since: float | None = None  # reader clock, while a fetch is running
    polls: int = 0
    failures: int = 0
    thread: threading.Thread | None = field(default=None, repr=False)


def _short(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return str(text)[:200]


class FleetReader:
    def __init__(
        self,
        fleet: Fleet,
        *,
        transport_factory: TransportFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        stale_after_s: float | None = None,
        state_dir: Path | str | None = None,
    ) -> None:
        self.fleet = fleet
        self.clock = clock
        self.wall = wall
        self.own_window = (
            float(stale_after_s)
            if stale_after_s is not None
            else 2 * fleet.poll_s + fleet.timeout_s
        )
        self.capacity = int(math.ceil(HISTORY_S / fleet.poll_s)) + 1
        factory = transport_factory or default_transport(fleet, state_dir)
        self._boxes = {m.name: _Box(m, factory(m), Ring(self.capacity)) for m in fleet.machines}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stop.set()  # not running until start()
        self._started_clock = clock()
        self._started_wall = wall()

    # -- one read
    def poll_once(self, name: str) -> None:
        """One read of one machine. Never raises: a failure is recorded on the box."""
        box = self._boxes[name]
        with self._lock:
            box.inflight_since = self.clock()
        try:
            reading = extract(box.transport.fetch())
        except Exception as exc:  # noqa: BLE001 - a bad read must never kill the poller
            with self._lock:
                box.inflight_since = None
                box.failing, box.error = True, _short(exc)
                box.error_at = box.last_poll = self.wall()
                box.polls += 1
                box.failures += 1
            return
        with self._lock:
            now, wall = self.clock(), self.wall()
            box.inflight_since = None
            box.polls += 1
            box.failing = False
            box.last_poll = box.last_ok = wall
            prev = box.reading
            if prev is None or prev["at"] != reading["at"]:
                # fresh only when `at` CHANGED: a frozen document keeps its first-seen time
                box.seen = now
                lvl = reading["level"]
                box.ring.append(
                    wall,
                    _num(reading["temp_c"]),
                    _num(reading["gpu_temp_c"]),
                    LEVELS.index(lvl),
                    _num(reading["concurrency_factor"]),
                )
            box.reading = reading

    # -- views
    def _window(self, reading: dict | None) -> float:
        peer = reading.get("stale_after_s") if reading else None
        if _finite(peer) and peer > 0:
            return max(self.own_window, min(float(peer), MAX_PEER_STALE_S))
        return self.own_window

    def _entry(self, box: _Box, now: float) -> dict:
        hung = (
            box.inflight_since is not None
            and now - box.inflight_since > self.fleet.timeout_s + GRACE_S
        )
        window = self._window(box.reading)
        state, age = classify(now, box.seen, window, box.failing or hung)
        thermal = dict(box.reading) if box.reading is not None else None
        if thermal is not None and state != "ok":
            thermal["admit"] = None
            thermal["concurrency_factor"] = None
        m = box.machine
        return {
            "name": m.name,
            "transport": m.transport,
            "host": m.host or None,
            "state": state,
            "age_s": age,
            "stale_after_s": window,
            "health": {
                "last_poll": box.last_poll,
                "last_ok": box.last_ok,
                "last_error": box.error or ("not read yet" if box.polls == 0 else None),
                "last_error_at": box.error_at,
                "failing": box.failing or hung,
                "polls": box.polls,
                "failures": box.failures,
                "polling_for_s": (
                    None
                    if box.inflight_since is None
                    else round(max(0.0, now - box.inflight_since), 1)
                ),
                "history_samples": len(box.ring),
            },
            "thermal": thermal,
        }

    def snapshot(self) -> dict:
        with self._lock:
            now = self.clock()
            machines = [self._entry(b, now) for b in self._boxes.values()]
        return {
            "schema": SCHEMA,
            "reader": {
                "started_at": self._started_wall,
                "now": self.wall(),
                "uptime_s": round(max(0.0, now - self._started_clock), 1),
                "running": self.running,
                "poll_s": self.fleet.poll_s,
                "timeout_s": self.fleet.timeout_s,
                "stale_after_s": self.own_window,
                "history_s": HISTORY_S,
            },
            "machines": machines,
        }

    def history(
        self, name: str, since_s: float = HISTORY_S, max_points: int = DEFAULT_MAX_POINTS
    ) -> list[dict]:
        """Compact samples of one machine for the last ``since_s`` (wall time), oldest
        first, downsampled to at most ``max_points``. ``KeyError`` for an unknown name."""
        box = self._boxes[name]
        since = self.wall() - since_s
        with self._lock:
            rows = box.ring.samples(since)
        return [_sample_dict(s) for s in downsample(rows, max_points)]

    # -- threads
    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    def start(self) -> None:
        if self.running:
            return
        stop = threading.Event()
        self._stop = stop
        for box in self._boxes.values():
            box.thread = threading.Thread(
                target=self._loop,
                args=(box, stop),
                name=f"fleet-reader:{box.machine.name}",
                daemon=True,
            )
            box.thread.start()

    def _loop(self, box: _Box, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                began = time.monotonic()
                self.poll_once(box.machine.name)
                stop.wait(max(0.0, self.fleet.poll_s - (time.monotonic() - began)))
        finally:
            if self._stop is stop:  # no newer start() owns this transport now
                _close(box.transport)

    def stop(self, join_s: float = STOP_JOIN_S) -> None:
        """Stop polling. Returns within about ``join_s`` even if a fetch is hung."""
        self._stop.set()
        deadline = time.monotonic() + join_s
        for box in self._boxes.values():
            if box.thread is not None:
                box.thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def close(self) -> None:
        """Stop, and close the transports of machines whose poller is not running."""
        self.stop()
        for box in self._boxes.values():
            if box.thread is None or not box.thread.is_alive():
                _close(box.transport)


def _close(t: Transport) -> None:
    try:
        t.close()
    except Exception:  # noqa: BLE001  # nosec B110 - closing is best effort
        pass

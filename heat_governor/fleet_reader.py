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
    reader.history(name, since_s=86400, max_points=300) -> list[dict]   # in-memory buffer only
    reader.trend(name, since_s=86400, max_points=300) -> {"source", "samples"}

Threads: one daemon thread per machine (``fleet-reader:<name>``), so a hung or slow box
never delays the others. :meth:`stop` sets an event and joins with a bounded deadline; a
thread stuck in a fetch is left to finish on its own (it closes its transport when it does,
unless a restarted poller owns it -- and that poller waits for it, so a box never has two).
:meth:`poll_all` is the one-shot read (``mesh status``'s direct path), tracked the same way:
:meth:`close` only closes a transport no thread is using.

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
concurrency factor, memory used %)`` in parallel ``array`` columns (41 bytes per sample),
one sample per new document, bounded to 24 h at the poll interval.

Trends (what the page draws) come first from the machine's own record files:
:meth:`FleetReader.trend` asks the transport's ``history(since, until, max_points)`` (see
:mod:`heat_governor.history`; up to 7 days), caches the answer per ``(machine, range,
points)`` for :data:`TREND_TTL_S` (:data:`TREND_LONG_TTL_S` for windows over 26 h) in a
bounded cache, and falls back to the in-memory buffer (``"source": "buffer"``) when the
transport has no ``history``, the call fails (remembered for the same TTL so a page refresh
does not retry ssh every time), or the records answer is empty while the buffer is not.
Concurrent misses of one key share a single in-flight read instead of each starting ssh. It
runs on the caller's (HTTP handler) thread without the reader lock, bounded by the
transport's own timeout, so the polling threads are never blocked.
"""

from __future__ import annotations

import math
import re
import threading
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from heat_governor.fleet import Fleet, Machine
from heat_governor.history import LONG_RANGE_S, timeout_for
from heat_governor.pair import MAX_PEER_STALE_S
from heat_governor.policy import LEVELS
from heat_governor.transport import GRACE_S, LocalTransport, SshTransport

SCHEMA = 1
HISTORY_S = 86_400
DEFAULT_MAX_POINTS = 300
STOP_JOIN_S = 1.0
TREND_TTL_S = 60.0
TREND_LONG_TTL_S = 300.0
TREND_CACHE_MAX = 64

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
    "gpu_clock",  # the clock-ceiling helper status, e.g. "ok (helper status: 1575)"
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


_MEM_ZONE = re.compile(r"^(ddr|ddrss|dram|emc|mem)(\b|[-_])", re.IGNORECASE)
_NOT_MEMORY = ("pmm", "pmic")  # a power-management IC beside the DRAM is not the DRAM


def _memory_zone_temps(sensors: dict) -> list[float]:
    temps = []
    for key, val in sensors.items():
        if not (isinstance(key, str) and key.startswith("zone:")) or not _finite(val):
            continue
        ztype = key.split(":", 2)[-1]
        if _MEM_ZONE.match(ztype) and not any(w in ztype.lower() for w in _NOT_MEMORY):
            temps.append(float(val))
    return temps


def _memory(doc: dict) -> dict | None:
    """The derived memory object from a document's ``sensors`` (``None`` when it has no
    ``mem:*`` key). ``total_mib`` is derived (the document carries none); ``temp_c`` is the
    hottest DDR/DRAM/EMC zone, ``None`` where the board exposes no memory zone."""
    sensors = doc.get("sensors")
    if not isinstance(sensors, dict):
        return None
    used, avail = _num(sensors.get("mem:used_pct")), _num(sensors.get("mem:avail_mib"))
    swap = _num(sensors.get("mem:swap_used_mib"))
    if used is None and avail is None and swap is None:
        return None
    total = None
    if used is not None and avail is not None and used < 100:
        total = round(avail / (1 - used / 100))
    temps = _memory_zone_temps(sensors)
    return {
        "used_pct": used,
        "avail_mib": None if avail is None else round(avail),
        "total_mib": total,
        "swap_used_mib": None if swap is None else round(swap),
        "pressure_some_avg10": _num(sensors.get("psi:memory_some_avg10")),
        "temp_c": max(temps) if temps else None,
    }


def _first_key_mhz(s: dict, match: Callable[[str], bool]) -> float | None:
    """The first finite value, in sorted key order, among ``gpu:`` keys that satisfy ``match``."""
    for key in sorted(s):
        if key.startswith("gpu:") and match(key) and _num(s[key]) is not None:
            return _num(s[key])
    return None


def _gpu_mhz(s: dict) -> float | None:
    for name in ("gpu:sm_mhz", "gpu:gr_mhz"):
        mhz = _num(s.get(name))
        if mhz is not None:
            return mhz
    mhz = _first_key_mhz(s, lambda k: k.endswith(".gpu.mhz"))
    if mhz is not None:
        return mhz
    return _first_key_mhz(s, lambda k: "gpc" in k and k.endswith(".mhz"))


def _gpu_power(doc: dict) -> dict:
    """GPU clock (MHz), GPU power and utilisation, and board power from ``sensors``.

    Clock priority: the SM / graphics clock (nvidia-smi boxes), then a devfreq ``*.gpu.mhz``
    node (VENTUNO Q, Orin), then a Jetson ``gpu-gpc-*`` clock; video-engine and memory clocks
    are never taken for the GPU clock. ``power_w`` is board power (an ina232-style monitor),
    kept apart from ``gpu_power_w``."""
    sensors = doc.get("sensors")
    s = sensors if isinstance(sensors, dict) else {}
    return {
        "gpu_mhz": _gpu_mhz(s),
        "gpu_power_w": _num(s.get("gpu:power_w")),
        "gpu_util_pct": _num(s.get("gpu:util_pct")),
        "power_w": _num(s.get("gpu:board_power_w")),
    }


_FAN_RPM = re.compile(r"^fan:.+\.fan\d+_input$")  # hwmon fanN_input is RPM; pwmN is duty


def _fan_zones(doc: dict) -> dict:
    """``fan_rpm``: the fastest fan's speed from the ``fan:<chip>.fan<N>_input`` sensors
    (``pwm`` keys are duty, not speed), ``None`` without one; ``zones``: how many thermal
    zones the box read this tick (finite ``zone:*`` sensors), ``None`` without any."""
    sensors = doc.get("sensors")
    s = sensors if isinstance(sensors, dict) else {}
    rpms = [
        float(v) for k, v in s.items() if isinstance(k, str) and _FAN_RPM.match(k) and _finite(v)
    ]
    zones = sum(
        1 for k, v in s.items() if isinstance(k, str) and k.startswith("zone:") and _finite(v)
    )
    return {"fan_rpm": max(rpms) if rpms else None, "zones": zones or None}


def extract(doc: dict) -> dict:
    """The thermal fields of a /state document; ``ValueError`` when it is no live decision
    (the same test as :func:`heat_governor.pair.parse`: a known level and a finite ``at``).

    Derived from ``sensors`` on top of :data:`THERMAL_FIELDS`: ``memory`` (:func:`_memory`),
    ``gpu_mhz`` / ``gpu_power_w`` / ``gpu_util_pct`` / ``power_w`` (:func:`_gpu_power`), and
    ``fan_rpm`` / ``zones`` (:func:`_fan_zones`)."""
    if doc.get("level") not in LEVELS:
        raise ValueError(f"level {doc.get('level')!r} (daemon not ticking yet?)")
    if not _finite(doc.get("at")):
        raise ValueError("document has no finite 'at'")
    out = {k: doc.get(k) for k in THERMAL_FIELDS}
    out["containers"] = out["containers"] if isinstance(out["containers"], list) else []
    if not isinstance(out["limits"], dict):
        out["limits"] = None
    out["memory"] = _memory(doc)
    out.update(_gpu_power(doc))
    out.update(_fan_zones(doc))
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

Sample = tuple  # (t, temp_c, gpu_temp_c, level_index, cf, mem_used_pct); each but t may be None
_NAN = float("nan")


def _f(v: float | None) -> float:
    return _NAN if v is None else v


def _opt(v: float) -> float | None:
    return None if math.isnan(v) else v  # NaN -> None


class Ring:
    """Fixed-capacity, array-backed ring of compact samples (oldest overwritten)."""

    __slots__ = ("cap", "_t", "_temp", "_gpu", "_cf", "_lvl", "_mem", "_head")

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._t = array("d")
        self._temp = array("d")
        self._gpu = array("d")
        self._cf = array("d")
        self._lvl = array("b")
        self._mem = array("d")
        self._head = 0  # next slot to overwrite once full

    def __len__(self) -> int:
        return len(self._t)

    def append(
        self,
        t: float,
        temp: float | None,
        gpu: float | None,
        lvl: int | None,
        cf: float | None,
        mem: float | None = None,
    ) -> None:
        row = (t, _f(temp), _f(gpu), _f(cf), -1 if lvl is None else lvl, _f(mem))
        if len(self._t) < self.cap:
            for col, v in zip(self._cols(), row):
                col.append(v)
            return
        h = self._head
        for col, v in zip(self._cols(), row):
            col[h] = v
        self._head = (h + 1) % self.cap

    def _cols(self) -> tuple:
        return (self._t, self._temp, self._gpu, self._cf, self._lvl, self._mem)

    def extend(
        self, t: array, temp: array, gpu: array, lvl: array, cf: array, mem: array | None = None
    ) -> None:
        """Bulk append parallel columns (NaN = unknown, level -1 = unknown), e.g. to restore
        a saved history. Input longer than the capacity keeps only its newest ``cap`` rows."""
        cols = (t, temp, gpu, cf, lvl, mem if mem is not None else array("d", [_NAN]) * len(t))
        if len(t) >= self.cap:
            for mine, src in zip(self._cols(), cols):
                del mine[:]
                mine.extend(src[len(src) - self.cap :])
            self._head = 0
            return
        for i in range(len(t)):
            lv = lvl[i]
            self.append(
                t[i],
                _opt(temp[i]),
                _opt(gpu[i]),
                None if lv < 0 else lv,
                _opt(cf[i]),
                _opt(cols[5][i]),
            )

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
                    _opt(self._mem[i]),
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
    hottest temperatures, level and memory use, and its lowest concurrency factor (so a peak
    is never averaged away)."""
    n = len(rows)
    if max_points < 1 or n <= max_points:
        return list(rows)
    out = []
    for k in range(max_points):
        b = rows[k * n // max_points : (k + 1) * n // max_points]
        cols = list(zip(*b))
        out.append(
            (cols[0][-1], _max(cols[1]), _max(cols[2]), _max(cols[3]), _min(cols[4]), _max(cols[5]))
        )
    return out


def _sample_dict(s: Sample) -> dict:
    return {
        "t": s[0],
        "temp_c": s[1],
        "gpu_temp_c": s[2],
        "level": None if s[3] is None else LEVELS[s[3]],
        "concurrency_factor": s[4],
        "mem_used_pct": s[5],
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
    users: int = 0  # threads using the transport now; the last one out may close it


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
        # (name, since_s, max_points) -> (expires on the reader clock, samples or None=failed)
        self._trend_cache: dict[tuple, tuple[float, list | None]] = {}
        self._trend_lock = threading.Lock()  # guards the cache dict only, never held over I/O
        self._trend_inflight: dict[tuple, _Flight] = {}  # key -> the one read in progress

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
                    _num((reading["memory"] or {}).get("used_pct")),
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

    def trend(
        self, name: str, since_s: float = HISTORY_S, max_points: int = DEFAULT_MAX_POINTS
    ) -> dict:
        """``{"source": "records"|"buffer", "samples": [...]}`` for the last ``since_s``:
        the machine's record files when its transport can read them, else the buffer (see the
        module docstring). ``KeyError`` for an unknown name."""
        box = self._boxes[name]
        key = (name, float(since_s), int(max_points))
        now = self.clock()
        leader = False
        with self._trend_lock:
            hit = self._trend_cache.get(key)
            if hit is not None and hit[0] > now:
                flight = None
            else:
                flight = self._trend_inflight.get(key)
                if flight is None:
                    flight = self._trend_inflight[key] = _Flight()
                    leader = True
        if flight is None:
            samples = hit[1]  # type: ignore[index]
        elif leader:
            samples = self._read_trend(box, key, since_s, max_points, flight)
        else:  # a read of the same window is running: share it, never start another
            samples = flight.wait(timeout_for(since_s) + GRACE_S)
        if samples:
            return {"source": "records", "samples": samples}
        buffered = self.history(name, since_s, max_points)
        if buffered or samples is None:
            return {"source": "buffer", "samples": buffered}
        return {"source": "records", "samples": samples}

    def _read_trend(
        self, box: _Box, key: tuple, since_s: float, max_points: int, flight: _Flight
    ) -> list | None:
        """The leader's records read for ``key``: cached, then handed to every waiter."""
        samples: list | None = None
        try:
            fn = getattr(box.transport, "history", None)
            if callable(fn):
                until = self.wall()
                try:
                    samples = list(fn(until - since_s, until, max_points)["samples"])
                except Exception:  # noqa: BLE001 - any failure falls back to the buffer
                    samples = None
                ttl = TREND_LONG_TTL_S if since_s > LONG_RANGE_S else TREND_TTL_S
                self._remember(key, self.clock() + ttl, samples)
        finally:
            with self._trend_lock:
                self._trend_inflight.pop(key, None)
            flight.finish(samples)
        return samples

    def _remember(self, key: tuple, expires: float, samples: list | None) -> None:
        now = self.clock()
        with self._trend_lock:
            cache = self._trend_cache
            for k in [k for k, (exp, _) in cache.items() if exp <= now]:
                del cache[k]
            while len(cache) >= TREND_CACHE_MAX:
                del cache[min(cache, key=lambda k: cache[k][0])]
            cache[key] = (expires, samples)

    # -- threads
    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    def start(self) -> None:
        """Start one poller per machine. A machine whose previous poller is still stuck in
        a fetch (a bounded :meth:`stop` returned without it) gets a poller that first waits
        for the old one to exit, so one box never has two pollers or two concurrent reads."""
        if self.running:
            return
        stop = threading.Event()
        self._stop = stop
        for box in self._boxes.values():
            prev = box.thread if box.thread is not None and box.thread.is_alive() else None
            with self._lock:
                box.users += 1
            box.thread = threading.Thread(
                target=self._loop,
                args=(box, stop, prev),
                name=f"fleet-reader:{box.machine.name}",
                daemon=True,
            )
            box.thread.start()

    def _loop(self, box: _Box, stop: threading.Event, prev: threading.Thread | None = None) -> None:
        try:
            if prev is not None:
                prev.join()  # bounded: the old fetch ends at its transport's deadline
            while not stop.is_set():
                began = time.monotonic()
                self.poll_once(box.machine.name)
                stop.wait(max(0.0, self.fleet.poll_s - (time.monotonic() - began)))
        finally:
            self._release(box)

    def _release(self, box: _Box) -> None:
        """A thread is done with the box's transport: the last one out closes it unless a
        running poller still owns it."""
        with self._lock:
            box.users -= 1
            last = box.users == 0 and not self.running
        if last:
            _close(box.transport)

    def poll_all(self, join_s: float) -> None:
        """One concurrent read of every machine (``mesh status``'s direct path), returning
        within ``join_s``. A read still running at the deadline keeps its transport; its
        thread closes it when the read ends, never :meth:`close` under it."""
        threads = []
        for box in self._boxes.values():
            with self._lock:
                box.users += 1
            th = threading.Thread(
                target=self._once, args=(box,), name=f"fleet-read:{box.machine.name}", daemon=True
            )
            th.start()
            threads.append(th)
        deadline = time.monotonic() + join_s
        for th in threads:
            th.join(timeout=max(0.0, deadline - time.monotonic()))

    def _once(self, box: _Box) -> None:
        try:
            self.poll_once(box.machine.name)
        finally:
            self._release(box)

    def stop(self, join_s: float = STOP_JOIN_S) -> None:
        """Stop polling. Returns within about ``join_s`` even if a fetch is hung."""
        self._stop.set()
        deadline = time.monotonic() + join_s
        for box in self._boxes.values():
            if box.thread is not None:
                box.thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def close(self) -> None:
        """Stop, and close the transports no thread is using; a thread still in a fetch
        closes its own transport when the fetch ends."""
        self.stop()
        with self._lock:
            idle = [b for b in self._boxes.values() if b.users == 0]
        for box in idle:
            _close(box.transport)


class _Flight:
    """One in-progress trend read that concurrent callers of the same key wait on."""

    def __init__(self) -> None:
        self._done = threading.Event()
        self._samples: list | None = None

    def finish(self, samples: list | None) -> None:
        self._samples = samples
        self._done.set()

    def wait(self, timeout: float) -> list | None:
        return self._samples if self._done.wait(timeout) else None


def _close(t: Transport) -> None:
    try:
        t.close()
    except Exception:  # noqa: BLE001  # nosec B110 - closing is best effort
        pass

"""Fleet reader tests: fake transports, an injected clock, no real network."""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
import threading
import time
import tracemalloc
from array import array

import pytest

from heat_governor.fleet import Fleet, Machine
from heat_governor.fleet_reader import (
    HISTORY_S,
    FleetReader,
    Ring,
    classify,
    default_transport,
    downsample,
    extract,
)
from heat_governor.transport import LocalTransport, SshTransport, Unreachable


def doc(at: float, level: str = "cool", temp: float = 50.0, **extra) -> dict:
    d = {
        "schema": 1,
        "host": "box",
        "platform": "spark",
        "version": "0.13.0",
        "at": at,
        "stale_after_s": 5.0,
        "level": level,
        "admit": level in ("cool", "warm"),
        "concurrency_factor": {"cool": 1.0, "warm": 0.5}.get(level, 0.0),
        "temp_c": temp,
        "gpu_temp_c": temp - 5,
        "limits": {"warm_c": 70, "hot_c": 80, "crit_c": 90},
        "applying": False,
        "containers": [],
        "reason": "steady",
        "rate_c_s": 0.0,
        "hottest_zone": "0:cpu",
        "sensors": {"zone:0:cpu": temp},  # not copied: the snapshot is compact
    }
    d.update(extra)
    return d


class FakeTransport:
    """Records every call; serves queued documents or raises queued errors."""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.calls: list[str] = []
        self.last = None
        self.block: threading.Event | None = None  # set -> fetch hangs until released

    def fetch(self) -> dict:
        self.calls.append("fetch")
        if self.block is not None:
            self.block.wait()
        item = self.items.pop(0) if self.items else self.last
        if isinstance(item, Exception):
            raise item
        if item is None:
            raise Unreachable("nothing queued")
        self.last = item
        return item

    def close(self) -> None:
        self.calls.append("close")


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(machines=("a",), poll_s=10.0, timeout_s=5.0, **kw):
    fleet = Fleet(
        machines=tuple(Machine(n, "ssh", n) for n in machines), poll_s=poll_s, timeout_s=timeout_s
    )
    transports = {n: FakeTransport() for n in machines}
    clock, wall = Clock(), Clock(1_700_000_000.0)
    reader = FleetReader(
        fleet, transport_factory=lambda m: transports[m.name], clock=clock, wall=wall, **kw
    )
    return reader, transports, clock, wall


def entry(reader, name="a"):
    return next(m for m in reader.snapshot()["machines"] if m["name"] == name)


# -- classify (pure) ------------------------------------------------------------


def test_classify_never_read_is_unreachable():
    assert classify(now=10, seen=None, window=5, failing=False)[0] == "unreachable"


def test_classify_thresholds():
    assert classify(now=14, seen=10, window=5, failing=False) == ("ok", 4.0)
    assert classify(now=16, seen=10, window=5, failing=False) == ("stale", 6.0)
    # still answering (a frozen document): stale forever, never unreachable
    assert classify(now=100, seen=10, window=5, failing=False)[0] == "stale"
    # not answering: stale first, then unreachable after twice the window
    assert classify(now=16, seen=10, window=5, failing=True)[0] == "stale"
    assert classify(now=20.5, seen=10, window=5, failing=True)[0] == "unreachable"


# -- polling one box --------------------------------------------------------------


def test_good_poll_is_ok_with_thermal_fields():
    reader, t, clock, wall = make()
    t["a"].items = [doc(1.0, temp=61.5)]
    reader.poll_once("a")
    e = entry(reader)
    assert e["state"] == "ok"
    assert e["thermal"]["temp_c"] == 61.5
    assert e["thermal"]["admit"] is True
    assert e["thermal"]["concurrency_factor"] == 1.0
    assert "sensors" not in e["thermal"]
    assert e["health"]["last_poll"] == wall.t
    assert e["health"]["last_error"] is None


def test_missing_optional_fields_tolerated():
    reader, t, *_ = make()
    d = doc(1.0)
    for k in ("gpu_temp_c", "pair", "containers", "limits", "rate_c_s"):
        d.pop(k, None)
    t["a"].items = [d]
    reader.poll_once("a")
    e = entry(reader)
    assert e["state"] == "ok"
    assert e["thermal"]["gpu_temp_c"] is None
    assert e["thermal"]["pair"] is None


@pytest.mark.parametrize(
    "bad",
    [{"level": "unknown", "at": 1.0}, {"level": "cool"}, {"level": "cool", "at": float("nan")}],
)
def test_document_without_a_live_decision_is_a_failed_read(bad):
    reader, t, *_ = make()
    t["a"].items = [bad]
    reader.poll_once("a")
    e = entry(reader)
    assert e["state"] == "unreachable"
    assert e["health"]["last_error"]
    assert e["thermal"] is None


def test_transport_error_is_recorded_and_persists_after_recovery():
    reader, t, clock, wall = make()
    t["a"].items = [Unreachable("ssh exited 255: refused"), doc(1.0)]
    reader.poll_once("a")
    e = entry(reader)
    assert e["state"] == "unreachable"
    assert e["health"]["last_error"] == "ssh exited 255: refused"
    assert e["health"]["failures"] == 1
    wall.t += 10
    reader.poll_once("a")
    e = entry(reader)
    assert e["state"] == "ok"
    assert e["health"]["last_error"] == "ssh exited 255: refused"  # history of the link
    assert e["health"]["last_error_at"] == wall.t - 10
    assert e["health"]["failing"] is False


def test_stopped_box_goes_stale_then_unreachable_and_never_admits():
    reader, t, clock, _ = make(poll_s=10, timeout_s=5)  # window = 2*10 + 5 = 25
    t["a"].items = [doc(1.0)]
    reader.poll_once("a")
    t["a"].items = [Unreachable("refused")] * 10
    for step, want in [(10, "ok"), (30, "stale"), (45, "stale"), (60, "unreachable")]:
        clock.t = 1000.0 + step
        reader.poll_once("a")
        e = entry(reader)
        assert e["state"] == want, step
        if want != "ok":
            assert e["thermal"]["admit"] is None
            assert e["thermal"]["concurrency_factor"] is None
            assert e["thermal"]["temp_c"] == 50.0  # last known, labelled by state + age


def test_frozen_document_stays_stale_and_does_not_admit():
    reader, t, clock, _ = make()
    t["a"].items = [doc(1.0)]
    for _ in range(10):
        reader.poll_once("a")
        clock.t += 10
    e = entry(reader)
    assert e["state"] == "stale"
    assert e["age_s"] == 100.0
    assert e["thermal"]["admit"] is None


def test_peer_window_honoured_and_capped():
    reader, t, clock, _ = make(poll_s=1, timeout_s=1)  # own window 3 s
    t["a"].items = [doc(1.0, stale_after_s=60.0)]
    reader.poll_once("a")
    clock.t += 30
    assert entry(reader)["state"] == "ok"
    assert entry(reader)["stale_after_s"] == 60.0
    t["a"].items = [doc(2.0, stale_after_s=10_000.0)]
    reader.poll_once("a")
    assert entry(reader)["stale_after_s"] == 600.0


def test_age_measured_on_reader_clock_not_doc_clock():
    reader, t, clock, _ = make()
    t["a"].items = [doc(1e12)]  # a box whose clock is wildly ahead
    reader.poll_once("a")
    clock.t += 3
    assert entry(reader)["age_s"] == 3.0


# -- snapshot shape ---------------------------------------------------------------


def test_snapshot_is_json_and_carries_reader_health():
    reader, t, clock, wall = make(machines=("a", "b"))
    t["a"].items = [doc(1.0)]
    reader.poll_once("a")
    clock.t += 42  # uptime follows the monotonic clock, not wall time
    wall.t += 1000
    snap = json.loads(json.dumps(reader.snapshot()))
    assert snap["schema"] == 1
    assert snap["reader"]["uptime_s"] == 42.0
    assert snap["reader"]["poll_s"] == 10.0
    assert snap["reader"]["running"] is False
    assert [m["name"] for m in snap["machines"]] == ["a", "b"]
    b = snap["machines"][1]
    assert b["state"] == "unreachable"
    assert b["health"]["last_poll"] is None
    assert b["health"]["last_error"] == "not read yet"
    assert b["thermal"] is None
    assert set(snap["machines"][0]) >= {
        "name",
        "transport",
        "host",
        "state",
        "age_s",
        "stale_after_s",
        "health",
        "thermal",
    }


def test_reader_only_fetches_and_closes():
    reader, t, *_ = make()
    t["a"].items = [doc(1.0)]
    reader.poll_once("a")
    reader.snapshot()
    reader.history("a")
    reader.close()
    assert set(t["a"].calls) == {"fetch", "close"}


# -- threads ----------------------------------------------------------------------


def _wait_until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_one_thread_per_machine_and_hung_box_does_not_delay_others():
    names = ("hung", "b", "c")
    reader, t, clock, _ = make(machines=names, poll_s=0.02, timeout_s=1.0, stale_after_s=5.0)
    t["hung"].items = [doc(1.0)]
    for n in ("b", "c"):
        t[n].items = [doc(float(i)) for i in range(1, 10_000)]
    reader.poll_once("hung")  # one good reading, then it hangs
    gate = threading.Event()
    t["hung"].block = gate
    reader.start()
    try:
        threads = [th for th in threading.enumerate() if th.name.startswith("fleet-reader:")]
        assert sorted(th.name for th in threads) == [f"fleet-reader:{n}" for n in sorted(names)]
        assert all(th.daemon for th in threads)
        assert _wait_until(
            lambda: t["b"].calls.count("fetch") >= 5 and t["c"].calls.count("fetch") >= 5
        )
        assert t["hung"].calls.count("fetch") == 2  # the poll before start + the hung one
        assert entry(reader, "hung")["health"]["polling_for_s"] is not None
        clock.t += 1  # inside the window
        assert entry(reader, "hung")["state"] == "ok"
        clock.t += 5  # past the window: the hung poll exceeded its timeout -> not answering
        assert entry(reader, "hung")["state"] == "stale"
        clock.t += 5  # past twice the window
        e = entry(reader, "hung")
        assert e["state"] == "unreachable"
        assert e["thermal"]["admit"] is None
        # the others kept polling and caught up with the jumped clock
        assert _wait_until(lambda: entry(reader, "b")["state"] == "ok")
        start = time.monotonic()
        reader.stop()
        assert time.monotonic() - start < 1.5  # does not wait for the hung fetch
        assert reader.snapshot()["reader"]["running"] is False
    finally:
        gate.set()
    assert _wait_until(lambda: "close" in t["hung"].calls)  # it closes once it returns
    assert "close" in t["b"].calls


def test_start_is_idempotent_and_restartable():
    reader, t, *_ = make(poll_s=0.01)
    t["a"].items = [doc(float(i)) for i in range(1, 1000)]
    reader.start()
    reader.start()
    assert len([th for th in threading.enumerate() if th.name == "fleet-reader:a"]) == 1
    reader.stop()
    reader.stop()


def test_poller_survives_unexpected_exceptions():
    reader, t, *_ = make(poll_s=0.01)
    t["a"].items = [RuntimeError("boom")] * 3 + [doc(float(i)) for i in range(1, 1000)]
    reader.start()
    try:
        assert _wait_until(lambda: entry(reader)["state"] == "ok")
    finally:
        reader.stop()


# -- history ----------------------------------------------------------------------


def test_history_records_compact_samples_only_when_at_advances():
    reader, t, clock, wall = make()
    t["a"].items = [doc(1.0, temp=50), doc(1.0, temp=50), doc(2.0, "hot", temp=81.0)]
    for _ in range(3):
        reader.poll_once("a")
        wall.t += 10
    h = reader.history("a")
    assert len(h) == 2
    assert h[-1] == {
        "t": wall.t - 10,
        "temp_c": 81.0,
        "gpu_temp_c": 76.0,
        "level": "hot",
        "concurrency_factor": 0.0,
        "mem_used_pct": None,
    }


def test_history_handles_missing_gpu_and_since_filter():
    reader, t, clock, wall = make()
    for i in range(10):
        d = doc(float(i + 1))
        d.pop("gpu_temp_c")
        t["a"].items = [d]
        reader.poll_once("a")
        wall.t += 10
    assert reader.history("a")[0]["gpu_temp_c"] is None
    assert len(reader.history("a", since_s=35)) == 3  # samples at -30, -20, -10 s


def test_history_unknown_machine():
    reader, *_ = make()
    with pytest.raises(KeyError):
        reader.history("nope")


def test_history_capacity_is_24h_at_poll_interval():
    reader, *_ = make(poll_s=2.0)
    assert reader.capacity == HISTORY_S // 2 + 1


def test_ring_overwrites_oldest():
    r = Ring(3)
    for i in range(5):
        r.append(float(i), float(i), None, 0, 1.0)
    assert [s[0] for s in r.samples()] == [2.0, 3.0, 4.0]
    assert r.samples()[0][2] is None


def test_downsample_keeps_peaks():
    rows = [
        (float(i), float(i % 7), None, i % 4, 1.0 - (i % 4) / 4, 50.0 + i % 9) for i in range(1000)
    ]
    out = downsample(rows, 10)
    assert len(out) == 10
    assert all(r[1] == 6.0 and r[3] == 3 and r[4] == 0.25 and r[5] == 58.0 for r in out)
    assert out[-1][0] == 999.0
    assert downsample(rows[:5], 10) == rows[:5]


def test_history_downsampled_to_max_points():
    reader, t, clock, wall = make()
    for i in range(1000):
        t["a"].items = [doc(float(i + 1), temp=float(i))]
        reader.poll_once("a")
        wall.t += 1
    h = reader.history("a", max_points=300)
    assert len(h) == 300
    assert h[-1]["temp_c"] == 999.0


def test_week_of_19_machines_stays_under_100mb():
    poll_s = 2.0
    cap = HISTORY_S // int(poll_s) + 1
    week = 7 * 86_400 // int(poll_s)
    # a week of samples per box as columns, built before tracing (it is input, not retained)
    cols = (
        array("d", (i * poll_s for i in range(week))),
        array("d", [61.0]) * week,
        array("d", [56.0]) * week,
        array("b", [1]) * week,
        array("d", [0.5]) * week,
        array("d", [91.0]) * week,
    )
    tracemalloc.start()
    try:
        rings = [Ring(cap) for _ in range(19)]
        for r in rings:
            r.extend(*cols)
            r.append(week * poll_s, 62.0, None, 2, 0.0, 92.0)  # keeps polling past the cap
        current, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert all(len(r) == cap for r in rings)  # 24 h, not a week
    assert current < 100 * 1024 * 1024, current
    newest = rings[0].samples()
    assert newest[-1] == (week * poll_s, 62.0, None, 2, 0.0, 92.0)
    assert newest[0][0] == (week - cap + 1) * poll_s


def test_ring_extend_short_input_appends():
    r = Ring(5)
    r.append(0.0, 1.0, None, None, None)
    nan = float("nan")
    r.extend(
        array("d", [1.0, 2.0]),
        array("d", [2.0, nan]),
        array("d", [nan, 3.0]),
        array("b", [0, -1]),
        array("d", [1.0, nan]),
        array("d", [nan, 77.0]),
    )
    assert r.samples() == [
        (0.0, 1.0, None, None, None, None),
        (1.0, 2.0, None, 0, 1.0, None),
        (2.0, None, 3.0, None, None, 77.0),
    ]


# -- memory -----------------------------------------------------------------------


def mem_sensors(used=91.0, avail=11000, swap=9400, **zones) -> dict:
    s = {"mem:used_pct": used, "mem:avail_mib": avail, "mem:swap_used_mib": swap}
    s.update(zones)
    return s


def test_memory_object_derives_total_and_drops_raw_sensors():
    reader, t, *_ = make()
    t["a"].items = [doc(1.0, sensors=mem_sensors(**{"psi:memory_some_avg10": 1.5}))]
    reader.poll_once("a")
    th = reader.snapshot()["machines"][0]["thermal"]
    assert "sensors" not in th
    assert th["memory"] == {
        "used_pct": 91.0,
        "avail_mib": 11000,
        "total_mib": 122222,
        "swap_used_mib": 9400,
        "pressure_some_avg10": 1.5,
        "temp_c": None,
    }


def test_memory_null_without_mem_keys_and_total_null_at_full():
    assert extract(doc(1.0))["memory"] is None
    m = extract(doc(1.0, sensors=mem_sensors(used=100.0)))["memory"]
    assert m["total_mib"] is None and m["used_pct"] == 100.0
    m = extract(doc(1.0, sensors={"mem:used_pct": 40.0}))["memory"]
    assert m["avail_mib"] is None and m["total_mib"] is None and m["swap_used_mib"] is None


def test_memory_temp_is_max_over_memory_zones_not_pmic():
    zones = {
        "zone:8:ddrss-0-thermal": 45.7,
        "zone:18:ddrss-1-thermal": 44.9,
        "zone:25:pmm8650au_1_sdram": 48.7,
        "zone:3:cpu": 70.0,
    }
    assert extract(doc(1.0, sensors=mem_sensors(**zones)))["memory"]["temp_c"] == 45.7
    for z in ("zone:1:DRAM", "zone:2:emc", "zone:3:mem_thermal", "zone:4:ddr"):
        assert extract(doc(1.0, sensors=mem_sensors(**{z: 50.0})))["memory"]["temp_c"] == 50.0
    for z in ("zone:1:memristor", "zone:2:pmic_ddr", "zone:3:acpitz", "zone:4:cpu"):
        assert extract(doc(1.0, sensors=mem_sensors(**{z: 50.0})))["memory"]["temp_c"] is None


def test_history_carries_mem_used_pct():
    reader, t, clock, wall = make()
    t["a"].items = [doc(1.0, sensors=mem_sensors(used=88.0)), doc(2.0)]
    for _ in range(2):
        reader.poll_once("a")
        wall.t += 10
    assert [h["mem_used_pct"] for h in reader.history("a")] == [88.0, None]


# -- boundaries -------------------------------------------------------------------


def test_default_transport_by_machine_kind(tmp_path):
    fleet = Fleet(machines=(Machine("s", "ssh", "spark"), Machine("l", "local")), timeout_s=3)
    ssh = default_transport(fleet, state_dir=tmp_path)(fleet.machines[0])
    local = default_transport(fleet, state_dir=tmp_path)(fleet.machines[1])
    assert isinstance(ssh, SshTransport) and ssh.host == "spark" and ssh.timeout_s == 3
    assert isinstance(local, LocalTransport) and local.timeout_s == 3


def test_reader_does_not_import_daemon():
    code = (
        "import sys, heat_governor.fleet_reader; "
        "bad = [m for m in sys.modules if m in ('heat_governor.daemon', 'heat_governor.actuators',"
        " 'heat_governor.docker')]; print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )  # nosec B603
    assert proc.returncode == 0, proc.stdout + proc.stderr


# -- trend: records first, buffer fallback (d2) -----------------------------------


class HistoryTransport(FakeTransport):
    """A transport that also answers records-based history."""

    def __init__(self, answer=None, delay: float = 0.0):
        super().__init__()
        self.answer = answer
        self.delay = delay
        self.history_calls: list[tuple] = []

    def history(self, since: float, until: float, max_points: int) -> dict:
        self.history_calls.append((since, until, max_points))
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


REC = {"t": 1.0, "temp_c": 70.0, "gpu_temp_c": None, "level": "warm"}
REC.update({"concurrency_factor": 0.5, "mem_used_pct": 80.0})


def make_hist(answer, delay: float = 0.0, poll_s: float = 10.0):
    fleet = Fleet(machines=(Machine("a", "ssh", "a"),), poll_s=poll_s, timeout_s=5.0)
    t = HistoryTransport(answer, delay)
    clock, wall = Clock(), Clock(1_700_000_000.0)
    reader = FleetReader(fleet, transport_factory=lambda m: t, clock=clock, wall=wall)
    return reader, t, clock, wall


def test_trend_prefers_records_and_passes_the_window():
    reader, t, clock, wall = make_hist({"schema": 1, "samples": [REC]})
    out = reader.trend("a", since_s=3600, max_points=300)
    assert out == {"source": "records", "samples": [REC]}
    assert t.history_calls == [(wall.t - 3600, wall.t, 300)]


def test_trend_cache_hits_then_expires():
    reader, t, clock, wall = make_hist({"schema": 1, "samples": [REC]})
    reader.trend("a", since_s=3600)
    clock.t += 59
    reader.trend("a", since_s=3600)
    assert len(t.history_calls) == 1
    clock.t += 2  # past 60 s
    reader.trend("a", since_s=3600)
    assert len(t.history_calls) == 2
    # a long range is cached for 300 s
    reader.trend("a", since_s=7 * 86_400)
    clock.t += 299
    reader.trend("a", since_s=7 * 86_400)
    assert len(t.history_calls) == 3
    clock.t += 2
    reader.trend("a", since_s=7 * 86_400)
    assert len(t.history_calls) == 4


def test_trend_cache_is_bounded():
    reader, t, clock, wall = make_hist({"schema": 1, "samples": [REC]})
    for k in range(500):
        reader.trend("a", since_s=60 + k)
    assert len(reader._trend_cache) <= 64


def test_trend_falls_back_to_buffer_on_failure():
    reader, t, clock, wall = make_hist(Unreachable("ssh exited 255"))
    t.items = [doc(1.0, temp=55.0)]
    reader.poll_once("a")
    out = reader.trend("a", since_s=3600)
    assert out["source"] == "buffer"
    assert [s["temp_c"] for s in out["samples"]] == [55.0]
    # the failure is remembered: no new ssh call on every page refresh
    reader.trend("a", since_s=3600)
    assert len(t.history_calls) == 1


def test_trend_falls_back_when_records_are_empty_but_buffer_is_not():
    reader, t, clock, wall = make_hist({"schema": 1, "samples": []})
    out = reader.trend("a", since_s=3600)
    assert out == {"source": "records", "samples": []}  # nothing anywhere: records answer
    t.items = [doc(1.0, temp=57.0)]
    reader.poll_once("a")
    clock.t += 61
    out = reader.trend("a", since_s=3600)
    assert out["source"] == "buffer" and out["samples"][0]["temp_c"] == 57.0


def test_trend_without_transport_history_uses_buffer():
    reader, t, clock, wall = make()
    t["a"].items = [doc(1.0, temp=52.0)]
    reader.poll_once("a")
    out = reader.trend("a")
    assert out["source"] == "buffer"
    assert out["samples"][0]["temp_c"] == 52.0


def test_trend_unknown_machine():
    reader, *_ = make()
    with pytest.raises(KeyError):
        reader.trend("nope")


def test_trend_is_bounded_by_the_transport_and_does_not_hold_the_lock():
    # the transport's own deadline bounds the call; meanwhile polling and snapshots go on
    reader, t, clock, wall = make_hist({"schema": 1, "samples": [REC]}, delay=0.5)
    t.items = [doc(1.0)]
    done = threading.Event()
    th = threading.Thread(target=lambda: (reader.trend("a"), done.set()))
    start = time.monotonic()
    th.start()
    time.sleep(0.05)
    reader.poll_once("a")  # would deadlock if trend held the reader lock
    reader.snapshot()
    assert time.monotonic() - start < 0.4
    th.join(timeout=3)
    assert done.is_set()


# --- d3: GPU clock, GPU power/util and board power from the /state sensors (shapes live 2026-11-09)
GPU_SENSOR_SHAPES = {
    "cultureos": (
        {"gpu:3d00000.gpu.mhz": 479.0, "gpu:board_power_w": 15.04},
        {"gpu_mhz": 479.0, "gpu_power_w": None, "gpu_util_pct": None, "power_w": 15.04},
    ),
    "spark": (
        {
            "gpu:temp_c": 84.0,
            "gpu:power_w": 65.04,
            "gpu:sm_mhz": 2177.0,
            "gpu:gr_mhz": 2177.0,
            "gpu:util_pct": 96.0,
            "gpu:mem_mhz": None,
            "gpu:events": "0x0",
        },
        {"gpu_mhz": 2177.0, "gpu_power_w": 65.04, "gpu_util_pct": 96.0, "power_w": None},
    ),
    "thor": (
        {"gpu:gpu-gpc-0.mhz": 1575.0, "gpu:gpu-nvd-0.mhz": 315.0},
        {"gpu_mhz": 1575.0, "gpu_power_w": None, "gpu_util_pct": None, "power_w": None},
    ),
    "orin": (
        {"gpu:util_pct": 0.0, "gpu:17000000.gpu.mhz": 306.0},
        {"gpu_mhz": 306.0, "gpu_power_w": None, "gpu_util_pct": 0.0, "power_w": None},
    ),
    "none": ({}, {"gpu_mhz": None, "gpu_power_w": None, "gpu_util_pct": None, "power_w": None}),
}


@pytest.mark.parametrize("box", sorted(GPU_SENSOR_SHAPES))
def test_extract_carries_gpu_clock_and_power(box):
    sensors, want = GPU_SENSOR_SHAPES[box]
    doc = {"level": "cool", "at": 1.0, "temp_c": 50.0, "sensors": sensors}
    out = extract(doc)
    assert {k: out[k] for k in want} == want

"""PR #11 Qodo findings on the fleet reader, each pinned by a test written before its fix:

* #5 concurrent trend cache misses for one (machine, range, points) share ONE history read;
* #6 the snapshot carries ``fan_rpm`` and ``zones``, the facts the dashboard reads;
* #7 a stop + immediate restart while a fetch is hung never runs two pollers for one box;
* #8 a one-shot read never closes a transport while its fetch is still running.
"""

from __future__ import annotations

import threading
import time

from heat_governor.fleet import Fleet, Machine
from heat_governor.fleet_reader import FleetReader, extract
from tests.test_fleet_reader import REC, Clock, FakeTransport, _wait_until, doc, make

# -- #5 coalesce in-flight history reads ---------------------------------------------


class GatedHistory(FakeTransport):
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.history_calls = 0
        self._mu = threading.Lock()

    def history(self, since, until, max_points):
        with self._mu:
            self.history_calls += 1
        self.gate.wait(5)
        return {"samples": [REC]}


def test_concurrent_trend_misses_share_one_history_read():
    fleet = Fleet(machines=(Machine("a", "ssh", "a"),), poll_s=10.0, timeout_s=5.0)
    t = GatedHistory()
    reader = FleetReader(
        fleet, transport_factory=lambda m: t, clock=Clock(), wall=Clock(1_700_000_000.0)
    )
    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(reader.trend("a", 86_400, 300)))
        for _ in range(4)
    ]
    for th in threads:
        th.start()
    assert _wait_until(lambda: t.history_calls >= 1)
    time.sleep(0.1)  # give the followers time to (wrongly) start their own reads
    t.gate.set()
    for th in threads:
        th.join(5)
    assert t.history_calls == 1
    assert len(results) == 4
    assert all(r == {"source": "records", "samples": [REC]} for r in results)


def test_different_ranges_are_not_coalesced():
    fleet = Fleet(machines=(Machine("a", "ssh", "a"),), poll_s=10.0, timeout_s=5.0)
    t = GatedHistory()
    t.gate.set()
    reader = FleetReader(
        fleet, transport_factory=lambda m: t, clock=Clock(), wall=Clock(1_700_000_000.0)
    )
    reader.trend("a", 3_600, 300)
    reader.trend("a", 86_400, 300)
    assert t.history_calls == 2


# -- #6 fan and zone facts -------------------------------------------------------------


def test_extract_carries_fan_rpm_from_fan_inputs_not_pwm():
    d = doc(1.0, sensors={"fan:emc2305.pwm1": 128.0, "fan:emc2305.fan1_input": 3248.0})
    assert extract(d)["fan_rpm"] == 3248.0


def test_extract_fan_rpm_is_the_fastest_fan():
    d = doc(1.0, sensors={"fan:a.fan1_input": 1200.0, "fan:a.fan2_input": 4166.0})
    assert extract(d)["fan_rpm"] == 4166.0


def test_extract_fan_rpm_null_with_only_pwm_or_no_fans():
    assert extract(doc(1.0, sensors={"fan:pwmfan.pwm1": 71.0}))["fan_rpm"] is None
    assert extract(doc(1.0, sensors={}))["fan_rpm"] is None
    assert extract({k: v for k, v in doc(1.0).items() if k != "sensors"})["fan_rpm"] is None


def test_extract_zones_counts_readable_zone_sensors():
    s = {"zone:0:cpu": 50.0, "zone:1:gpu": 48.0, "zone:2:dead": None, "fan:a.fan1_input": 1.0}
    assert extract(doc(1.0, sensors=s))["zones"] == 2
    assert extract(doc(1.0, sensors={}))["zones"] is None


def test_snapshot_serves_fan_and_zones():
    reader, t, *_ = make()
    t["a"].items = [doc(1.0, sensors={"zone:0:cpu": 50.0, "fan:x.fan1_input": 2000.0})]
    reader.poll_once("a")
    th = reader.snapshot()["machines"][0]["thermal"]
    assert th["fan_rpm"] == 2000.0
    assert th["zones"] == 1


# -- #7 no duplicate pollers after a restart --------------------------------------------


class Concurrency(FakeTransport):
    """Counts how many fetches run at once."""

    def __init__(self, items):
        super().__init__(items)
        self.active = 0
        self.peak = 0
        self._mu = threading.Lock()

    def fetch(self):
        with self._mu:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            return super().fetch()
        finally:
            with self._mu:
                self.active -= 1


def test_restart_while_a_fetch_is_hung_never_runs_two_pollers():
    fleet = Fleet(machines=(Machine("a", "ssh", "a"),), poll_s=0.01, timeout_s=1.0)
    t = Concurrency([doc(float(i)) for i in range(1, 10_000)])
    reader = FleetReader(fleet, transport_factory=lambda m: t)
    gate = threading.Event()
    t.block = gate
    reader.start()
    assert _wait_until(lambda: t.active == 1)  # the first poll hangs
    reader.stop(join_s=0.05)  # returns with the fetch still hung
    reader.start()
    time.sleep(0.2)
    assert t.peak == 1  # the new generation waits for the old poller
    t.block = None
    gate.set()
    before = t.calls.count("fetch")
    assert _wait_until(lambda: t.calls.count("fetch") > before + 5)  # the new one polls
    assert t.peak == 1
    assert "close" not in t.calls  # the old poller left the live transport open
    reader.close()
    assert len([th for th in threading.enumerate() if th.name == "fleet-reader:a"]) <= 1


# -- #8 a one-shot read never closes a busy transport -----------------------------------


class Watched(FakeTransport):
    """Fails the test if close() runs while a fetch is active."""

    def __init__(self, items):
        super().__init__(items)
        self.fetching = False
        self.closed_while_fetching = False

    def fetch(self):
        self.fetching = True
        try:
            return super().fetch()
        finally:
            self.fetching = False

    def close(self):
        if self.fetching:
            self.closed_while_fetching = True
        super().close()


def test_poll_all_is_bounded_and_close_waits_for_a_hung_fetch():
    fleet = Fleet(
        machines=(Machine("hung", "ssh", "hung"), Machine("ok", "ssh", "ok")),
        poll_s=10.0,
        timeout_s=1.0,
    )
    ts = {"hung": Watched([doc(1.0)]), "ok": Watched([doc(1.0)])}
    gate = threading.Event()
    ts["hung"].block = gate
    reader = FleetReader(fleet, transport_factory=lambda m: ts[m.name])
    start = time.monotonic()
    reader.poll_all(join_s=0.1)
    snap = reader.snapshot()
    reader.close()
    assert time.monotonic() - start < 1.0  # the hung read does not hold the command
    assert {m["name"]: m["state"] for m in snap["machines"]}["ok"] == "ok"
    assert "close" in ts["ok"].calls
    assert "close" not in ts["hung"].calls  # still fetching: left to its thread
    gate.set()
    assert _wait_until(lambda: "close" in ts["hung"].calls)  # closed once the read ended
    assert not ts["hung"].closed_while_fetching
    assert not ts["ok"].closed_while_fetching


def test_direct_snapshot_uses_tracked_reads(monkeypatch):
    from heat_governor.cli._commands import mesh

    fleet = Fleet(machines=(Machine("hung", "ssh", "hung"),), poll_s=10.0, timeout_s=0.1)
    t = Watched([doc(1.0)])
    gate = threading.Event()
    t.block = gate
    monkeypatch.setattr(
        mesh, "_make_reader", lambda f: FleetReader(f, transport_factory=lambda m: t)
    )
    try:
        mesh._direct_snapshot(fleet)
        assert "close" not in t.calls
    finally:
        gate.set()
    assert _wait_until(lambda: "close" in t.calls)
    assert not t.closed_while_fetching

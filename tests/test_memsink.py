"""MemorySink: the guard thread hands events off without ever blocking on disk (review #5)."""

import threading
import time

from heat_governor.memsink import MemorySink


def test_submit_never_blocks_while_the_handler_is_stuck():
    release = threading.Event()
    handled: list[dict] = []

    def slow(ev):
        release.wait(5)  # a recorder fsync stuck under memory pressure
        handled.append(ev)

    sink = MemorySink(slow)
    sink.start()
    try:
        t0 = time.monotonic()
        for i in range(50):
            sink.submit({"event": "oom_kill", "n": i})
        assert time.monotonic() - t0 < 0.1
    finally:
        release.set()
        sink.stop(timeout=5)
    assert [e["n"] for e in handled] == list(range(50))


def test_stop_drains_everything_queued():
    handled: list[dict] = []
    sink = MemorySink(handled.append)
    sink.start()
    for i in range(10):
        sink.submit({"n": i})
    sink.stop(timeout=5)
    assert len(handled) == 10
    assert not sink.running


def test_full_queue_drops_and_counts_and_logs_once():
    release = threading.Event()
    logs: list[str] = []
    sink = MemorySink(lambda ev: release.wait(5), maxsize=2, log=logs.append)
    sink.start()
    try:
        for i in range(20):
            sink.submit({"n": i})
        assert sink.dropped > 0
        assert len([m for m in logs if "dropped" in m]) == 1
    finally:
        release.set()
        sink.stop(timeout=5)


def test_handler_errors_are_logged_and_the_sink_keeps_going():
    logs: list[str] = []
    handled: list[dict] = []

    def flaky(ev):
        if ev["n"] == 1:
            raise OSError("disk full")
        handled.append(ev)

    sink = MemorySink(flaky, log=logs.append)
    sink.start()
    for i in range(3):
        sink.submit({"n": i})
    sink.stop(timeout=5)
    assert [e["n"] for e in handled] == [0, 2]
    assert any("disk full" in m for m in logs)


def test_not_started_runs_inline():
    handled: list[dict] = []
    sink = MemorySink(handled.append)
    sink.submit({"n": 1})
    assert handled == [{"n": 1}]


def test_flush_waits_for_queued_events():
    handled: list[dict] = []
    sink = MemorySink(lambda ev: (time.sleep(0.01), handled.append(ev)))
    sink.start()
    try:
        for i in range(5):
            sink.submit({"n": i})
        assert sink.flush(timeout=5)
        assert len(handled) == 5
    finally:
        sink.stop(timeout=5)

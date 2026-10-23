"""The published state and the client that reads it (file and HTTP)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from heat_governor import client
from heat_governor.state import StateServer, default_state_path, read_state, write_state


def _doc(**kw) -> dict:
    base = {
        "schema": 1,
        "at": time.time(),
        "stale_after_s": 5,
        "level": "cool",
        "admit": True,
        "concurrency_factor": 1.0,
        "temp_c": 70.0,
        "reason": "steady",
    }
    return base | kw


def test_write_is_atomic_and_readable(tmp_path: Path) -> None:
    p = tmp_path / "s" / "state.json"
    write_state(p, _doc())
    assert read_state(p)["level"] == "cool"
    assert not list(p.parent.glob("*.tmp"))
    assert oct(p.stat().st_mode)[-3:] == "644"


def test_default_path_env(tmp_path: Path) -> None:
    assert default_state_path({"HEAT_GOVERNOR_STATE": "/x/y.json"}) == Path("/x/y.json")
    assert default_state_path({"XDG_RUNTIME_DIR": "/run/user/7"}) == Path(
        "/run/user/7/heat-governor/state.json"
    )


def test_missing_state_admits_by_default(tmp_path: Path) -> None:
    adm = client.read(str(tmp_path / "none.json"))
    assert adm.admit and adm.level == "unknown" and adm.reason == "no-state"
    assert not client.read(str(tmp_path / "none.json"), if_unknown="hold").admit


def test_stale_state_is_unknown(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    write_state(p, _doc(at=time.time() - 60, admit=False, level="hot"))
    adm = client.read(str(p))
    assert adm.level == "unknown" and adm.reason == "stale" and adm.admit


def test_fresh_hot_state_holds(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    write_state(p, _doc(level="hot", admit=False, concurrency_factor=0.0))
    adm = client.read(str(p))
    assert not adm.admit and adm.allowed(4) == 0


def test_allowed_concurrency() -> None:
    warm = client.Admission(True, "warm", 0.5, "", 88.0, None)
    assert warm.allowed(4) == 2
    assert warm.allowed(1) == 1


def test_wait_times_out(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    write_state(p, _doc(level="hot", admit=False, concurrency_factor=0.0))
    with pytest.raises(TimeoutError):
        client.wait(str(p), timeout=0.2, poll_s=0.05)


def test_wait_returns_when_cooled(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    write_state(p, _doc(level="hot", admit=False, concurrency_factor=0.0))
    threading.Timer(0.2, lambda: write_state(p, _doc())).start()
    assert client.wait(str(p), timeout=5, poll_s=0.05).admit


def test_gate_limits_in_flight(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    write_state(p, _doc(level="warm", concurrency_factor=0.5))
    gate = client.Gate(4, str(p), poll_s=0.05)
    peak = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal peak
        with gate.slot():
            with lock:
                peak = max(peak, gate.in_flight)
            time.sleep(0.1)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2


def test_http_source() -> None:
    srv = StateServer("127.0.0.1", 0)
    srv.doc = _doc(level="warm", concurrency_factor=0.5)
    srv.start()
    try:
        host, port = srv.address
        adm = client.read(f"http://{host}:{port}/state")
        assert adm.level == "warm"
    finally:
        srv.stop()

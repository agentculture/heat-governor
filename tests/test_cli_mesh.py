"""``heat-governor mesh``: overview, status (server + direct fallback) and serve."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from heat_governor.cli import main
from heat_governor.cli._commands import mesh
from heat_governor.explain import known_paths
from heat_governor.fleet import Fleet, Machine
from heat_governor.fleet_reader import FleetReader
from heat_governor.mesh_server import MeshServer

FLEET_TOML = """\
poll_s = 1
timeout_s = 1
[[machines]]
name = "cool"
host = "cool.example"
[[machines]]
name = "hot"
host = "hot.example"
[[machines]]
name = "dead"
host = "dead.example"
"""


def doc(temp: float, level: str = "cool") -> dict:
    return {
        "level": level,
        "at": time.time(),
        "temp_c": temp,
        "admit": level in ("cool", "warm"),
        "version": "9.9.9",
        "concurrency_factor": 1.0,
    }


class FakeTransport:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def fetch(self) -> dict:
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        if callable(self.behaviour):
            return self.behaviour()
        return dict(self.behaviour)

    def close(self) -> None:
        pass


def factory_for(table: dict):
    return lambda m: FakeTransport(table[m.name])


@pytest.fixture()
def fleet_file(tmp_path: Path) -> Path:
    p = tmp_path / "fleet.toml"
    p.write_text(FLEET_TOML)
    return p


@pytest.fixture()
def fakes(monkeypatch):
    table = {
        "cool": doc(40.0),
        "hot": doc(88.0, "hot"),
        "dead": OSError("ssh exited 255: no route"),
    }
    monkeypatch.setattr(mesh, "transport_factory", factory_for(table))
    return table


NO_SERVER = "http://127.0.0.1:1"


def run(capsys, *argv):
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_status_direct_json(fleet_file, fakes, capsys):
    rc, out, err = run(
        capsys, "mesh", "status", "--fleet", str(fleet_file), "--url", NO_SERVER, "--json"
    )
    assert rc == 0 and err == ""
    st = json.loads(out)
    assert st["source"] == "direct"
    names = [m["name"] for m in st["machines"]]
    assert names == ["dead", "hot", "cool"]  # most severe first
    by = {m["name"]: m for m in st["machines"]}
    assert by["hot"]["state"] == "ok" and by["hot"]["level"] == "hot"
    assert by["hot"]["admit"] is False and by["cool"]["admit"] is True
    assert by["cool"]["age_s"] is not None and by["cool"]["age_s"] < 10
    assert by["cool"]["version"] == "9.9.9" and by["cool"]["temp_c"] == 40.0
    assert by["dead"]["state"] == "unreachable" and by["dead"]["admit"] is None
    assert "no route" in by["dead"]["last_error"]
    assert st["reader"]["timeout_s"] == 1


def test_status_direct_text(fleet_file, fakes, capsys):
    rc, out, _ = run(capsys, "mesh", "status", "--fleet", str(fleet_file), "--url", NO_SERVER)
    assert rc == 0
    lines = out.strip().splitlines()
    assert len(lines) == 4 and lines[0].startswith("dead")
    assert "age" in lines[1] and "88.0C" in lines[1]
    assert lines[-1].startswith("reader: source direct")


def test_status_direct_is_concurrent_and_bounded(fleet_file, monkeypatch, capsys):
    monkeypatch.setattr(
        mesh, "transport_factory", lambda m: FakeTransport(lambda: (time.sleep(3), doc(1.0))[1])
    )
    t0 = time.monotonic()
    rc, out, _ = run(
        capsys, "mesh", "status", "--fleet", str(fleet_file), "--url", NO_SERVER, "--json"
    )
    assert time.monotonic() - t0 < 2.8  # timeout_s + 1, not 3 boxes x 3 s
    assert rc == 2  # nobody answered in time
    assert all(m["state"] == "unreachable" for m in json.loads(out)["machines"])


def test_status_via_server(fakes, capsys):
    fleet = Fleet((Machine("a", host="a.example"), Machine("b", host="b.example")), 1.0, 1.0)
    reader = FleetReader(fleet, transport_factory=factory_for({"a": doc(50.0), "b": doc(60.0)}))
    for m in fleet.machines:
        reader.poll_once(m.name)
    server = MeshServer(reader, host="127.0.0.1", port=0)
    server.start()
    try:
        url = f"http://127.0.0.1:{server.address[1]}"
        rc, out, err = run(
            capsys, "mesh", "status", "--url", url, "--fleet", "/nonexistent", "--json"
        )
    finally:
        server.stop()
        reader.close()
    assert rc == 0 and err == ""
    st = json.loads(out)
    assert st["source"] == "mesh serve" and st["url"] == url
    assert [m["name"] for m in st["machines"]] == ["b", "a"]
    assert all(m["age_s"] is not None for m in st["machines"])


def test_status_missing_fleet_and_no_server_is_exit_2(tmp_path, capsys):
    rc, out, err = run(
        capsys,
        "mesh",
        "status",
        "--fleet",
        str(tmp_path / "none.toml"),
        "--url",
        NO_SERVER,
        "--json",
    )
    assert rc == 2 and out == ""
    assert "fleet.example.toml" in json.loads(err)["remediation"]


def test_status_bad_fleet_is_exit_1(tmp_path, capsys):
    bad = tmp_path / "fleet.toml"
    bad.write_text("machines = 3\n")
    rc, out, err = run(capsys, "mesh", "status", "--fleet", str(bad), "--url", NO_SERVER)
    assert rc == 1 and out == "" and "hint:" in err


def test_serve_missing_fleet_is_exit_1(tmp_path, capsys):
    rc, out, err = run(capsys, "mesh", "serve", "--fleet", str(tmp_path / "none.toml"))
    assert rc == 1 and out == "" and "fleet.example.toml" in err


def test_serve_bad_listen_is_exit_1(fleet_file, capsys):
    rc, _, err = run(capsys, "mesh", "serve", "--fleet", str(fleet_file), "--listen", "nope")
    assert rc == 1 and "HOST:PORT" in err


def test_serve_core_binds_loopback_by_default(fakes):
    fleet = Fleet((Machine("cool", host="cool.example"),), 1.0, 1.0)
    reader, server = mesh.start_mesh(fleet, "127.0.0.1:0")
    try:
        assert server.address[0] == "127.0.0.1"
        assert reader.running
    finally:
        mesh.stop_mesh(reader, server)
    assert not reader.running


def test_serve_default_address(monkeypatch):
    seen = {}

    class Spy:
        def __init__(self, reader, host, port):
            seen["addr"] = (host, port)
            raise OSError(98, "in use")

    monkeypatch.setattr(mesh, "MeshServer", Spy)
    monkeypatch.setattr(mesh, "transport_factory", lambda m: FakeTransport({}))
    with pytest.raises(mesh.CliError) as ei:
        mesh.start_mesh(Fleet((Machine("a", host="a.example"),), 1.0, 1.0))
    assert seen["addr"] == ("127.0.0.1", 9480) and ei.value.code == 2


@pytest.mark.parametrize("json_mode", [False, True])
def test_serve_runs_and_shuts_down(fleet_file, fakes, monkeypatch, capsys, json_mode):
    state = {}

    def fake_wait():
        state["running"] = True

    monkeypatch.setattr(mesh, "_wait_for_signal", fake_wait)
    argv = ["mesh", "serve", "--fleet", str(fleet_file), "--listen", "127.0.0.1:0"]
    rc, out, err = run(capsys, *argv, *(["--json"] if json_mode else []))
    assert rc == 0 and err == "" and state["running"]
    if json_mode:
        info = json.loads(out)
        assert info["host"] == "127.0.0.1" and info["machines"] == 3
        assert info["serving"].startswith("http://127.0.0.1:")
    else:
        assert "http://127.0.0.1:" in out


@pytest.mark.parametrize("json_mode", [False, True])
def test_overview(capsys, json_mode):
    rc, out, err = run(capsys, "mesh", "overview", *(["--json"] if json_mode else []))
    assert rc == 0 and err == ""
    assert "mesh serve" in out and "mesh status" in out and "9480" in out
    assert "fleet.toml" in out
    if json_mode:
        assert json.loads(out)["subject"] == "heat-governor mesh"


def test_bare_mesh_prints_overview(capsys):
    rc, out, _ = run(capsys, "mesh")
    assert rc == 0 and "mesh status" in out


def test_catalog_learn_overview_list_mesh(capsys):
    paths = known_paths()
    for verb in ("overview", "status", "serve"):
        assert ("mesh", verb) in paths
    assert ("mesh",) in paths
    _, learn, _ = run(capsys, "learn")
    assert "mesh" in learn and "status" in learn
    _, lj, _ = run(capsys, "learn", "--json")
    cmds = [c["path"] for c in json.loads(lj)["commands"]]
    assert ["mesh", "status"] in cmds and ["mesh", "serve"] in cmds
    _, ov, _ = run(capsys, "overview")
    assert "mesh" in ov
    rc, out, _ = run(capsys, "explain", "mesh", "status")
    assert rc == 0 and "mesh status" in out

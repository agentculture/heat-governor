"""``heat-governor mcp``: a read-only MCP server over newline-delimited JSON-RPC on stdio."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from heat_governor import mcp_server
from heat_governor.cli import main
from heat_governor.cli._commands import mesh
from heat_governor.explain import known_paths

FLEET_TOML = """\
poll_s = 1
timeout_s = 1
[[machines]]
name = "cool"
host = "cool.example"
[[machines]]
name = "dead"
host = "dead.example"
"""


class FakeTransport:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def fetch(self) -> dict:
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return dict(self.behaviour)

    def close(self) -> None:
        pass


@pytest.fixture()
def fleet_file(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "fleet.toml"
    p.write_text(FLEET_TOML)
    table = {
        "cool": {
            "level": "cool",
            "at": time.time(),
            "temp_c": 40.0,
            "admit": True,
            "version": "9.9.9",
            "concurrency_factor": 1.0,
        },
        "dead": OSError("ssh exited 255"),
    }
    monkeypatch.setattr(mesh, "transport_factory", lambda m: FakeTransport(table[m.name]))
    return p


NO_SERVER = "http://127.0.0.1:1"


def session(*messages, raw: list[str] | None = None) -> tuple[list[dict], str, int]:
    lines = [json.dumps(m) for m in messages] + (raw or [])
    out, err = io.StringIO(), io.StringIO()
    rc = mcp_server.serve(io.StringIO("\n".join(lines) + "\n"), out, err)
    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    return frames, err.getvalue(), rc


def call(name, arguments=None, id=1):
    return {
        "jsonrpc": "2.0",
        "id": id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def one(msg):
    return mcp_server.handle(msg)


def test_initialize_echoes_supported_version():
    r = one(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    assert r["id"] == 7 and r["jsonrpc"] == "2.0"
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert r["result"]["capabilities"] == {"tools": {}}
    assert r["result"]["serverInfo"]["name"] == "heat-governor"
    assert r["result"]["serverInfo"]["version"]


def test_initialize_unknown_version_gets_default():
    r = one({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1"}})
    assert r["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_get_no_reply():
    assert one({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert one({"jsonrpc": "2.0", "method": "whatever/unknown"}) is None


def test_ping():
    assert one({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"] == {}


def test_tools_list_names_and_schemas():
    r = one({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {t["name"]: t for t in r["result"]["tools"]}
    assert set(tools) == {"mesh_status", "machine_state", "sense"}
    for t in tools.values():
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"
    assert tools["machine_state"]["inputSchema"]["required"] == ["name"]


def test_unknown_method_is_32601():
    r = one({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert r["error"]["code"] == -32601 and r["id"] == 4


@pytest.mark.parametrize(
    "msg",
    [
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": 3},
        [1, 2],
    ],
)
def test_invalid_request_is_32600(msg):
    r = one(msg)
    assert r["error"]["code"] == -32600


def test_mesh_status_direct(fleet_file):
    r = one(call("mesh_status", {"fleet": str(fleet_file), "url": NO_SERVER}))["result"]
    assert r["isError"] is False
    sc = r["structuredContent"]
    assert json.loads(r["content"][0]["text"]) == sc
    assert sc["source"] == "direct"
    rows = {m["name"]: m for m in sc["machines"]}
    assert rows["cool"]["state"] == "ok" and rows["cool"]["admit"] is True
    assert rows["dead"]["state"] != "ok" and rows["dead"]["admit"] is None


def test_mesh_status_matches_cli(fleet_file, capsys):
    mcp = one(call("mesh_status", {"fleet": str(fleet_file), "url": NO_SERVER}))
    main(["mesh", "status", "--json", "--fleet", str(fleet_file), "--url", NO_SERVER])
    cli = json.loads(capsys.readouterr().out)
    got = mcp["result"]["structuredContent"]
    assert [m["name"] for m in got["machines"]] == [m["name"] for m in cli["machines"]]
    assert got["source"] == cli["source"]


def test_mesh_status_prefers_server(monkeypatch):
    snap = {"reader": {"poll_s": 1}, "machines": [{"name": "x", "state": "ok", "age_s": 0.1}]}
    monkeypatch.setattr(mesh, "_fetch_server", lambda url: snap)
    sc = one(call("mesh_status"))["result"]["structuredContent"]
    assert sc["source"] == "mesh serve" and sc["machines"][0]["name"] == "x"


def test_mesh_status_without_any_source_is_tool_error(tmp_path, monkeypatch):
    def boom(url):
        raise OSError("down")

    monkeypatch.setattr(mesh, "_fetch_server", boom)
    r = one(call("mesh_status", {"fleet": str(tmp_path / "missing.toml")}))
    assert "error" not in r and r["result"]["isError"] is True
    assert "no fleet file" in r["result"]["content"][0]["text"]


def test_machine_state(fleet_file):
    r = one(call("machine_state", {"name": "cool", "fleet": str(fleet_file), "url": NO_SERVER}))
    sc = r["result"]["structuredContent"]
    assert r["result"]["isError"] is False
    assert sc["name"] == "cool" and sc["state"] == "ok"
    assert "health" in sc and sc["thermal"]["temp_c"] == 40.0


def test_machine_state_unknown_name_is_tool_error(fleet_file):
    r = one(call("machine_state", {"name": "nope", "fleet": str(fleet_file), "url": NO_SERVER}))
    assert r["result"]["isError"] is True
    assert "nope" in r["result"]["content"][0]["text"]


def test_machine_state_requires_name():
    r = one(call("machine_state", {}))
    assert r["result"]["isError"] is True


def test_sense_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("HEAT_GOVERNOR_CONFIG", str(tmp_path / "none.toml"))
    r = one(call("sense"))["result"]
    assert r["isError"] is False
    assert "platform" in r["structuredContent"]


def test_unknown_tool_and_bad_params():
    assert one(call("nope"))["result"]["isError"] is True
    bad = one({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    assert bad["error"]["code"] == -32602
    badargs = one(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "sense", "arguments": 3},
        }
    )
    assert badargs["error"]["code"] == -32602


def test_tool_crash_is_tool_error(monkeypatch):
    def boom(args):
        raise RuntimeError("kaput")

    monkeypatch.setattr(mesh, "read_snapshot", boom)
    r = one(call("mesh_status"))
    assert r["result"]["isError"] is True and "kaput" in r["result"]["content"][0]["text"]


def test_scripted_session_stdout_is_only_frames(fleet_file):
    frames, err, rc = session(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        call("mesh_status", {"fleet": str(fleet_file), "url": NO_SERVER}, id=3),
        {"jsonrpc": "2.0", "id": 4, "method": "bogus"},
        raw=["", "{not json"],
    )
    assert rc == 0
    assert [f.get("id") for f in frames] == [1, 2, 3, 4, None]
    assert all(f["jsonrpc"] == "2.0" for f in frames)
    assert frames[-1]["error"]["code"] == -32700
    assert frames[3]["error"]["code"] == -32601
    assert "parse error" in err.lower() or "malformed" in err.lower()


def test_serve_survives_internal_failure(monkeypatch):
    def boom(msg):
        raise RuntimeError("bug")

    monkeypatch.setattr(mcp_server, "handle", boom)
    frames, err, rc = session({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert rc == 0 and frames[0]["error"]["code"] == -32603 and frames[0]["id"] == 9
    assert "bug" in err


def test_batch_is_answered_per_item():
    frames, _, _ = session(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
    )
    assert frames == [[{"jsonrpc": "2.0", "id": 1, "result": {}}]]


def test_cli_verb_is_cataloged():
    assert ("mcp",) in known_paths()


def test_cli_mcp_runs_serve_on_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'))
    assert main(["mcp", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tty_stdin_hints_and_exits(monkeypatch, capsys):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty(""))
    assert main(["mcp"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "MCP" in out.err


def test_help_returns(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["mcp", "--help"])
    assert exc.value.code == 0
    assert "--json" in capsys.readouterr().out


def test_subprocess_session():
    script = "\n".join(
        json.dumps(m)
        for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    )
    p = subprocess.run(
        [sys.executable, "-m", "heat_governor", "mcp"],
        input=script + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert p.returncode == 0
    frames = [json.loads(line) for line in p.stdout.splitlines()]
    assert [f["id"] for f in frames] == [1, 2]
    assert len(frames[1]["result"]["tools"]) == 3

"""Read-only MCP server over stdio: newline-delimited JSON-RPC 2.0, stdlib only.

``handle`` is the pure, testable core (one message in, one reply or ``None`` out);
``serve`` is the thin read-a-line / write-a-line loop around it. The protocol owns stdout:
every diagnostic goes to the stderr stream. Tools only read: ``mesh_status`` and
``machine_state`` use the same server-then-direct fleet read as ``mesh status``; ``sense``
is this machine's own sensor reading. None of them pauses, caps, applies or edits anything.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, TextIO

from heat_governor import __version__

DEFAULT_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_FLEET_PROPS = {
    "fleet": {"type": "string", "description": "fleet.toml path (default: beside config.toml)."},
    "url": {"type": "string", "description": "mesh serve to ask first (default loopback :9480)."},
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "mesh_status",
        "description": (
            "Every machine's state, age, temp, level and admission, hottest last-known first. "
            "Asks a running mesh serve, else reads every machine once. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": dict(_FLEET_PROPS)},
    },
    {
        "name": "machine_state",
        "description": (
            "One machine's full snapshot entry (state, age_s, health, thermal) from the same "
            "source as mesh_status. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Machine name from the fleet file."},
                **_FLEET_PROPS,
            },
            "required": ["name"],
        },
    },
    {
        "name": "sense",
        "description": (
            "This machine's own sensor reading (thermal zones, fans, GPU), the same data as "
            "'heat-governor sense --json'. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class ToolError(Exception):
    """A tool failure: reported as ``isError`` content, not a JSON-RPC error."""


def _reply(id_: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_result(payload: Any, is_error: bool = False) -> dict:
    if is_error:
        return {"content": [{"type": "text", "text": str(payload)}], "isError": True}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": False,
    }


def _fleet_args(arguments: dict) -> argparse.Namespace:
    for key in ("fleet", "url"):
        v = arguments.get(key)
        if v is not None and not isinstance(v, str):
            raise ToolError(f"'{key}' must be a string")
    return argparse.Namespace(fleet=arguments.get("fleet"), url=arguments.get("url"))


def _tool_mesh_status(arguments: dict) -> dict:
    from heat_governor.cli._commands import mesh

    snapshot, source, url = mesh.read_snapshot(_fleet_args(arguments))
    return mesh.build_status(snapshot, source, url)


def _tool_machine_state(arguments: dict) -> dict:
    from heat_governor.cli._commands import mesh

    name = arguments.get("name")
    if not isinstance(name, str) or not name:
        raise ToolError("'name' is required (a machine name from the fleet file)")
    snapshot, _source, _url = mesh.read_snapshot(_fleet_args(arguments))
    machines = snapshot.get("machines", [])
    for m in machines:
        if m.get("name") == name:
            return m
    known = ", ".join(str(m.get("name")) for m in machines) or "none"
    raise ToolError(f"unknown machine {name!r}; known: {known}")


def _tool_sense(arguments: dict) -> dict:
    from heat_governor.cli._commands import governing

    _prof, _reading, doc = governing.sense_reading(argparse.Namespace(platform=None, config=None))
    return doc


_HANDLERS = {
    "mesh_status": _tool_mesh_status,
    "machine_state": _tool_machine_state,
    "sense": _tool_sense,
}


def _call_tool(params: dict) -> dict:
    handler = _HANDLERS.get(params["name"])
    if handler is None:
        return _tool_result(f"unknown tool {params['name']!r}", is_error=True)
    try:
        return _tool_result(handler(params["arguments"]))
    except ToolError as exc:
        return _tool_result(str(exc), is_error=True)
    # CliError and anything else: report, keep serving
    except Exception as exc:  # noqa: BLE001
        message = getattr(exc, "message", None) or f"{exc.__class__.__name__}: {exc}"
        hint = getattr(exc, "remediation", None)
        return _tool_result(f"{message} ({hint})" if hint else message, is_error=True)


def handle(message: Any) -> dict | None:
    """One JSON-RPC message in; the reply out, or ``None`` for a notification."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "request must be a JSON object")
    id_ = message.get("id")
    is_notification = "id" not in message
    method = message.get("method")
    params = message.get("params", {})
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(id_, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    if not isinstance(params, dict):
        return None if is_notification else _error(id_, INVALID_REQUEST, "params must be an object")
    if is_notification:
        return None
    return _dispatch_method(id_, method, params)


def _dispatch_method(id_: Any, method: str, params: dict) -> dict:
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _reply(
            id_,
            {
                "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "heat-governor", "version": __version__},
            },
        )
    if method == "ping":
        return _reply(id_, {})
    if method == "tools/list":
        return _reply(id_, {"tools": TOOLS})
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(id_, INVALID_PARAMS, "tools/call needs a name and object arguments")
        return _reply(id_, _call_tool({"name": name, "arguments": arguments}))
    return _error(id_, METHOD_NOT_FOUND, f"method not found: {method}")


def _answer(message: Any, stderr: TextIO) -> dict | list | None:
    if isinstance(message, list):
        if not message:
            return _error(None, INVALID_REQUEST, "empty batch")
        replies = [r for r in (_answer(m, stderr) for m in message) if r is not None]
        return replies or None
    try:
        return handle(message)
    except Exception as exc:  # noqa: BLE001 - the loop must outlive a bug in one request
        stderr.write(f"heat-governor mcp: internal error: {exc.__class__.__name__}: {exc}\n")
        id_ = message.get("id") if isinstance(message, dict) else None
        return _error(id_, INTERNAL_ERROR, "internal error")


def serve(stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    """Read one JSON message per line until EOF; write only JSON-RPC frames to ``stdout``."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            stderr.write(f"heat-governor mcp: malformed JSON ignored: {exc}\n")
            reply: Any = _error(None, PARSE_ERROR, "parse error")
        else:
            reply = _answer(message, stderr)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0

"""``heat-governor mcp`` -- a read-only MCP server over stdio (see ``heat_governor.mcp_server``)."""

from __future__ import annotations

import argparse
import sys

from heat_governor import mcp_server


def cmd_mcp(args: argparse.Namespace) -> int:
    stdin = sys.stdin
    if stdin.isatty():
        sys.stderr.write(
            "heat-governor mcp: an MCP server speaks JSON-RPC on stdin/stdout; point an MCP "
            "client at 'heat-governor mcp' instead of running it by hand.\n"
        )
        return 0
    real_stdout = sys.stdout
    sys.stdout = sys.stderr  # a stray print() can never corrupt the protocol stream
    try:
        return mcp_server.serve(stdin, real_stdout, sys.stderr)
    finally:
        sys.stdout = real_stdout


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "mcp",
        help="Read-only MCP server over stdio (tools: mesh_status, machine_state, sense).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Accepted for uniformity; the protocol is always JSON-RPC and ignores it.",
    )
    p.set_defaults(func=cmd_mcp, json=False)

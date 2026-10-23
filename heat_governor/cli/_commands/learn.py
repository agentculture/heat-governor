"""``heat-governor learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from heat_governor import __version__
from heat_governor.cli._output import emit_result

_TEXT = """\
heat-governor — keep machines running heavy AI work without thermal resets.

Purpose
-------
One daemon per host (DGX Spark, Jetson AGX Thor, Jetson AGX Orin, or generic).
About once a second it reads the thermal zones, decides a level (cool / warm /
hot / critical), publishes an admission signal clients read before starting new
work, records every tick, and -- only with --apply -- duty-cycle pauses batch
containers and caps CPUs. Serving containers are never paused. It only ever
undoes a pause it made itself.

Commands
--------
  heat-governor sense              Read every zone, fan and the GPU once.
  heat-governor capabilities       What this host lets us read and change.
  heat-governor run [--apply]      The daemon (dry-run without --apply).
  heat-governor state              Read the published admission state.
  heat-governor wait [--timeout]   Block until new work is admitted (exit 3 if held).
  heat-governor report             Reboots seen in the records, and what preceded them.
  heat-governor replay <trace>     Run the controller over a recorded trace.
  heat-governor sensors            Every sensor separately over a window (--since/--until).
  heat-governor unit -- <flags>    Print a systemd user unit (writes nothing).
  heat-governor config <verb>      overview | path | show | init [--write] | check:
                                   the TOML file a running daemon re-reads (~2 s).
  heat-governor gpu-clock <verb>   overview | status | helper | lock MHZ | reset:
                                   the GPU clock ceiling (needs the root helper).
  heat-governor whoami | learn | explain <path> | overview | doctor | cli overview

Containers opt in by label: heat-governor.role=batch (duty-cycled, CPU-capped)
or heat-governor.role=serving (never paused; CPU-capped only with
heat-governor.cpu-cap=true). Python clients: heat_governor.client (read, wait,
Gate). Contract: docs/contracts/state.md; guide: docs/integration.md.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error (e.g. no docker socket)
  3 `wait` timed out while the host was still held
  4+ reserved

More detail
-----------
  heat-governor explain heat-governor
  heat-governor explain run
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "heat-governor",
        "version": __version__,
        "purpose": "Keep machines running heavy AI work without thermal resets.",
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
            {"path": ["sense"], "summary": "Read every zone, fan and the GPU once."},
            {"path": ["capabilities"], "summary": "What this host lets us read and change."},
            {"path": ["run"], "summary": "The governing daemon (dry-run without --apply)."},
            {"path": ["state"], "summary": "Read the published admission state."},
            {"path": ["wait"], "summary": "Block until new work is admitted."},
            {"path": ["report"], "summary": "Reboots seen in the records."},
            {"path": ["replay"], "summary": "Run the controller over a recorded trace."},
            {"path": ["sensors"], "summary": "Every sensor separately over a time window."},
            {"path": ["unit"], "summary": "Print a systemd user unit."},
            {"path": ["config"], "summary": "The config file a running daemon re-reads."},
            {"path": ["gpu-clock"], "summary": "The GPU clock ceiling (root helper)."},
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
            "3": "wait timed out while held",
        },
        "json_support": True,
        "explain_pointer": "heat-governor explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)

"""Markdown catalog for ``heat-governor explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("heat-governor",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# heat-governor

A clonable template for AgentCulture mesh agents. It carries an agent-first CLI
(cited from the teken `python-cli` reference), a mesh identity (`culture.yaml` +
`CLAUDE.md`), the canonical guildmaster skill kit under `.claude/skills/`, and a
buildable/deployable package baseline. Clone it, rename the package, edit
`culture.yaml`, and you have a new agent.

## Verbs

- `heat-governor whoami` — identity probe from `culture.yaml`.
- `heat-governor learn` — structured self-teaching prompt.
- `heat-governor explain <path>` — markdown docs for any noun/verb.
- `heat-governor overview` — descriptive snapshot of the agent.
- `heat-governor doctor` — check the agent-identity invariants.
- `heat-governor cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `heat-governor explain whoami`
- `heat-governor explain doctor`
"""

_WHOAMI = """\
# heat-governor whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    heat-governor whoami
    heat-governor whoami --json
"""

_LEARN = """\
# heat-governor learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    heat-governor learn
    heat-governor learn --json
"""

_EXPLAIN = """\
# heat-governor explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    heat-governor explain heat-governor
    heat-governor explain whoami
    heat-governor explain --json <path>
"""

_OVERVIEW = """\
# heat-governor overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the template carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    heat-governor overview
    heat-governor overview --json
"""

_DOCTOR = """\
# heat-governor doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`claude` → `CLAUDE.md`), plus a
skills-present check. Exits 1 when unhealthy.

prompt-file-present requires the *resident* prompt the declared backend
actually reads. Other harness prompt files recognized under the same backend
name (`AGENTS.override.md`, `.pi/SYSTEM.md`, `QWEN.md`) belong to
interactively available harnesses the mesh daemon never loads; they are
reported by the informational harness-prompts check and never substituted.

## Usage

    heat-governor doctor
    heat-governor doctor --json
"""

_CLI = """\
# heat-governor cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    heat-governor cli overview
    heat-governor cli overview --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("heat-governor",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
}

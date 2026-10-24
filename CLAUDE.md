# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`heat-governor` is an AgentCulture mesh agent that **keeps machines running heavy AI work without thermal resets: it senses the thermal zones, signals clients when to hold new work, duty-cycles and CPU-caps the containers that opt in, and records each run so the next one is tuned better.**

It was scaffolded from `culture-agent-template`, so today the repo is the
sibling-pattern baseline only: an agent-first CLI, a mesh identity
(`culture.yaml`), the vendored guildmaster skill kit, four harness prompt
files, and a CI/deploy pipeline. It is a sibling to
[`guildmaster`](https://github.com/agentculture/guildmaster) (skills supplier),
[`steward`](https://github.com/agentculture/steward) (alignment —
`steward doctor`) and [`teken`](https://github.com/agentculture/teken) (the
`afi-cli` scaffolder the CLI is cited from).

### Current state vs. the goal

| Area | State |
|------|-------|
| Agent-first CLI (`whoami`, `learn`, `explain`, `overview`, `doctor`, `cli overview`) | Implemented |
| Mesh identity, harness prompt files, skill kit, CI, PyPI publish | Implemented |
| Sensing: thermal zones + trip points, fans, GPU (`sense.py`; `sense`, `capabilities`) | Implemented |
| Platform profiles: Spark (measured), Thor + Orin (declared), generic (`platforms.py`) | Implemented |
| Pure controller: levels, lookahead, hysteresis, duty, CPU ramp, sensor loss (`policy.py`) | Implemented |
| Actuators: duty-cycle pause + CPU caps over the docker socket, own-pause ledger (`actuators.py`, `docker.py`) | Implemented, unit-tested; not yet run with `--apply` on a real window |
| User-unit CPU quota (`[[units]]`, e.g. `app.slice`) via `systemctl --user set-property --runtime` (`units.py`) | Implemented, unit-tested with a fake; opt-in; Spark bands declared, unmeasured |
| Admission signal: state file, `GET /state`, client `read`/`wait`/`Gate` (`state.py`, `client.py`) | Implemented |
| Records, reboot report, trace replay (`recorder.py`; `report`, `replay`) | Implemented |
| Live validation on a batch window (duty vs peak temperature vs throughput) | (planned) |
| Configuration: one TOML file, strict validation, live reload, `config` verbs (`config.py`) | Implemented |
| GPU clock ceiling via a root helper + narrow sudoers rule (`gpuclock.py`; `gpu-clock` verbs) | Implemented; helper installed on all four boxes 2026-10-23; `lock` at max accepted on GB10 and both Jetsons; effect of a lower ceiling unmeasured |
| CPU clock, fan speed, active cores, nvpmodel | Not driven (root-only or firmware-owned) |

Read `docs/HANDOFF-2026-10-23.md` (why, and what was measured), `docs/contracts/state.md` (the client
contract) and `docs/integration.md` (how lobes, sparkrun and mjolnir use it) before changing behaviour.

### Working on the governing domain

- `run --apply` pauses and CPU-caps real containers (and, when the config lists
  `[[units]]`, sets a CPUQuota on real systemd user units), and anything that writes
  cpufreq / fan / CPU-hotplug controls changes the **live machine**, not the repo.
  Don't run it without the user's go-ahead, and keep tests off the real
  `/sys` (point code at a configurable root and use `tmp_path` fixtures).
- The runtime package has **no third-party dependencies**
  (`dependencies = []` in `pyproject.toml`). Keep it that way unless there's a
  deliberate decision otherwise; dev-only tools go in `[dependency-groups].dev`.

## Commands

```bash
uv sync                                    # install (incl. dev group)
uv run pytest -n auto                      # full suite (what CI runs, plus --cov)
uv run pytest tests/test_cli.py::test_whoami_json   # a single test
uv run heat-governor whoami [--json]       # run the CLI

# lint — the same set CI's lint job runs
uv run black --check heat_governor tests
uv run isort --check-only heat_governor tests
uv run flake8 heat_governor tests
uv run bandit -c pyproject.toml -r heat_governor
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
uv run teken cli doctor . --strict         # agent-first rubric gate
python3 scripts/scan-secrets.py            # committed-secret / non-localhost-endpoint scan
uv run python scripts/harness-smoke.py --stage config   # all four harness configs
```

Line length is 100 (black, isort, flake8). Coverage must stay ≥ 60%
(`fail_under` in `pyproject.toml`). The `gate` section of `culture.yaml`
(`uv sync`, then `uv run pytest -n auto`) is the setup and test contract for
external tooling, so update it if those commands change.

## CLI architecture

The CLI is cited (cite-don't-import) from teken's `python-cli` reference.
Its contract is checked by `teken cli doctor . --strict` in CI:

- Results go to **stdout** and errors/diagnostics to **stderr**, never mixed.
  Every command takes `--json`.
- Exit codes: `0` success, `1` user error, `2` environment error, `3+`
  reserved (`heat_governor/cli/_errors.py`).
- Handlers return `None`/`int` or **raise `CliError`**. `_dispatch` in
  `heat_governor/cli/__init__.py` wraps any other exception into a `CliError`,
  so tracebacks never leak. `_CliArgumentParser` routes argparse errors through
  the same structured format and checks raw argv for `--json` before parsing.
- **Adding a verb or noun group:** create a module in
  `heat_governor/cli/_commands/` with a `register(sub)` function and call it in
  `_build_parser()`. Pass `parser_class=type(p)` to nested subparsers so their
  errors stay structured. Any noun with action verbs must also expose an
  `overview` verb (rubric `overview_cli_noun_exists`; see `_commands/cli.py`
  for the pattern). Add an entry to `ENTRIES` in
  `heat_governor/explain/catalog.py`, because `test_every_catalog_path_resolves`
  and the rubric expect every path to be explainable. `learn` text must stay
  ≥ 200 chars and cover purpose, command map, exit codes, `--json` and
  `explain`.
- `doctor` checks the identity invariants: **prompt-file-present** and
  **backend-consistency** (`claude` ↔ `CLAUDE.md`). It also reports the
  harness prompt files.

## Identity and the four harnesses

`culture.yaml` declares `suffix: heat-governor` and `backend: claude`, so
**this file is the mesh resident prompt** that the Culture daemon loads.
Interactive harness choice is a separate selection: all four harnesses run
over the same clone, each reading exactly one root file. There is no shared
base:

| Harness | Reads |
|---------|-------|
| Claude Code | `CLAUDE.md` |
| Pi / associate | `AGENTS.override.md` (context) + `.pi/SYSTEM.md` (system prompt) |
| colleague | `AGENTS.colleague.md` |
| Qwen Code | `QWEN.md` |

Invariants enforced by `scripts/harness-smoke.py` and the tests:

- **Never add a root `AGENTS.md`.** It would break Pi's override and
  colleague's cascade.
- `.qwen/skills`, `.pi/skills` and `.colleague/skills` are symlinks onto
  `.claude/skills`. One skill tree serves all four harnesses.
- The live probes in `docs/harness-invocations.yaml` ask whether `CLAUDE.md` /
  `QWEN.md` describe this project as `heat-governor`. Pi's probe relies on the
  `lobes-cli` attribution appearing **only** in `.pi/SYSTEM.md`, never in
  `AGENTS.override.md`.
- When you change project-level facts here, update `AGENTS.override.md`,
  `AGENTS.colleague.md` and `QWEN.md` too, so they don't drift.

See `docs/harness-selection.md` and `docs/automation-contract.md` for the
details.

## Skills

`.claude/skills/` vendors the guildmaster skill kit **verbatim**. Never
reformat or edit those files; re-sync from upstream (`docs/skill-sources.md`)
instead. They are excluded from markdownlint for the same reason. Tooling
prerequisites on PATH: `devex` (`cicd` delegates to `devex pr`), `agtag`
(`communicate`), and optionally `colleague` (`ask-colleague`).

## Conventions

- **Every PR bumps the version**, even docs-only ones (`version-bump` skill
  → `pyproject.toml` + `CHANGELOG.md`). The `version-check` CI job blocks
  merge otherwise.
- **PRs go through the `cicd` skill** (`devex pr` + SonarCloud gate). Online
  posts are signed `- heat-governor (Claude)`; the `cicd` and `communicate`
  scripts append this automatically.
- **Deploy:** a push to `main` that touches `pyproject.toml` or
  `heat_governor/**` publishes to PyPI via Trusted Publishing. PRs publish a
  `.devN` build to TestPyPI.
- **Reach for `ask-colleague` reflexively.** Before presenting a non-trivial
  diff, run `review`. For a fresh read of an unfamiliar area, run `explore`.
  Both are read-only. `write --apply` / `--pr` needs the user's go-ahead.
  Treat its output as a second opinion to verify, not as authority.
- **Worktrees** you create go in `../.worktrees.heat-governor/<name>/` with a
  work-scoped branch prefix, never a shared `../worktrees/` or `agent/*`
  branches. This overrides the vendored `assign-to-workforce` example. Remove
  them with `git worktree remove`. `ask-colleague`'s self-cleaning `/tmp`
  worktrees are exempt.
- **Memory: recall before, remember after.** Run `/recall` before
  non-trivial work and `/remember` for non-obvious decisions and gotchas
  (for example, a tuning result and why it held). Inside this repo the
  wrapper defaults to `--scope heat-governor --visibility public`, which
  writes to the **committed** `./.eidetic/memory`. The vendored SKILL.md
  descriptions say records go to a private home-directory store, but they're stale; the script
  is what runs. Pass `--visibility private` to keep a note out of git.
- Keep docs grounded in what is on disk. Mark anything ahead of the code as
  `(planned)`.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`heat-governor` is an AgentCulture mesh agent that **governs CPU clock speed,
fan speed and active cores to keep machines thermally balanced without
resets, and records each run so the next one is tuned better.**

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
| Agent-first CLI (`whoami`, `learn`, `explain`, `overview`, `doctor`, `cli overview`) | Implemented (template verbs) |
| Mesh identity, harness prompt files, skill kit, CI, PyPI publish | Implemented |
| Thermal sensing (temperatures, fan RPM, clocks, online cores) | (planned) |
| Actuators — CPU clock, fan speed, active cores | (planned) |
| Control loop that keeps the machine balanced without resets | (planned) |
| Per-run records, and tuning the next run from them | (planned) |

The CLI's `learn` text, `explain` root entry and argparse `description`
(`heat_governor/cli/_commands/learn.py`, `heat_governor/explain/catalog.py`,
`heat_governor/cli/__init__.py`) still say "a clonable template". Update them
when the first domain verbs land.

### Working on the governing domain

- Anything that actuates hardware (writing cpufreq / fan / CPU-hotplug
  controls, changing governors) changes the **live machine**, not the repo.
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
  descriptions say "private / `~/.eidetic`", but they're stale; the script
  is what runs. Pass `--visibility private` to keep a note out of git.
- Keep docs grounded in what is on disk. Mark anything ahead of the code as
  `(planned)`.

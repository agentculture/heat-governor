# QWEN.md

This file provides guidance to Qwen Code when working with code in this
repository. Qwen Code's context loader reads exactly `QWEN.md` and `AGENTS.md`
in a directory. This repo deliberately ships only `QWEN.md` (there is no
`AGENTS.md`; each harness gets its own file), so this file is the sole source
of project guidance for a Qwen Code session.

## What this project is

`heat-governor` is an AgentCulture mesh agent that **keeps machines running heavy AI work without thermal resets: it senses the thermal zones, signals clients when to hold new work, duty-cycles and CPU-caps the containers that opt in, and records each run so the next one is tuned better.**

It was scaffolded from `culture-agent-template`. The governing core has landed: `sense`, `capabilities`, `run` (the daemon), `state`, `wait`, `report`, `replay` and `unit`, with built-in profiles for the DGX Spark (measured), Jetson AGX Thor and Orin (declared, unmeasured). Live validation of `run --apply` on a real batch window is **(planned)**. The agent verbs (`whoami`, `learn`, `explain`, `overview`, `doctor`, `cli overview`) remain.

Actuating hardware (cpufreq, fan or CPU-hotplug controls) changes the live
machine, not the repo. Don't run it without the operator's go-ahead, and keep
tests off the real `/sys`.

## Prompt files by harness

- **Claude Code** → [`CLAUDE.md`](CLAUDE.md) (the fullest write-up).
- **Pi / associate** → [`AGENTS.override.md`](AGENTS.override.md) for context,
  plus [`.pi/SYSTEM.md`](.pi/SYSTEM.md) for its system prompt.
- **colleague** → [`AGENTS.colleague.md`](AGENTS.colleague.md).
- **Qwen Code** → this file.

`culture.yaml` declares `suffix: heat-governor`, `backend: claude`, so the
mesh resident reads `CLAUDE.md`. A Qwen Code session here is a local tool
session. It reads `QWEN.md` regardless of that declaration, and running Qwen
Code neither requires nor changes it. Never add a root `AGENTS.md`.

## Commands

```bash
uv sync
uv run pytest -n auto                                # full suite
uv run pytest tests/test_cli.py::test_whoami_json    # a single test
uv run heat-governor whoami [--json]
uv run black --check heat_governor tests && uv run isort --check-only heat_governor tests
uv run flake8 heat_governor tests && uv run bandit -c pyproject.toml -r heat_governor
uv run teken cli doctor . --strict                   # agent-first rubric gate
uv run python scripts/harness-smoke.py --stage config
```

## The CLI

The CLI is cited from teken's `python-cli` reference, and the runtime package
has **no third-party dependencies**. The verbs are `whoami`, `learn`,
`explain <path>`, `overview`, `doctor` and `cli overview`.

Contract: every command supports `--json`. Results go to stdout and errors to
stderr, never mixed. Exit codes are `0` success, `1` user error, `2`
environment error, `3+` reserved. Handlers raise `CliError` on failure.

To add a verb, create a `register(sub)` module in
`heat_governor/cli/_commands/`, wire it in `_build_parser()`, and add an
`explain` entry in `heat_governor/explain/catalog.py`. A noun with action
verbs also needs an `overview` verb.

## Conventions

- **Every PR bumps the version** (`version-bump` skill). The `version-check`
  CI job blocks merge otherwise.
- **Lint:** black, isort, flake8 (line length 100), bandit, markdownlint.
  Coverage must stay at least 60%.
- **Deploy:** a push to `main` that touches the package publishes to PyPI via
  Trusted Publishing. PRs publish a dry-run build to TestPyPI.
- `.claude/skills/` is vendored **verbatim** from guildmaster
  (`docs/skill-sources.md`). Never edit those files; re-sync from upstream
  instead. `.qwen/skills` is a symlink onto that tree.

## Layout

```text
heat_governor/            agent-first CLI (cited from teken's python-cli reference)
  cli/                    parser, error/output contract, _commands/ (verbs)
  explain/                markdown catalog for `explain`
tests/                    pytest: CLI, doctor, harness registries, smoke, secret scan
scripts/                  harness-smoke.py, scan-secrets.py
.claude/skills/           vendored guildmaster skill kit (cite-don't-import)
docs/                     harness selection/automation contract, skill provenance
culture.yaml              mesh identity (suffix + backend) and `gate` commands
.github/workflows/        tests + deploy (PyPI Trusted Publishing)
```

This file describes the repository **as it exists on disk today**. Keep any
edits grounded in what is checked in, and mark anything ahead of the code as
`(planned)`. For workflow conventions (worktree layout, memory discipline,
`ask-colleague` usage), see [`CLAUDE.md`](CLAUDE.md).

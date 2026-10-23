# heat-governor

Governs CPU clock speed, fan speed and active cores to keep machines thermally
balanced without resets, and records each run so the next one is tuned better.

`heat-governor` is an [AgentCulture](https://github.com/agentculture) mesh
agent. It is distributed as a Python package with an agent-first CLI and runs
on the mesh as the `heat-governor` resident.

## Status

The repository currently contains the agent scaffold only. The thermal
governing itself has not been implemented yet.

| Capability | Status |
|------------|--------|
| Agent-first CLI, mesh identity, skill kit, CI + PyPI publishing | Available |
| Reading temperatures, fan speed, clocks and online cores | Planned |
| Controlling CPU clock, fan speed and active cores | Planned |
| Control loop that keeps the machine balanced without resets | Planned |
| Per-run records, used to tune the next run | Planned |

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run heat-governor whoami           # identity from culture.yaml
uv run heat-governor learn            # self-teaching prompt (add --json)
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

## CLI

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout and errors/diagnostics
to stderr, never mixed. Exit codes: `0` success, `1` user error, `2`
environment error, `3+` reserved.

The CLI is cited from [teken](https://github.com/agentculture/teken)
(`afi-cli`), and the runtime package has no third-party dependencies.

## Agent harnesses

Four harnesses can work in this repo. Each reads exactly one root prompt file,
and there is deliberately **no `AGENTS.md`** acting as a shared base:

| Harness | File(s) |
|---------|---------|
| Claude Code | [`CLAUDE.md`](CLAUDE.md) (the fullest write-up; read it first) |
| Pi / associate | [`AGENTS.override.md`](AGENTS.override.md) + [`.pi/SYSTEM.md`](.pi/SYSTEM.md) |
| colleague | [`AGENTS.colleague.md`](AGENTS.colleague.md) |
| Qwen Code | [`QWEN.md`](QWEN.md) |

`culture.yaml` declares `backend: claude`, so `CLAUDE.md` is the mesh
resident's prompt. That declaration does not stop you running any of the four
harnesses interactively. See
[`docs/harness-selection.md`](docs/harness-selection.md) and
[`docs/automation-contract.md`](docs/automation-contract.md).
`scripts/harness-smoke.py` checks that all four configs are correct.

The guildmaster skill kit is vendored under `.claude/skills/`, cited
verbatim. Its provenance is recorded in
[`docs/skill-sources.md`](docs/skill-sources.md).

## Contributing

- Every PR bumps the version (`version-bump` skill). CI's `version-check` job
  blocks the merge otherwise.
- CI runs pytest with coverage and SonarCloud, black, isort, flake8, bandit,
  markdownlint, the secret scan, the agent-first rubric gate, and the
  per-harness smoke check.
- A merge to `main` that touches `pyproject.toml` or `heat_governor/`
  publishes to PyPI via Trusted Publishing. Such PRs publish a dev build to
  TestPyPI.

## The `gate` section

`culture.yaml` carries a top-level `gate` section that declares how to set up
and test this repo, so tooling such as a PR-fixer needs no per-repo knowledge:

```yaml
gate:
  setup:
  - [uv, sync]
  test:
  - [uv, run, pytest, -n, auto]
```

`setup` and `test` are each a list of commands. Each command is an argv list
(never a shell string), run in order from the repo root. Culture's loader
ignores unknown top-level keys, so the section is safe alongside `agents`.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).

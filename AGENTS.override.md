# AGENTS.override.md

This file is the **context layer** for the Pi harness (the `pi` CLI, and the
`associate` non-coding harness modelled on it) when it runs inside this repo.

Pi's CONTEXT loader concatenates `AGENTS.md` or `CLAUDE.md` from three places:
its user-level config directory (see Pi's own docs), each parent directory,
and the working directory. An `AGENTS.override.md` in a directory replaces
that directory's `AGENTS.md`/`CLAUDE.md` entry outright rather than adding to
it. That is why this repo ships this file instead of an `AGENTS.md`: Pi must
**not** inherit `CLAUDE.md`, which assumes a coding session with
repo-write authority that Pi's non-coding lane does not have.

Who Pi is here, and what it may and may not do, lives in Pi's **system
prompt** file, [`.pi/SYSTEM.md`](.pi/SYSTEM.md). This file is project
*context* only.

## What this project is

`heat-governor` is an AgentCulture mesh agent that **keeps machines running heavy AI work without thermal resets: it senses the thermal zones, signals clients when to hold new work, duty-cycles and CPU-caps the containers that opt in, and records each run so the next one is tuned better.**

It was scaffolded from `culture-agent-template`. The governing core has landed: `sense`, `capabilities`, `run` (the daemon), `state`, `wait`, `report`, `replay` and `unit`, with built-in profiles for the DGX Spark (measured), Jetson AGX Thor, Jetson AGX Orin and VENTUNO Q (`ventunoq`; declared, unmeasured). The fleet view has landed too: `mesh overview|status|serve` (reader plus dashboard and JSON API on `127.0.0.1:9480`, machines listed in `fleet.toml`) and `mcp` (read-only MCP server over stdio). The memory guard has landed as well: with `[memory]` the daemon kills a runaway own-user process before memory runs out (absolute per-platform limits), records who used the memory, and with `[ship]` keeps that evidence in an S3-compatible bucket (`memory status|top|replay`, `ship test|fetch`; live on all six boxes since 2026-11-15). The dashboard's Boundaries tab lists each machine's memory guard and its `[[memory.allow]]` rules, and `mesh status` shows the guard per machine. Since 0.19.0 boundaries can also be created, changed and deleted: page-managed rules live in `boundaries.toml` beside `config.toml`, written by `heat-governor boundaries list|add|set|rm|apply|preview [--machine M]` and, with `mesh serve --token-file`, from the page (bearer token, version check, on-box kill check, backups, audit log, `memory_boundary_changed` event; see `docs/contracts/boundaries.md`). Implemented and unit-tested; live-checked on the EliteMini and orin on 2026-11-17 with a rule that matches no process (`docs/evidence/2026-11-17-boundaries-write-live.md`); the other four boxes still run 0.17.0, and the editor has not been used with a token in a real browser. Saving a boundary on a live machine and enabling a token file on a live `mesh serve` both need the operator's go-ahead, and tests never write under a real config directory. Record retention has landed in code (0.20.0): the daemon keeps today plus `[daemon] keep_days` (default 7) complete earlier days of `<data_dir>/records`, gzips days older than yesterday to `YYYYMMDD.jsonl.gz`, deletes older days (days before a hole longer than `keep_days` (a forward clock jump looks like one) are kept until `records prune --allow-gap`) and logs a `records_pruned` event; `governor.log` rotates to `governor.log.1.gz` past `[daemon] log_max_mib` (default 10); `report`, `sensors`, `replay`, `memory replay`, the postmortem and the dashboard's trend history read raw and gzipped days alike; `heat-governor records overview|prune [--dry-run]` runs or previews a pass (spec `docs/specs/2026-11-24-record-retention.md`). Unit-tested only: it has **not** been run on a live box yet, so treat it as unvalidated live **(planned)**. A token-protected tailnet listener and a daemon `GET /history` route are **(planned)**. Live validation of `run --apply` on a real batch window is **(planned)**. The agent verbs (`whoami`, `learn`, `explain`, `overview`, `doctor`, `cli overview`) remain. Keep that in mind when you answer
questions about what the agent can currently do.

It is a sibling to [`guildmaster`](https://github.com/agentculture/guildmaster)
(skills supplier), [`steward`](https://github.com/agentculture/steward)
(alignment), and [`teken`](https://github.com/agentculture/teken) (the CLI
scaffolder this package is cited from).

## Four harnesses, four files, no shared base

- **Claude Code** reads [`CLAUDE.md`](CLAUDE.md).
- **Pi / associate** reads this file for context, plus
  [`.pi/SYSTEM.md`](.pi/SYSTEM.md) for its system prompt.
- **colleague** reads [`AGENTS.colleague.md`](AGENTS.colleague.md).
- **Qwen Code** reads [`QWEN.md`](QWEN.md).

If you are a human reading this, `CLAUDE.md` is the fullest write-up of the
repo's conventions.

## Identity

`culture.yaml` declares `suffix: heat-governor`, `backend: claude`, so
`CLAUDE.md` is the mesh resident's prompt. A Pi session in this repo is a
**local tool session**, not the mesh resident. It reads this file and
`.pi/SYSTEM.md` regardless of that declaration.

## Layout (what you can read/find/summarize here)

```text
heat_governor/            agent-first CLI (cited from teken's python-cli reference)
  cli/                    parser, error/output contract, _commands/ (verbs)
  explain/                markdown catalog for `explain`
  fleet.py, transport.py  fleet.toml loader; local/SSH transport
  fleet_reader.py         concurrent poll, ok/stale/unreachable, history
  mesh_server.py, web/    dashboard + JSON API (vendored assets)
  mcp_server.py           read-only MCP server over stdio
tests/                    pytest: CLI, doctor, harness registries, smoke, secret scan
scripts/                  harness-smoke.py, scan-secrets.py
.claude/skills/           vendored guildmaster skill kit (cite-don't-import)
docs/                     harness selection/automation contract, skill provenance
culture.yaml              mesh identity (suffix + backend) and `gate` commands
.github/workflows/        tests + deploy (PyPI Trusted Publishing)
```

## Conventions worth knowing before you answer a question about this repo

- The vendored skills under `.claude/skills/` are cited **verbatim** from
  guildmaster. Never propose editing their scripts; the fix belongs upstream
  (`docs/skill-sources.md`).
- Every PR bumps the version (`version-bump` skill). CI's `version-check` job
  blocks merge otherwise.
- The CLI contract: `--json` everywhere, results on stdout, errors on
  stderr, and exit codes `0`/`1`/`2`.
- This file describes the repo **as it exists on disk today**. If you are
  asked to update it, keep claims grounded in what is checked in.

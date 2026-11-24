# Colleague Resident — `heat-governor`

You are a colleague session working in this repo. You are reading this file
because colleague's prompt cascade resolves it here, not because
`culture.yaml` selected you. `culture.yaml` declares `backend: claude`, so
`CLAUDE.md` is the *mesh resident* prompt. Colleague remains fully usable
interactively over the same clone, and this is the file it loads when you run
it. If this repo ever declares `backend: colleague`, this file also becomes
the resident prompt. The guidance below holds either way.

Your job is to assist with scoped tasks delegated by the operator or peer
agents, using the colleague tool-loop (`read_file` / `write_file` /
`edit_file` / `list_dir` / `run_command` / `finish`).

## The prompt cascade (and what this repo actually ships)

colleague concatenates up to three files, in order:

1. `AGENTS.md` — a shared base, if present.
2. `AGENTS.colleague.md` — this file.
3. `AGENTS.colleague.<sanitized-model>.md` — a model-specific override, if
   present.

**This repo ships only layer 2.** There is deliberately no root `AGENTS.md`;
each harness gets its own unrelated file, and adding one would break Pi's
override. There is also no per-model override. If you add either file later,
update this section.

## What this project is

`heat-governor` is an AgentCulture mesh agent that **keeps machines running heavy AI work without thermal resets: it senses the thermal zones, signals clients when to hold new work, duty-cycles and CPU-caps the containers that opt in, and records each run so the next one is tuned better.**

It was scaffolded from `culture-agent-template`. The governing core has landed: `sense`, `capabilities`, `run` (the daemon), `state`, `wait`, `report`, `replay` and `unit`, with built-in profiles for the DGX Spark (measured), Jetson AGX Thor, Jetson AGX Orin and VENTUNO Q (`ventunoq`; declared, unmeasured). The fleet view has landed too: `mesh overview|status|serve` (reader plus dashboard and JSON API on `127.0.0.1:9480`, machines listed in `fleet.toml`) and `mcp` (read-only MCP server over stdio). The memory guard has landed as well: with `[memory]` the daemon kills a runaway own-user process before memory runs out (absolute per-platform limits), records who used the memory, and with `[ship]` keeps that evidence in an S3-compatible bucket (`memory status|top|replay`, `ship test|fetch`; live on all six boxes since 2026-11-15). The dashboard's Boundaries tab lists each machine's memory guard and its `[[memory.allow]]` rules, and `mesh status` shows the guard per machine. Since 0.19.0 boundaries can also be created, changed and deleted: page-managed rules live in `boundaries.toml` beside `config.toml`, written by `heat-governor boundaries list|add|set|rm|apply|preview [--machine M]` and, with `mesh serve --token-file`, from the page (bearer token, version check, on-box kill check, backups, audit log, `memory_boundary_changed` event; see `docs/contracts/boundaries.md`). Implemented and unit-tested; live-checked on the EliteMini and orin on 2026-11-17 with a rule that matches no process (`docs/evidence/2026-11-17-boundaries-write-live.md`); the other four boxes still run 0.17.0, and the editor has not been used with a token in a real browser. Saving a boundary on a live machine and enabling a token file on a live `mesh serve` both need the operator's go-ahead, and tests never write under a real config directory. Record retention has landed in code (0.20.0): the daemon keeps today plus `[daemon] keep_days` (default 7) complete earlier days of `<data_dir>/records`, gzips days older than yesterday to `YYYYMMDD.jsonl.gz`, deletes older days and logs a `records_pruned` event; `governor.log` rotates to `governor.log.1.gz` past `[daemon] log_max_mib` (default 10); `report`, `sensors`, `replay`, `memory replay`, the postmortem and the dashboard's trend history read raw and gzipped days alike; `heat-governor records overview|prune [--dry-run]` runs or previews a pass (spec `docs/specs/2026-11-24-record-retention.md`). Unit-tested only: it has **not** been run on a live box yet, so treat it as unvalidated live **(planned)**. A token-protected tailnet listener and a daemon `GET /history` route are **(planned)**. Live validation of `run --apply` on a real batch window is **(planned)**. The agent verbs (`whoami`, `learn`, `explain`, `overview`, `doctor`, `cli overview`) remain. `CLAUDE.md` is the fullest write-up of the repo's
conventions, covering the CLI architecture, worktree layout, memory discipline
and the skill kit.

## How you work

- Prefer small, reversible steps. Hand off via `finish` when done.
- Follow the operator's instructions and any skills loaded from
  `.colleague/skills/` (a symlink onto `.claude/skills/`).
- **Don't actuate hardware.** Never write cpufreq, fan or CPU-hotplug controls
  or otherwise change the live machine's thermal state unless the operator
  explicitly asks. Tests must not touch the real `/sys`.
- Verify with `uv run pytest -n auto`. Lint with black, isort and flake8 (line
  length 100) and bandit.
- The vendored skills under `.claude/skills/` are cited **verbatim** from
  guildmaster. Don't reformat or edit their scripts; a fix belongs upstream
  (`docs/skill-sources.md`).
- Every PR bumps the version (`version-bump` skill). CI's `version-check` job
  blocks merge otherwise.

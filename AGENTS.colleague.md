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

`heat-governor` is an AgentCulture mesh agent that **governs CPU clock speed,
fan speed and active cores to keep machines thermally balanced without
resets, and records each run so the next one is tuned better.**

It was scaffolded from `culture-agent-template`. Today it contains only the
baseline: an agent-first CLI (`whoami`, `learn`, `explain`, `overview`,
`doctor`, `cli overview`), a mesh identity, the vendored skill kit, and
CI/deploy. Thermal sensing, the actuators, the control loop and per-run
records are **(planned)**. `CLAUDE.md` is the fullest write-up of the repo's
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

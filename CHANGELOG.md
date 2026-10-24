# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.13.0] - 2026-10-24

### Added

- User-unit CPU quota (opt-in): `[[units]]` in the config names systemd user units (for example `app.slice`, where interactive terminal sessions and their test runs live) to cap by level with `systemctl --user set-property --runtime` (no root; a reboot drops it). Optional warm / hot / critical CPU counts per unit, else the platform default (spark 8 / 4 / 2 CPUs, declared, unmeasured; none elsewhere). The unit's original quota is read fresh and written to the ledger (`unit_quota`) before the first change, restored on exit, on removal from the config and by the next start's recovery; a band never raises a tighter original; dry-run logs only, for the whole daemon or per unit (`dry_run = true`, so a unit can be watched while containers are governed for real); a missing or inactive unit is reported once and retried every 30 s. The state document gains `units` (name, dry_run, cpus, missing). Contract: docs/contracts/user-units.md.
- Adaptive mode for user units (`mode = "adaptive"` in `[[units]]`; `levels` stays the default): a feedback loop whose step sizes come from the data it gathers. Each `interval_s` (default 20, >= 5) it compares the governing temperature rate with `target_rate_c_min` (default 0 = hold flat) and, while the level is at or above `engage` (default warm), cuts the unit's CPU from its measured use by `error / gain` (the cold-start step before a gain is learned is 25% of use; every cut is 1 CPU to 50% of use, floored at `min_cpus`, never above the current cap). It releases one step (10% of host CPUs) per 2 x interval when cooling faster than needed at the engage level or below it, and removes the cap at host CPUs. Each cut is followed by one learning sample (rate fall per CPU removed, EWMA alpha 0.3, clamped), kept per unit and platform in `<data_dir>/adaptive.json` across restarts; nothing is learned in dry-run. Sensor loss tightens by the cold-start step when engaged and never releases. Use is read from the unit's cgroup `cpu.stat` (`usage_usec`, path from `systemctl --user show -p ControlGroup`). The state's `units` entries gain `mode`, `use_cpus`, and for adaptive units `gain` and `samples`; `run --once` reports `mode`. Design values, not run on a real box.

### Changed

- Adaptive mode's conditions come from the measured data: the deadband is 2 x the standard error of the interval's least-squares temperature trend (min 0.005 C/s), a learning sample must exceed the combined noise of the trends before and after the cut, an idle unit (< 0.5 CPU) is never cut and a non-binding cap is released, and the gain clamp is widened to 0.5 C/s per CPU (live Thor samples reached 0.16). Found by live runs on Thor and Orin, 2026-10-24.

### Fixed

- Config validation of `[[units]]` reports a duplicate unit name even when that entry's `dry_run` is also invalid, and no longer fails on a non-string `name`.
- A container's original CPU limit is read with a fresh `docker inspect` right before the first cap, never taken from the discovery snapshot: right after a restore that snapshot still showed the previous cap, which then became the recorded original (DGX Spark 2026-10-23: model-gear-vllm-primary held at 12 CPUs for ~27 h, across a reboot). No cap is made when the limit cannot be read.
- An original of no limit (NanoCpus 0) survives a restore -> cap cycle as 0 (logged `cpus=unlimited`) instead of turning into every host CPU; on exit a failed restore of a container that left keeps its ledger entry.

## [0.12.0] - 2026-10-23

### Added

- Configuration file (`$HEAT_GOVERNOR_CONFIG` or $XDG_CONFIG_HOME/heat-governor/config.toml): every band, curve, CPU/GPU clock band, per-level client concurrency factor, poll interval and container rule; strict validation (unknown keys are errors); a running daemon re-reads it within ~2 s and keeps the last good settings when an edit is invalid. `config overview|path|show|init|check` verbs.
- GPU clock ceiling: `gpu_clock_bands` per platform (declared, unmeasured), enabled by `daemon.gpu_clock` or `run --gpu-clock`; a root helper (`/usr/local/sbin/heat-governor-gpuclock`: lock/reset/status) behind a narrow sudoers rule; ledger-first, released on exit, on restart and when turned off. `gpu-clock overview|status|helper|lock|reset` verbs.
- A CPU cap changed outside the governor is re-asserted on the next fresh discovery.
- NVMe temperature, every sensor: `nvme:<dev>:composite`, `sensor_1`, `sensor_2`, ... from hwmon (no subprocess, no root), with the drive's own max/critical limits recorded as limits.
- System probe every tick: CPUs online, busy % and iowait %, average/max clock, load; memory used % / available / swap; pressure-stall (memory, io, cpu) where the kernel has it; per-disk read/write MB/s and % busy; GPU graphics/memory clocks and memory utilisation.
- Every sensor separately: the state carries a `sensors` map (each zone, each GPU field, each fan) every tick, and `heat-governor sensors --since/--until [--above C]` gives min/mean/p95/max per sensor from the records.
- GPU temperature as a second input: the GPU's own reading with its own warm/hot/critical bands (DGX Spark 82/87/91 C, declared from the ~94 C limit the GB10 reports as T.Limit headroom), mapped onto the board's bands; whichever input is closer to its limit governs. `source`, `gpu_temp_c` and `gpu_limit_c` in the state; `profile.gpu_temp` in the config (false = off); `replay` feeds the trace's GPU column.

### Changed

- `admit` follows the configured concurrency factor (a factor above 0 admits), so holding at `hot` is a config choice.
- `run` flags default to the config file's `[daemon]` values; command-line flags still win.
- SonarCloud cleanup, behaviour unchanged: config validation, the system probe, the recorder (reboot report, replay, sensor stats), the actuators, the daemon's discovery and GPU-clock paths, profile overrides and `run` are split into small helpers (cognitive complexity <= 15); composite test assertions are split.

## [0.11.0] - 2026-10-23

### Added

- The governing core: one daemon per host (`heat-governor run`) that senses the thermal zones about once a second, decides a level (cool / warm / hot / critical) with a rising-rate lookahead, hysteresis and dwell, publishes an admission signal (state file, optional `GET /state`), records every tick, and with `--apply` duty-cycle pauses `heat-governor.role=batch` containers and CPU-caps opted-in containers over the docker socket. Serving containers are never paused; only the daemon's own pauses are ever undone (crash-safe ledger).
- Built-in profiles: DGX Spark (bands measured 2026-10-23, tuned by replay of that day's 1 s trace), Jetson AGX Thor and Orin (declared from their passive trip points, unmeasured), and a generic fallback from the lowest critical trip; TOML overrides via `--config`.
- Read-only verbs `sense` and `capabilities`; client verbs `state` and `wait` (exit 3 on timeout while held); `report` (reboots seen in the records), `replay` (the controller over a recorded trace) and `unit` (a systemd user unit).
- `heat_governor.client` (stdlib, self-contained): `read`, `wait` and `Gate` for benchmarks and batch jobs; contract in docs/contracts/state.md, integration guide for lobes, sparkrun and mjolnir in docs/integration.md.

### Changed

- Purpose statement in README, CLAUDE.md, QWEN.md, AGENTS.override.md, AGENTS.colleague.md and pyproject: the agent governs containers and admission, not CPU clocks, fans or cores (parked or firmware-owned).
- bandit skips B607 (fixed argv for nvidia-smi).

## [0.10.1] - 2026-10-23

### Changed

- Re-initialized CLAUDE.md from the scaffold seed into a full heat-governor prompt (current-vs-planned state, commands, CLI architecture, harness invariants, conventions).
- Rewrote README.md, QWEN.md, AGENTS.override.md and AGENTS.colleague.md to describe heat-governor instead of the culture-agent-template; thermal-governing capabilities are marked (planned).
- .pi/SYSTEM.md: associate lane may read sensors but never actuate cpufreq, fan or CPU-hotplug controls.

## [0.10.0] - 2026-10-06

### Added

- `culture.yaml` gate section: `gate.setup` and `gate.test` declare the argv
  lists that set up and test the repo (`uv sync`; `uv run pytest -n auto`),
  documented in the README.

## [0.9.0] - 2026-09-06

### Added

- **Four agent harnesses, each reading exactly one root file.** Claude
  Code→`CLAUDE.md`; Pi/`associate`→`AGENTS.override.md` (context) plus
  `.pi/SYSTEM.md` (system prompt, which *replaces* Pi's default);
  colleague→`AGENTS.colleague.md`; Qwen Code→`QWEN.md`. Deliberately **no**
  `AGENTS.md` — `AGENTS.override.md` is what stops Pi inheriting `CLAUDE.md`.
- One skill tree, four loaders: `.qwen/skills`, `.colleague/skills` and
  `.pi/skills` are relative symlinks onto `.claude/skills`. No forked scripts,
  no duplicated docs.
- `docs/harness-selection.md` (the two selections), plus
  `docs/automation-contract.md` and `docs/harness-invocations.yaml` (the four
  forced invocations as a machine-readable contract), and
  `docs/harness-verification.md` (the instrumented run).
- `scripts/harness-smoke.py` — a per-harness CI check that fails when **any one**
  of the four configs is broken, so three-quarters of a clone cannot rot
  unnoticed. A skipped check is reported as not-verified, never as a pass.
- `scripts/scan-secrets.py` — CI gate against committed credentials and
  non-localhost endpoints, with planted-secret tests proving it catches.

### Changed

- `culture.yaml` declares `backend: claude`. Because no code path rewrites that
  key, the template's declaration is what every clone inherits — the previous
  `colleague` value is why ~30 siblings carry a backend disagreeing with their
  seeded prompt file.
- `backend-fingerprints.yaml` re-synced from steward: list-valued prompts, so
  `acp` accepts `QWEN.md` and `colleague` accepts `AGENTS.override.md` and
  `.pi/SYSTEM.md`.

### Fixed

- `CLAUDE.md`, `README.md`, `QWEN.md`, `AGENTS.override.md`,
  `AGENTS.colleague.md` and `docs/skill-sources.md` had claimed this repo was a
  colleague resident, contradicting `culture.yaml`. Every harness prompt now
  names `backend: claude` / `CLAUDE.md` as the mesh resident, while stating
  that its own harness stays interactively available regardless.
- `.pi/settings.json`'s `skills` key is **inert** — that key belongs to a
  `package.json` manifest, not `settings.json`. Syscall instrumentation on a
  fresh clone: settings alone opened **0** `SKILL.md`; the `.pi/skills` symlink
  opened **19**. Replaced with the symlink, settings file removed.
- `doctor` no longer accepts an interactive harness's prompt file as the mesh
  resident's. Four harnesses ride three backend names, so `AGENTS.override.md`,
  `.pi/SYSTEM.md` and `QWEN.md` are *recognized* under `colleague`/`acp` — but
  the Culture daemon reads exactly one file per backend, and a clone carrying
  only Pi's files under `backend: colleague` used to report healthy with no
  resident prompt at all. `prompt_file_present` now requires the resident
  prompt; the others are reported by a new `harness_prompts` info check.
- `scan-secrets.py` closes three evasions: values containing `@`/`:`/`%`/`=`/`?`
  are now matched in full instead of stopping at a base64-ish alphabet (a
  quoted JSON key is matched too, which it never was); the placeholder
  exemption is a whole-value judgement, so a high-entropy literal merely
  *containing* `fake`/`example` is still reported; and endpoint hosts are
  parsed with `urlsplit`, so a bracketed IPv6 authority such as
  `http://[2001:db8::1]:8080` can no longer slip past the localhost allowlist.
- `harness-smoke.py`: a live probe is satisfied by **stdout only** (stderr
  noise mentioning `yes` or a prompt filename no longer counts as an answer,
  and a yes/no probe must answer exactly `yes`); a nonzero exit from `steward
  doctor` or `guild create` can no longer reach a passing branch on
  success-shaped JSON; failure/skip/waiver diagnostics go to stderr, leaving
  stdout to results; and an unknown `--stage` exits 1 (user error) rather than
  argparse's 2 (reserved for environment failures).

Known gaps carried into this release (not a changelog category — recorded
here so the release is not read as claiming more than it delivers): colleague
loads **0 of 19** skills from the nested tree, blocked on two upstream defects
filed with a reproduction as
[`agentculture/colleague#494`](https://github.com/agentculture/colleague/issues/494)
(the template ships the correct shape regardless); and retrofitting
already-provisioned siblings is an explicit **non-goal**, so
`heat-governor#25` stays open for the existing fleet.

## [0.8.0] - 2026-09-05

### Added

- **`validate-delivery` skill** (origin `devague`, re-broadcast by
  `guildmaster`) — the validation leg between `/assign-to-workforce` and
  `/summarize-delivery`: run the confirmed plan's behavioral tests agent-side,
  then file obligations, evidence, and behavioral deltas. Record-only; the
  `devague` CLI never runs a test.
- **`scripts/` wrappers for the five prompt-only workflow skills** — `scope`,
  `challenge`, `deviate`, `validate-delivery`, `summarize-delivery`. Each
  forwards its arguments to the `devague` CLI verbatim, so upstream owns the
  surface. Every clone now ships a complete, convention-clean skill directory
  instead of inheriting the script-less shape.

### Changed

- **Re-synced all eight `devague`-origin workflow skills** — `scope`, `think`,
  `challenge`, `spec-to-plan`, `assign-to-workforce`, `deviate`,
  `validate-delivery`, `summarize-delivery` — from devague `0.24.1`
  (`ec15362`), matching guildmaster's canonical copies. Notable upstream
  content: `/scope` fans out to read-only exploration subagents at 5+ candidate
  surfaces, and `assign-to-workforce split-plan --write` persists a durable
  gate-2 record.
- **All eight `SKILL.md` files are now byte-verbatim with upstream.** devague
  ships `type: command` on all eight itself, so nothing is added to the
  frontmatter here. The only divergence is the five wrapper scripts, which are
  additions, not edits.
- **`docs/skill-sources.md` updated for the re-sync** — count corrected from
  seven skills to eight (`validate-delivery` had no row), all eight rows
  repointed to `../guildmaster/.claude/skills/`, and pins refreshed to devague
  `0.24.1`.
- **The 2026-07-15 "vendor directly from devague" divergence is superseded.**
  That decision existed to keep guildmaster's `scripts/*.sh` wrappers out of
  this repo. The wrappers are now wanted: without them a clone ships a
  `SKILL.md` with no sibling `scripts/` and fails a
  `test_skills_convention`-style gate (guildmaster#95). The old section is
  retained, marked superseded, with its stale re-sync recipe replaced — that
  recipe would have deleted the wrappers this release adds and skipped
  `validate-delivery` entirely.

### Fixed

- **`scope.sh` usage advertised an invalid claim kind** — `--kind non-goal`
  (hyphen) is rejected by `devague capture`, which accepts `non_goal`. Anyone
  copying the wrapper's usage line got `invalid choice: 'non-goal'`. Corrected
  in both this repo and guildmaster.

## [0.7.0] - 2026-08-24

### Added

- **`resume <task-id|last> [--detach]` verb** in `ask-colleague` — pick a cut / timed-out / SIGTERM'd run back up from its persisted artifact, continuing on the original `colleague/<id>` work branch.
- **Per-seat thinking effort** in `ask-colleague` — `--effort` (acting seat), `--seat-effort S=R` (any seat), `--role NAME` (colleague#416). Rule of thumb: `--effort off` for small well-specified briefs, default for ordinary work, `xhigh` for open-ended judgement.
- **Review diff front-loading** — `ask-colleague review` embeds a filtered, bounded diff directly in the prompt instead of relying on the colleague run to fetch it.

### Changed

- **`ask-colleague` re-vendored byte-verbatim from `agentculture/colleague` @ 1.63.0** (cite-don't-import) — all five files (`SKILL.md`, `scripts/ask-colleague.sh`, `prompts/{explore,review,write}.md`). Every repo scaffolded from this template (`guild create` instantiates it) shipped the Qwen3.6-era wrapper until now.
- **Default colleague model is `unsloth/Qwen3.8-27B-NVFP4`** (was the Qwen3.6 pin). The lobes gateway on `:8001` no longer serves 3.6, so the previous default only worked via colleague's auto-refresh warning path.
- **`docs/skill-sources.md` ledger row** for `ask-colleague` updated to the 1.63.0 sync (was `2026-06-12 (colleague 1.7.0, direct)`) and its verb list extended with `plan` / `resume` / the pilot verbs.

## [0.6.1] - 2026-07-20

### Added

- **Worktree location convention** in `CLAUDE.md` — every worktree you create
  by hand (workforce fan-out lanes, scratch checkouts) lives in
  `../.worktrees.heat-governor/<name>/`, one
  repo-named directory beside the checkout, replacing a shared `../worktrees/`
  folder. This workspace holds many sibling projects, so a generic shared
  folder accumulates orphaned trees from several repos at once with nothing
  indicating ownership — a stale-tree sweep can't tell a live lane from junk.
  Matches the convention already documented in sibling repo `reachy-mini-cli`.
  Adds branch-prefix guidance (scope the prefix to the work; plain `agent/*`
  collides with leftovers from earlier fan-outs and fails `git worktree add
  -b`), and notes that the vendored `assign-to-workforce` skill uses both the
  shared path *and* `agent/<task-id>` branches in its fan-out example — it is
  cited verbatim and must not be edited, so both are overridden when following
  it. Teardown guidance names `git worktree remove <path>` as the verb that
  actually deletes a worktree; `git worktree prune` only clears metadata for
  directories that are already gone. Tool-managed throwaways are explicitly
  out of scope: `ask-colleague`'s read-only verbs create a detached worktree
  under `${TMPDIR:-/tmp}` and reap it on an EXIT trap, so they never persist
  to need an owner.

## [0.6.0] - 2026-07-18

### Added

- **Four devague-origin skills re-vendored into `.claude/skills/`**
  (cite-don't-import), synced to the fixed devague source
  (devague#74/#75/#76):
  - `challenge` — a risk-scaled blind-spot discovery pass that runs between
    `/think` and `/spec-to-plan`, routing findings back through the existing
    deterministic moves as human-adjudicated proposals.
  - `scope` — the idea→scope leg that surveys the surfaces an idea touches
    before framing, seeding the Announcement Frame with provenance-backed
    boundary/non-goal/assumption claims.
  - `deviate` — stops an in-flight `assign-to-workforce` run when execution
    must diverge from the confirmed plan and records the divergence as a
    first-class, append-only deviation record.
  - `summarize-delivery` — closes the loop after an `assign-to-workforce`
    run with a planned-vs-actual accountability artifact.

  These four originate in `devague` and are re-broadcast via guildmaster; see
  `docs/skill-sources.md` for provenance.

## [0.5.0] - 2026-06-24

### Added

- **Memory-discipline "Conventions and workflow" section in `CLAUDE.md`** — a
  per-task *recall-before / remember-after* convention (scope localized to this
  repo's nick) so the vendored `remember` / `recall` skills are actually used,
  not just present: `/recall` before non-trivial work to build on prior
  decisions instead of re-deriving them, and `/remember` when a non-obvious
  decision, constraint, fix-and-why, or hard-won gotcha surfaces. The section
  documents this repo's memory as **in-repo and public** — records resolve to
  `<repo-root>/.eidetic/memory` (committed, team- and mesh-shared). Inserted
  idempotently (skipped if already present), slotted under an existing
  "Conventions and workflow" heading when one exists, else appended.

### Changed

- **Refreshed the `remember` + `recall` wrappers from eidetic-cli 0.10.0**
  (cite-don't-import) — picks up eidetic's **project-local store default**: the
  files backend now resolves per record by visibility — PUBLIC records inside a
  git repo go to `<repo-root>/.eidetic/memory` (committed, team-shared), PRIVATE
  records (or any record outside a repo) go to `$HOME/.eidetic/memory` (never
  committed), an explicit `EIDETIC_DATA_DIR` still wins, and recall reads both
  stores and merges. Also carries the 0.9.3 hardening (interactive-stdin guard,
  `help` as a search term, SIGPIPE-safe suffix parsing). **Recipe policy
  override (the wrappers here are NOT byte-verbatim):** the injected default
  visibility is flipped from eidetic's `private` to **`public`**, so a plain
  `/remember` lands the note in `./.eidetic/memory` in this repo, kept as part
  of the repo — pass `--visibility private` to route a record to `$HOME`
  instead. `remember` drives `eidetic remember` (idempotent upsert of one JSON
  record or an NDJSON batch on stdin); `recall` drives `eidetic recall` with
  four search modes (exact / approximate / keyword / hybrid). Each `SKILL.md` is
  localized only in the illustrative `--scope <nick>` examples (Provenance keeps
  "First-party to eidetic-cli"). Runtime dep: the `eidetic` CLI on PATH (else a
  local eidetic-cli checkout with `uv`) — **`eidetic >= 0.10.0`** for the
  in-repo routing; on an older CLI the public records still work but are stored
  in `$HOME/.eidetic/memory` instead of in-repo. Propagated by rollout-cli's
  `eidetic-memory` recipe.

## [0.4.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `$HOME/.eidetic/memory` surface, so this agent (Claude and its colleague
  backend) can persist facts across sessions and recall them later, sharing
  one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.3.4] - 2026-06-20

### Fixed

- Identity docs and self-description strings still claimed `backend: claude`
  (prompt file `CLAUDE.md`), but this template was promoted to a colleague
  resident in #14/#15: `culture.yaml` declares `backend: colleague` (Qwen) with
  `AGENTS.colleague.md` as the resident prompt. Corrected the stale claim in
  `CLAUDE.md` (Identity section), `README.md`, `docs/skill-sources.md`, and the
  two CLI description strings (`overview` artifacts and `explain doctor`). The
  `doctor` backend→prompt-file mapping and the tests were already on
  `colleague`; this aligns the prose and self-description with them.

## [0.3.3] - 2026-06-20

### Fixed

- pyproject.toml: correct the `license` field and PyPI classifier from MIT to
  Apache-2.0 to match the `LICENSE` file. The README License section was already
  corrected in 0.3.2, but the package metadata was missed; the built wheel now
  reports `License-Expression: Apache-2.0`.

## [0.3.2] - 2026-06-18

### Added

- ask-colleague skill: `monitor`/`guide`/`stop` pilot verbs plus a `--watch`
  flag to dispatch, watch the live feed of, send mid-flight guidance to, and
  cooperatively stop a running colleague flight (re-vendored from colleague).

### Changed

- README: correct the License section from MIT to Apache 2.0 to match the
  `LICENSE` file.

## [0.3.1] - 2026-06-13

### Changed

- CLAUDE.md: add a convention to reach for the `ask-colleague` skill reflexively
  for explore/review/write/grade — read-only `review`/`explore` are always safe;
  side-effecting `write` needs the user's go-ahead.

## [0.3.0] - 2026-06-13

### Added

- AGENTS.colleague.md resident prompt file (backend colleague <-> AGENTS.colleague.md)

### Changed

- Promote agent identity to a colleague resident: culture.yaml backend
  claude -> colleague with a pinned model. The `doctor` backend-consistency
  map gains `colleague` -> AGENTS.colleague.md.

## [0.2.1] - 2026-06-12

### Changed

- **Re-vendored the `ask-colleague` skill from colleague (now 1.7.0, up from the
  0.39.2 sync)** — the wrapper had drifted multiple releases behind origin. Picks
  up the `clean` verb (reap stale/corrupt `colleague/*` branches + orphaned
  `.colleague/` artifacts a crashed run left behind), the `--json` flag on every
  verb (result JSON on stdout, diagnostics/digest on stderr), the
  `_colleague_via_uv` local-dev resolution that honors `--repo`, and the
  tri-state (0/1/2) exit-code contract. `scripts/ask-colleague.sh` + `prompts/`
  are byte-identical to the origin; `SKILL.md` diverges only in the one
  consumer-identifying Provenance clause (`heat-governor vendors from
  guildmaster`). `docs/skill-sources.md` sync row updated to
  `2026-06-12 (colleague 1.7.0, direct)`. Refs: colleague#183, #186.

## [0.2.0] - 2026-06-06

### Added

- **`ask-colleague` skill** (`.claude/skills/ask-colleague/`) — the first-party front door to the `colleague` CLI (the renamed `convertible`). On top of `explore` / `review` / `write` it adds a `feedback` verb (grade a finished work item — the ROI loop), and `write` now **previews by default** in a throwaway worktree (no side effects) unless `--apply` / `--pr` is given. Reach for it reflexively — `review` for a diverse second opinion on a committed diff before opening a PR, `explore` for a fresh read of an unfamiliar area.

### Changed

- **Replaced the `outsource` skill with `ask-colleague`.** `outsource` was renamed to `ask-colleague` upstream ([colleague#148](https://github.com/agentculture/colleague/pull/148)). Because guildmaster has not re-broadcast the rename yet (its kit still ships the old `outsource`), `ask-colleague` is vendored **directly from the sibling `colleague` checkout** rather than from guildmaster — a tracked local divergence recorded in `docs/skill-sources.md`, parallel to the `agex` → `devex` one. Vendored verbatim except one consumer-identifying clause in the Provenance paragraph.
- **Ledger + CLAUDE.md + `.gitignore`:** point `docs/skill-sources.md` and the CLAUDE.md Skills section at `colleague` / `ask-colleague`, swap the *optional* runtime prerequisite `convertible` → `colleague` (env prefix `CONVERTIBLE_*` → `COLLEAGUE_*`, with the legacy names kept as a deprecated fallback), and gitignore the `.colleague/` run-artifact dir the skill writes (plus the stale `.agex/`).

## [0.1.4] - 2026-05-31

### Added

- **Vendor the `outsource` skill** (`.claude/skills/outsource/`) from
  guildmaster's canonical copy (origin
  [`agentculture/convertible`](https://github.com/agentculture/convertible),
  re-broadcast via guildmaster — guildmaster
  [#51](https://github.com/agentculture/guildmaster/pull/51)). Every agent
  cloned from this template now inherits the ability to hand a scoped task to a
  *different* engine/mind: `explore` (read-only investigation), `review` (a
  diverse second opinion on the committed diff), and `write` (delegate a small
  implementation). `explore`/`review` run isolated in a throwaway `git worktree`;
  `write` refuses a dirty tree. Fulfils
  [#8](https://github.com/agentculture/heat-governor/issues/8).
- **Ledger + CLAUDE.md:** record `outsource` in `docs/skill-sources.md`
  (origin = convertible, re-broadcast via guildmaster; vendored verbatim — it
  already carries `type: command`) and document its *optional* runtime
  dependency on the `convertible` CLI (the skill exits with an install hint if
  absent, so a clone that never uses it is unaffected).

### Changed

### Fixed

## [0.1.3] - 2026-05-31

### Changed

- Expanded the clone-and-rename instructions in `CLAUDE.md`: added `README.md` to
  the rename targets and a portable `git grep` discovery command so a cloner can
  find every occurrence of the template name (hard-coded in ~100 places across the
  package, including the CLI command files and `_ISSUES_URL` in
  `heat_governor/cli/__init__.py`) rather than renaming by hand.
- Synced `README.md`'s "Make it your own" checklist with `CLAUDE.md`: it now lists
  `README.md` itself as a rename target and points to `CLAUDE.md`'s discovery
  command as the authoritative procedure, so the two onboarding checklists no
  longer drift.

## [0.1.2] - 2026-05-30

### Changed

- Renamed the PR-lifecycle CLI references `agex` / `agex-cli` to `devex` (same
  tool, new name) across `CLAUDE.md`, `docs/skill-sources.md`, `.gitignore`, and
  the vendored `cicd`, `assign-to-workforce`, and `communicate` skills — the
  `cicd` scripts now invoke `devex pr`.
- Logged the vendored-skill in-place patch as a local divergence in
  `docs/skill-sources.md`; the matching canonical rename is tracked upstream for
  guildmaster in
  [agentculture/guildmaster#48](https://github.com/agentculture/guildmaster/issues/48)
  so a future re-sync reconciles cleanly.
- Aligned the documented `devex` version floor to `>=0.21` across the vendored
  `cicd` `SKILL.md` and `workflow.sh` install hint (were `>=0.1`), matching
  `docs/skill-sources.md` and the `await`-era feature set; flagged upstream on
  guildmaster#48.

### Fixed

- SonarCloud now reports code coverage — added `relative_files = true` to
  `[tool.coverage.run]` so `coverage.xml` emits repo-relative paths that map to
  `sonar.sources=heat_governor` (absolute / `.venv` paths were dropped
  as unmappable). Mirrors the sibling `convertible` setup.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/heat-governor/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/heat-governor/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: heat-governor`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed

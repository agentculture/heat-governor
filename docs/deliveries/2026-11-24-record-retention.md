# Delivery Summary — record retention

plan: `record-retention` · run: `partial` · date: `2026-11-24`
baseline: `devague summary skeleton`

## Intent

> heat-governor keeps a bounded, configurable window of records on every box: days older than a
> day are gzip-compressed, days older than the retention window (default 7) are deleted, so no
> machine's disk fills with telemetry

The run executed the converged plan `docs/plans/2026-11-24-record-retention.md` (spec
`docs/specs/2026-11-24-record-retention.md`, after a rigorous /challenge pass). `t1`-`t9` were
built by a parallel workforce in four waves, each merge gated by the full suite and the CI lint
set. They shipped as heat-governor 0.20.0 in PR #23, squash-merged as `398664a`. `t10`, the live
check, deployed 0.20.0 to all six boxes on 2026-11-24 (PR #24, 0.20.1). The run is `partial`
because `t10`'s seven-day size criterion cannot be measured before about 2026-12-01.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — records readers accept .jsonl.gz in recorder.py (`iter_records`, report, `sensor_stats`, replay)
- `t2` — history.py reads gzipped days and sizes its stride from the gzip ISIZE trailer
- `t3` — retention core: `heat_governor`/retention.py `prune_pass` and `compress_day`
- `t4` — config: `keep_days` and `log_max_mib` in \[daemon\]
- `t5` — memory replay accepts a .jsonl.gz records file
- `t6` — governor.log rotation at `log_max_mib`
- `t7` — daemon: retention thread at UTC rollover, `records_pruned` event, gz-aware postmortem, live reload
- `t8` — records CLI noun: overview and prune
- `t9` — docs and harness files carry the retention facts; version bump
- `t10` — live check: EliteMini first, then the fleet, recorded in docs/evidence

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `open_day()` and `.jsonl.gz` support in `iter_records`, `report`, `sensor_stats` and `replay` (branch commit `c06917e`); a corrupt `.gz` is skipped (review fix, `d2`) |
| `t2` | delivered | the history script reads gzipped days, takes the stride from the ISIZE trailer and lets raw win over `.gz` (`4a9ec4e`); corrupt deflate bodies are skipped (`d2`) |
| `t3` | delivered | `heat_governor/retention.py` with `prune_pass` and `compress_day`, 47 tests (`4a01a69`); a gap guard and `allow_gap` were added after review (`d1`) |
| `t4` | delivered | `[daemon] keep_days` and `log_max_mib` validated, commented in the template and live-reloadable (`4d3932b`); later capped at 3650 and required finite (`d2`) |
| `t5` | delivered | `memory replay` accepts `.jsonl.gz` (`8768e32`) |
| `t6` | delivered | `governor.log` rotates to `governor.log.1.gz` (`7c8fb36`); the cap follows a live reload (main-agent fix `75dff93`) |
| `t7` | delivered | `RetentionThread` with a pass at start and at each UTC date change, the `records_pruned` row, a gz-aware postmortem and `keep_days` reload (`2b5d546`); the test seams later moved onto `Options` (`9066b44`) |
| `t8` | delivered | `records overview` and `records prune [--dry-run] [--keep-days N]`, explain and learn entries, the stale records path in the explain text fixed (`87a05b0`); options before the verb and `--allow-gap` were added after review (`d2`, `d1`) |
| `t9` | delivered | `CLAUDE.md`, the three other harness files, README, `docs/integration.md`, 0.20.0 changelog (`46c2f3d`) |
| `t10` | partial | 0.20.0 deployed to all six boxes and the first passes ran (`docs/evidence/2026-11-24-record-retention-live.md`, PR #24). Not yet done: the seven-day size check (about 2026-12-01), cultureos's first compress (timing and RSS with the shipped code), a pass at a UTC date change, a live log rotation and a live gap check |

## Mid-work Decisions

- `d1` — `prune_pass` never deletes day files before a hole longer than `keep_days` (they are
  still compressed; a 'gap check' warning is recorded each pass); 'records prune --allow-gap'
  deletes across it. After a real downtime longer than the window, up to ~`keep_days`+1 old days
  stay until the operator runs --allow-gap — review of PR 23 (M1): the daemon writes today's file
  before any pass, so the newest-file clock check (c28) could not see a forward jump; a jump to
  2030 deleted every real day (reproduced).
- `d2` — records CLI options given before the verb now reach it; `keep_days` capped at 3650;
  `log_max_mib` must be finite; corrupt .gz days are skipped by `iter_records` and the history
  script — review of PR 23 (M2, S1, S2, S3), each reproduced and fixed test-first; user approved.
- The `governor.log` cap follows a live reload (`75dff93`). `t6`'s closure captured the startup
  config, and no task's instruction covered reload for `log_max_mib` (spec h7). The main agent
  fixed it with a test that fails on the old code. No deviation record covers this.
- An autouse test fixture gives a `Daemon` whose `Options` carry no `prune` a no-op retention pass
  (`17ae86d`). Older daemon tests `start()` without `shutdown()`, and a real pass would race their
  2020-dated fixture days. Test-only; no deviation record.
- A `records_pruned` row is also written for warnings-only passes (`t7`'s choice, behavioral
  delta `b3`). `records prune` exits 2 after printing when a file failed, and exits 0 on a clock
  refusal (`t8`'s choice, `b4`).
- CI fixes before merge (`b82fcfa`, `2aa6eb9`, `9066b44`):
  - scope finding s7 reworded to avoid a hard-coded home path;
  - the five record-retention devague records added to the exact-path portability waiver in
    `scripts/harness-smoke.py`;
  - SonarCloud's 42 new issues fixed: retention seams moved onto `Options`, two helpers for
    cognitive complexity, 36 composite assertions split.
- Deploy: spark2's `uv` had a stale index cache and needed `--refresh`. Every restart was gated on
  level cool, and all six boxes were cool.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t3` (`d1`) | review of PR 23 (M1): the daemon writes today's file before any pass, so the newest-file clock check (c28) could not see a forward jump and a jump to 2030 deleted every real day (reproduced). A jump and a long downtime look the same from the files alone; holding the days before the hole is the safe side. User approved the fix ('Please do') after the review proposed a gap check | acceptable |
| `t8` (`d2`) | review of PR 23 (M2, S1, S2, S3), each reproduced and fixed test-first; user approved ('Please do') | acceptable |
| `t6` | the plan's instruction read the cap from the startup config; `h7` requires `log_max_mib` to follow a live reload; fixed outside every task's file list (`75dff93`) | acceptable |
| `t7` | `Daemon(prune=, retention_check_s=)` keywords moved onto `Options` for SonarCloud S107; it also writes a `records_pruned` row for warnings-only passes, beyond c27 (`b3`) | acceptable |
| `t10` | the seven-day size criterion and cultureos's compress measurement cannot run until about 2026-12-01; the run closes with `t10` partial | needs-follow-up |

## Evidence

- tests: the obligation test files ran green at main `398664a` on 2026-11-24 23:40 UTC (156
  passed): `tests/test_retention.py`, `tests/test_daemon_retention.py`,
  `tests/test_cli_records.py`, `tests/test_config_retention.py`,
  `tests/test_governor_log_rotation.py`, `tests/test_recorder_gz.py`,
  `tests/test_memory_replay_gz.py`, `tests/test_history.py` — pass
- full suite at the PR #23 tip `3086d54`: 1845 passed, 1 skipped (existing cross-repo skip) — pass
- lint: black, isort, flake8, bandit, `teken cli doctor . --strict` (26/26), markdownlint,
  harness-smoke (config locally, toolchain in CI), `scan-secrets.py`; SonarCloud quality gate OK
  with 0 open issues on PR #23 — pass
- evidence records `e1`-`e16` (one per obligation `o1`-`o16`, execution strength, run commit
  `398664a`), `e17` (live observation for `o17`, the `c18` after-state), `e18`-`e21`
  (supplementary records backing deltas `b1`-`b4`) — all approved 2026-11-25
- behavioral deltas `b1`-`b4` (approved); obligation `o18` (`c19`, the seven-day bound) filed
  with no evidence: not yet checkable
- live: `docs/evidence/2026-11-24-record-retention-live.md`. The first passes freed 1.20, 3.34,
  3.32, 3.70 and 3.31 GB on the EliteMini, spark, spark2, thor and orin, with 0 errors. Every dry
  run matched its pass. `/api/history?range=7d` answers from the records on all six.
- commits: `4bc2f35..398664a` on main (PR #23 squashed; branch history `c3a44d0..c7f934e`), plus
  PR #24
- PRs: #23 (merged), #24 (open)

## Delivery Claims

Evidence records `e1`-`e21`, deltas `b1`-`b4`, obligations `o17`-`o18` and lapses `l1`-`l8`
were approved by the operator on 2026-11-25. Where an approved lapse bears on a claim, it caps
that claim at `medium` (named in the row) rather than letting it default to `high`.

| Claim | Confidence | Evidence |
|-------|------------|----------|
| `c2` — retention deletes day files past `keep_days` (today + 7 by default) | high | test `tests/test_retention.py::test_window_boundary_raw_and_gz` · `e1` · live passes in `docs/evidence/2026-11-24-record-retention-live.md` |
| `c3` — completed days older than yesterday are gzip-compressed in place, byte-identical on decompression | high | test `tests/test_retention.py::test_compress_day_roundtrip_and_raw_removed` · `e2` · live: about 30x per day on the EliteMini |
| `c5` — every record reader accepts `.jsonl.gz` | high | tests in `e3` and `e19` · live `/api/history?range=7d` on all six (approved lapse `l3` bears only on the `t1s` replay path, not on the claim as a whole) |
| `c6` — today and yesterday are never compressed or deleted | high | test `tests/test_retention.py::test_today_and_yesterday_never_touched_around_midnight` · `e4` |
| `c7` — files beside `records/` (audit log, applied rules, ledger, spool, ssh) are never touched | high | test `tests/test_retention.py::test_data_dir_siblings_untouched` · `e5` |
| `c10` — `keep_days` and `log_max_mib` are validated, documented and follow a live reload | high | tests in `e6` and `e20` (including `tests/test_governor_log_rotation.py::test_run_options_cap_follows_a_live_reload`) |
| `c20` — `records overview` / `records prune` follow the CLI contract | high | tests in `e7` and `e18` · `teken cli doctor --strict` 26/26 (approved lapse `l6` bears on the per-file-error test only) |
| `c21` — `governor.log` stays bounded with one gzip generation | medium | tests in `e8` · not yet seen live (no log near 10 MiB) |
| `c23` — a crash during compression leaves the raw day or a complete `.gz`; other files are never removed | high | tests in `e9` |
| `c24` — pruning never runs on the tick loop and compresses in 1 MiB chunks | medium | tests in `e10` · approved lapse `l7` (loose tick bound) · cultureos compress timing not yet measured (h16) |
| `c25` — the daemon's pass and `records prune` can run at the same time safely | medium | tests in `e11` · approved lapse `l5` (two passes in one process standing in for two processes) |
| `c26` — expired days go first; a failed compress keeps the raw day and the pass continues | high | tests in `e12` |
| `c27` — a pass writes one `records_pruned` row and log line and never raises into the daemon | high | tests in `e13`, `e21` · live: one `records_pruned` log line per box that changed |
| `c28` — a pass deletes nothing when the clock looks wrong, including a forward jump seen through the daemon | high | tests in `e14` (including `tests/test_daemon_retention.py::test_a_forward_clock_jump_deletes_nothing_through_the_daemon`) · deviation `d1` |
| `c29` — symlinks are never followed and subdirectories are never entered | high | tests in `e15` |
| `c30` — the history stride uses the uncompressed size | medium | tests in `e16` · approved lapse `l2` (the script's own size value is checked only through the stride) |
| `c18` — the six live boxes hold today and yesterday raw and older days gzipped, and the readers work | medium | `e17` · `docs/evidence/2026-11-24-record-retention-live.md` (cultureos has not compressed yet) |
| `c19` — after seven days: at most 8 day files, at most 2 raw, under 300 MB per box (under 600 MB on cultureos) | unverified | `o18` filed with no evidence — not claimed done |

Lapse ledger evidence:

| Lapse | Code | What |
|-------|------|------|
| `l1` | `control-absent` | t2 implemented history.py before writing its tests (not test-first); afterwards it re-ran the new tests against the stashed old code: 2 of 3 failed as they should, the raw-wins-over-gz test passed on old code (guards, does not prove) |
| `l2` | `grader-unverified` | t2's ISIZE test checks the trailer in the test itself; its second assert only greps `HISTORY_SCRIPT` for 'ISIZE' or 'little' and never reads the script's own size value; the script's ISIZE use is covered only indirectly by the stride-equality test |
| `l3` | `grader-unverified` | t1's t1s-\*.log replay test replays an empty file, so it checks the format dispatch still runs, not t1s sample parsing through `open_day` |
| `l4` | `control-absent` | t3 wrote retention.py before its tests (not test-first); mitigated afterwards by seven deliberate mutations (clock check off, tmp age gate off, symlinks followed, rename removed, compress-before-delete, compressing yesterday, whole-file reads), each caught by the suite |
| `l5` | `assumption-for-measurement` | o11 (concurrent passes) is tested with two passes interleaved inside one process (pid= argument + monkeypatching), standing in for two real processes |
| `l6` | `grader-unverified` | t8's per-file-error exit-2 test monkeypatches retention.`prune_pass` to append an error instead of injecting a real compress failure (`prune_pass` binds its compress default at definition time), so that path is checked at the CLI level only |
| `l7` | `assumption-for-measurement` | t7's cadence test bounds each tick at < 0.5 s while the pass is blocked; the bound is loose and was not measured on a loaded CI runner, and cadence is shown by tick() returning promptly, not by the pragma-no-cover run() loop |
| `l8` | `control-absent` | main agent opened PR 23 after running only harness-smoke --stage config and no Sonar check; CI's toolchain stage (steward doctor portability) failed twice on text the main agent wrote (a hard-coded home path in s7, per-user state-dir refs in the new records and CHANGELOG), and Sonar flagged 42 new issues |

## Remaining Work / Follow-up

- `t10` — around 2026-12-01, measure all six boxes against `c19` (`o18`): day files, raw count,
  `du` of `records/` and `governor.log*`. Then file the evidence and update the live evidence
  file. Owner: the main agent, with the operator's go-ahead to read the boxes.
- `t10` — cultureos's first compress (the 20261123 day, two UTC rollovers after the deploy): read
  its `records_pruned` row and the daemon's CPU and RSS during that pass (`h16`).
- Open findings from the PR #23 review, not fixed:
  - S4: `governor.log` rotation has no lock across the daemon's threads and no backoff after a
    failed rotation.
  - S5: a day present as both raw and `.gz` is read twice by `iter_records`.
  - `_scan`'s stat can fail a whole pass under a concurrent pass.
  - A live temp file can look stale to a concurrent pass (spurious error, no loss).
  - `replay` / `memory replay` on a truncated `.gz` give a generic error.
- Parked in the frame: `v1` (history over gzipped days on slow boxes, unmeasured), `v2`
  (first-rollout burst, observed fine: 0 errors), `v3` (a downgrade hides `.gz` days), `v4`
  (backdated rows), `v5` (records disk usage in `mesh status` and the dashboard).
- PR #24 (0.20.1, live evidence and this summary): review and merge.

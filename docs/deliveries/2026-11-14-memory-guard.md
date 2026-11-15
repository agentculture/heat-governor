# Delivery Summary — memory guard

plan: `memory-guard` · run: `complete` · date: `2026-11-14`
baseline: `devague summary skeleton`

## Intent

> heat-governor catches a runaway process before it takes the machine down: when memory is
> about to run out it kills the biggest own-user grower, records who used the memory and what
> started it, and keeps that record in S3 so a dead machine's last minutes survive off-box.

This was prompted by the 2026-11-14 EliteMini reset. A test grew to about 41 GB five times in
17 minutes, and the box thrashed for 16 minutes until it was reset. heat-governor had recorded
memory but acted on none of it and kept no per-process data. The run executed the converged
plan `memory-guard` (13 tasks in 6 waves, `docs/plans/2026-11-14-memory-guard.md`) through
`/assign-to-workforce`. It shipped as PR #14: 52 commits, squash-merged to `main` as `91aa846`
on 2026-11-15 and released as 0.17.0.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — memtrigger.py: pure trigger (floor + slope/horizon) and a replay over recorded series
- `t2` — memprocs.py: /proc process scan (anon RSS, uid, parent chain, start time, cgroup), growth, grouping, top-N, redaction
- `t3` — sigv4.py: stdlib AWS Signature V4 signer
- `t4` — config.py: strict \[memory\] and \[ship\] sections
- `t5` — recorder.py: thread-safe writes and memory in the reboot report
- `t6` — memkill.py: victim choice, pidfd-pinned SIGKILL, protect list, kill budget, repeat offenders
- `t7` — ship.py: S3-compatible client, credentials, key layout, black box, spool and retry
- `t8` — memguard.py: the guard thread (poll, trigger, kill, OOM counters, top snapshots, mlockall)
- `t9` — daemon.py: start/stop guard and shipper, event rows, top field, `mem_guard` in the state doc
- `t10` — CLI: memory (overview, status, top, replay) and ship (overview, test, fetch); report and unit updates; explain/learn/overview
- `t11` — Docs, version 0.17.0, CHANGELOG and a green CI
- `t12` — Deploy: MinIO on spark, a scoped heat-governor key, EliteMini profile and config (user go-ahead)
- `t13` — Live validation on the EliteMini: controlled runaway, black box from another box, evidence doc (user go-ahead)

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `heat_governor/memtrigger.py` plus the incident fixture `tests/data/memory-2026-11-14.csv` (t and avail only). The lead-time criterion was relaxed by `d1`. Later gained `rearm()` (code review #2) and absolute limits (`d3`). |
| `t2` | delivered | `heat_governor/memprocs.py`: anonymous RSS, uid, parent chain, start time, cgroup, growth tracker, groups, top-N and `redact()`. Checked once against the real `/proc` (429 processes, ~34 ms). |
| `t3` | delivered | `heat_governor/sigv4.py`. It reproduces AWS's documented GET Object signature. The agent returned no report; the main agent re-checked the work (lapse `l2`). |
| `t4` | delivered | Strict `[memory]` and `[ship]` validation, an http endpoint allowed only for loopback and the tailnet. Extended by `d3` (`floor_mib`/`soft_mib`) and `d4` (`[[memory.allow]]`, `act_over_pct`). |
| `t5` | delivered | Lock-serialized `Recorder.write`, a `mem` block and `events` in reboot entries, and `tail()`. Code review #3 made `tail()` read backwards from the end of the file. |
| `t6` | delivered | `heat_governor/memkill.py`: pidfd-pinned SIGKILL, protect list, kill budget, repeat offenders and group fallback. Code review #1 made the choice growth-based, and `d4` added a boundary tier first. |
| `t7` | delivered | `heat_governor/ship.py` and `tests/fake_s3.py`: SigV4 client, credentials, object layout, spool and retry, black box. Sonar refactors added `ShipperTuning`. The postmortem is keyed under the crashed boot (code review #8). |
| `t8` | delivered | `heat_governor/memguard.py`: guard thread, OOM counters, scans only when needed. Later added retry with backoff (code review #2), `mlockall(MCL_CURRENT)` (code review #4), and blame and boundaries (`d4`). Written code-first (lapse `l1`). |
| `t9` | delivered | Daemon wiring, `mem_guard` in the state doc, postmortem from a bounded scan of the last two day files. Code review #5 added `heat_governor/memsink.py` and code review #6 keeps the kill budget across reloads. |
| `t10` | delivered | `memory overview\|status\|top\|replay` and `ship overview\|test\|fetch`, memory in `report`, `MemoryMin=128M` in the unit, catalog, learn and overview text. |
| `t11` | partial | Docs, 0.17.0, CHANGELOG and the `d2` CLI fix all landed, and tests, lint, version-check, SonarCloud and the Sonar gate (zero issues) are green. Two checks stayed red: `harness-smoke` (an external steward rule change, steward#87) and GitGuardian (incidents in two earlier commits still to be marked as test data). |
| `t12` | delivered | MinIO on spark and a scoped key. Beyond the plan, the guard is enabled on **all six** boxes (`d3`), and a second MinIO on arduino holds spark's evidence (user decision). |
| `t13` | delivered | The controlled runaway was killed with 8.7 GB available (re-run on the final build: 8.8 GB), max tick gap 1.0 s, the event in S3 in the same second, and `ship fetch` from spark. Evidence: `docs/evidence/2026-11-15-memory-guard-live.md`. |

## Mid-work Decisions

- `d1` — the `t1` criterion "each fire at least 15 s before avail < 2048 MiB" became "fires before 2048 MiB on all five waves, each >= 4 s ahead, with >= 5 GB available at the fire". The trigger defaults stay horizon 10 s / soft 20%. Reason: measured leads were 17.6/7/5/6/4 s. Reaching 15 s needed horizon 25 s / soft 50%, which fires with 15-21 GB free and is more likely to kill legitimate large allocations. The 15 s figure was an unmeasured margin.
- `d2` — `t11` also fixed the CLI run path, so `[memory]`/`[ship]` apply with `[daemon] reload = false`. Reason: `t9` found that the CLI passes `config=None` when live reload is off, and no task covered the CLI side.
- `d3` — memory limits became absolute and per-platform (spark/thor 3072/6144 MiB, orin 2048/4096, ventunoq 1024/2048, generic 5%/20%), and kill was enabled on all six boxes. Reason: the user said "Limits need to be configured and enabled; some machines like Thor are full for AI load". thor sat at 8% free, so the percentage defaults would fire constantly.
- `d4` — blame (top growers of any user) and `[[memory.allow]]` acceptable boundaries. Reason: the user said "We need to take into consideration growing processes so we can blame them. Alternatively, acceptable boundaries for a specific process (with multiple of them)."
- No deviation record covers the code review fixes. After `/code-review medium` on PR #14 (10 findings), the user approved fixing #1-#6, #8 and #9 and redeploying; #7 and #10 became issues #15 and #16.
- No record covers spark's ship target. spark ships to a second MinIO on arduino, so no box ships to itself (user decision; it resolved risk `r3`).
- No record covers a test the `t1` agent weakened. It pinned the lead-time test at >= 3 s instead of reporting the shortfall; the main agent replaced it with the `d1` bounds (>= 4 s, >= 5 GB).
- No record covers the shape of `t9`'s postmortem. It is built from a bounded scan of the last two day files with a clean-stop check, not `recorder.report()`. That was the brief's instruction; `report()` would have parsed all ~363 MB of records.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|------------------------|-----------------|
| `t1` (`d1`) | Measured on the 2026-11-14 fixture: default leads are 17.6/7/5/6/4 s with 5423-7097 MiB available at the fire; reaching 15 s on every wave needs horizon 25 s / soft 50%, which fires with 15-21 GB free and raises the risk of killing legitimate large allocations. The 15 s figure was an unmeasured margin. | acceptable |
| `t11` (`d2`) | t9 found that with reload = false the CLI passes config=None, so the daemon never sees \[memory\]/\[ship\]; no task covered the CLI side. | acceptable |
| `t4` (`d3`) | thor 10.5 GB of 126 free (8%), spark 12.2/125, spark2 9.8/125, orin 8.3/63: percentage defaults would fire or scan continuously; user chose absolute per-platform limits and kill everywhere. | needs-follow-up |
| `t6` (`d4`) | User asked for growth-based blame and per-process acceptable boundaries after the code review. | needs-follow-up |
| `t11` | CI not fully green: `harness-smoke` fails on `main` too (a newer steward flags vendored skill paths, steward#87), and GitGuardian incidents on two earlier commits await marking as test data. | needs-follow-up |
| `t12` | Scope grew from "EliteMini only" to all six boxes plus a second MinIO on arduino (user decisions, recorded with `d3`). | acceptable |
| `t6`, `t8` | Post-plan code review fixes changed victim choice (growth-based), added retry with backoff, and narrowed `mlockall`. These behaviours differ from the confirmed `t6`/`t8` text, and no deviation record covers them. | acceptable |

## Evidence

Every test below ran agent-side on `main` at `91aa846` (2026-11-15T07:03Z) and passed. They are filed as devague evidence `e1`-`e13`, approved by the operator on 2026-11-15.

- tests: `tests/test_daemon_memory.py::test_level_admit_and_factor_identical_with_and_without_memory`, `::test_state_doc_has_mem_guard_only_with_memory_and_clients_still_read_it`, `tests/test_client_state.py` (10) — pass (`o1`, `e1`)
- tests: `tests/test_recorder_memory.py` (7) — pass (`o2`, `e2`)
- tests: `tests/test_ship.py` layout/postmortem (7) and `tests/test_cli_memory_ship.py` fetch (7) — pass (`o3`, `e3`); live `ship fetch` from spark — pass (`e4`)
- tests: `tests/test_sigv4.py` (9, including the AWS GET Object example) and `tests/test_ship.py` round-trip/skew (2) — pass (`o4`, `e5`); live `ship test` on all six boxes plus the scoped-key refusal against MinIO — pass (`e6`)
- tests: `tests/test_memkill.py` and `tests/test_memkill_boundary.py` (48, including a real `sleep 60` child killed through the pidfd path) — pass (`o5`, `e7`); live runaway kills at 04:35Z and 06:16Z — pass (`e8`)
- tests: `tests/test_config_memory_ship.py` and `tests/test_config_memory_allow.py` (111) — pass (`o6`, `e9`)
- tests: `tests/test_cli_memory_ship.py` (35) and `teken cli doctor . --strict` (exit 0) — pass (`o7`, `e10`)
- tests: `tests/test_memprocs.py -k redact` (3), `tests/test_ship.py -k secret/world` (3), `tests/test_memkill.py -k redact/raw` (1) — pass (`o8`, `e11`)
- tests: `tests/test_memtrigger.py::test_replay_lead_time_before_danger` and a live `memory replay` over the 2026-11-10..13 records (0 fires in about 345k samples) — pass (`o10`, `e13`); the live `c26` signal — pass (`o9`, `e12`)
- full suite on the PR head: 1247 passed, 1 skipped (the existing cross-repo report-only skip); coverage 94% at the 0.17.0 docs commit
- lint: black, isort, flake8, bandit, markdownlint, scan-secrets, `harness-smoke --stage config` — clean; Sonar gate on PR #14 — "no unaccepted issues, no hotspots to review"
- commits: `8c841cf..91aa846` (squash of 52 PR commits)
- PRs / issues: #14 (merged), #15, #16 (follow-ups), agentculture/steward#87 (harness-smoke)

## Delivery Claims

Every row is backed by approved evidence (`e1`-`e13`): a test that ran or a live observation. Two approved lapses bear on the claims:

- **`l1` (control-absent):** the `t8` guard-thread tests were written after the code and never seen failing first. Mutation checks were reported in mitigation. This caps at **medium** any behaviour backed only by `tests/test_memguard.py`, such as retry with backoff and OOM-counter events. The live kill (`e8`, `e12`) does not rest on those tests, so `c26` stays high.
- **`l2` (provenance-missing):** the `t3` signer agent left no report; the main agent re-verified it against AWS's published example before merge. This bears on `c10`, which is already medium.

| Claim | Confidence | Evidence |
|-------|------------|----------|
| `c4`/`c30`/`c33` — the guard kills only eligible own-user processes, picked by growth, through a pidfd, within 3 kills per hour | high | `tests/test_memkill.py` · `tests/test_memkill_boundary.py` · live kills (`e7`, `e8`) |
| `c26` — a ~1.2 GB/s runaway is killed with >= 2 GB available, no tick gap > 2 s, the event in S3 within 10 s | high | `docs/evidence/2026-11-15-memory-guard-live.md` (`e12`) |
| `c27` (as amended by `d1`) — fires on all five incident waves >= 4 s ahead with >= 5 GB available, and 0 times on 2026-11-10..13 | high | `tests/test_memtrigger.py::test_replay_lead_time_before_danger` · live replay (`e13`) |
| `c8` — new record rows carry `event`; existing readers parse them unchanged | high | `tests/test_recorder_memory.py` (`e2`) |
| `c9`/`c35` — events, black box and postmortem ship off-box under `<prefix>/<machine>/<boot_id>/`, never the hostname | high | `tests/test_ship.py` · live `ship fetch` from spark (`e3`, `e4`) |
| `c10` — any S3-compatible store works (AWS, R2, MinIO) | medium | AWS example signature test plus live MinIO (`e5`, `e6`). R2 and AWS S3 were never run live. |
| `c11` — `[memory]`/`[ship]`/`[[memory.allow]]` are strictly validated | high | `tests/test_config_memory_ship.py` · `tests/test_config_memory_allow.py` (`e9`) |
| `c12` — the `memory`/`ship` CLI follows the agent-first contract | high | `tests/test_cli_memory_ship.py` · teken doctor exit 0 (`e10`) |
| `c15` — `mem_guard` is additive; level, admit and factor are unchanged | high | `tests/test_daemon_memory.py::test_level_admit_and_factor_identical_with_and_without_memory` (`e1`) |
| `c18` — uploads carry no secrets; command lines are redacted and truncated | high | `tests/test_memprocs.py` · `tests/test_ship.py` · `tests/test_memkill.py` (`e11`) |
| `c2`/`c7` — the guard thread retries a failed kill with backoff and records every kernel or cgroup OOM kill | medium | `tests/test_memguard.py` (capped by approved lapse `l1`) |
| `d3` — per-platform limits keep the AI boxes calm at 8-13% free | medium | all six boxes armed with their platform limits and no fires since rollout (`docs/evidence/2026-11-15-memory-guard-live.md`); no runaway tested on an AI box |
| `d4` — blame and boundaries work as specified | medium | 107 new tests (`tests/test_memboundary.py`, `tests/test_memguard_boundary.py`, `tests/test_memkill_boundary.py`, `tests/test_config_memory_allow.py`); blame seen live on the EliteMini re-run. No `[[memory.allow]]` rule is configured on any box yet. |
| the postmortem after an unclean boot lands under the crashed boot | low | unit tests only (`tests/test_daemon_memory.py::test_unclean_previous_boot_submits_a_postmortem_once`); never fired live |
| a multi-worker runaway is caught by the group fallback | low | unit tests only (`tests/test_memkill.py`); never run live |

## Remaining Work / Follow-up

- `t11` — mark the GitGuardian incidents (40094428 and 40102132, fake test values in earlier commits) as test data, and land steward#87 so `harness-smoke` goes green on `main`. Owner: operator / steward.
- `[[memory.allow]]` boundaries are configured on no box. They need the operator's limits (for example vLLM on the sparks and thor, the orin services), then a deploy.
- R2 and AWS S3 compatibility is untested live. Run `ship test` against R2 before moving any box there, and decide S3 retention first (R2 bills storage).
- The postmortem and group-fallback paths have never fired live. Watch for the first unclean boot; optionally run a multi-worker runaway inside a `MemoryMax` scope.
- The platform limits (`d3`) are declared from 2026-11-15 readings, not measured under a runaway on an AI box.
- Issue #15 (`memory replay` should honour platform limits) and issue #16 (shared reboot detector, deque caps).
- The boundary watch state (once-only crossing and far-over marks) resets on a config reload, so expect one repeat row per reload. It could be carried across like the kill budget.
- `memory_boundary_exceeded` ships per process instance, so a tight rule on short-lived workers could upload often. There is no rate limit yet.
- There is no notification on `memory_kill` (mesh or dashboard), which is a fleet-view follow-up.
- The devague records are adjudicated: deviations `d1`-`d4`, lapses `l1`/`l2`, evidence `e1`-`e13`, obligations `o9`/`o10` and deltas `b1`-`b7` are approved (2026-11-15).
- The merged `feat/memory-guard` and `memory-guard/*` branches are still present locally and on the remote. Delete them at the operator's discretion.

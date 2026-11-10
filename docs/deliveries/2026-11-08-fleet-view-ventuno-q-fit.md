# Delivery Summary — fleet view + VENTUNO Q fit

plan: `fleet-view-ventuno-q-fit` · run: `complete` · date: `2026-11-08`
baseline: `devague summary skeleton`

Written 2026-11-10, after PR #11 was squash-merged as `7f9e6e3` and 0.16.0 was
published to PyPI and rolled out to all six machines. The run is `complete`:
every planned task merged. The three failed obligations below (o7, o8, o9) are
reported as failures, not folded into the pass count.

Every evidence record (`e1`–`e28`), behavioural delta (`b1`–`b8`) and lapse
(`l1`–`l7`) cited here was filed `--origin llm`. All of them are **proposed** and
await the operator's `--confirm` / `--reject`. Until then the Delivery Claims
table reports confidence provisionally: no proposed record is evidence yet.

Superseded records: `e21`–`e28` replace `e1`, `e2`, `e5`, `e7`, `e9`, `e12`,
`e14` and `e18`, and `b6`–`b8` replace `b1`, `b2` and `b5`. The PR #12 review
found that those originals carried approximated run timestamps. The checks were
re-run with UTC times captured at run time, and lapse `l7` records the slip.
`e2`, `e5` and `e14` were also narrowed to exactly what was exercised. Only the
replacements are cited below.

**Outcome count:** 17 obligations pass and 3 fail (`o7`, `o8`, `o9`). Three of
the passes are **partial checks**: `o2`, `o5` and `o14` each have a part that
was never exercised. Those parts are unverified, not passed.

## Intent

> heat-governor runs natively on the VENTUNO Q, governs all five mesh boxes (cultureos, spark, spark2, thor, orin), and one React dashboard plus an MCP server and CLI on cultureos show every box live

After: from cultureos, one command (heat-governor mesh status), one MCP server (heat-governor mcp) and one web page (heat-governor mesh serve, which is the fleet reader the other two query) show every machine's live temperature, level, admission and trends, and the VENTUNO Q is detected and watched with its own profile in dry-run

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — VENTUNO Q fake sysfs fixture in tests/fakes.py
- `t2` — detect() reads device-tree compatible and resolves a built-in ventunoq profile
- `t3` — sense reads the Adreno GPU via devfreq and board power via ina232; fix fan ownership text
- `t4` — gpu-clock refuses unsupported platforms before touching the root helper
- `t5` — fleet.toml loader with strict host validation
- `t6` — SSH transport with reader-managed persistent connections
- `t7` — fleet reader: concurrent polling, staleness, compact 24 h history, reader health
- `t8` — mesh serve HTTP server: same-origin page, JSON API, 127.0.0.1 by default
- `t9` — dashboard page built to the locked design, Polar night only, vendored assets
- `t10` — mesh noun: overview, status, serve; status queries mesh serve and falls back to a direct read
- `t11` — heat-governor mcp: read-only MCP server over stdio
- `t12` — docs, harness mirrors and version bump
- `t13` — install on cultureos in dry-run, run mesh serve, verify all five machines

## Actual Delivery

All 13 tasks are accounted for. Everything landed in PR #11, squash-merged to
`main` as `7f9e6e3`.

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `VENTUNOQ_ZONES` and the compatible/model device tree in `tests/fakes.py`. Its trip types were guessed at first and later corrected (`l1`). |
| `t2` | delivered | `platforms.py` reads `/proc/device-tree/compatible` before nvidia-smi and resolves the built-in `ventunoq` profile. Live: cultureos reports `ventunoq`, and the other boxes' detection is unchanged (`e10`). |
| `t3` | delivered | `sense.py` gains the `devfreq` GPU mode and reads board power from the `ina232` hwmon. Fan ownership reports a kernel-driven fan; the first heuristic was corrected after a live probe (`l3`). |
| `t4` | delivered | `gpu-clock lock`/`reset` refuse ventunoq/devfreq profiles with exit 1. The PR #11 review then added `hold`, live reload and crash recovery to the guard (`b3`, `e11`). |
| `t5` | delivered | `heat_governor/fleet.py`: `fleet.toml` loading with strict `validate_host`. Its tests were never shown red first (`l2`). |
| `t6` | delivered | `heat_governor/transport.py`: `SshTransport` with a reader-managed ControlMaster, plus `LocalTransport`. `LocalTransport` was shipped untested (`l4`); it now has `tests/test_transport_local.py`. Output caps added in review (`b4`). |
| `t7` | delivered | `heat_governor/fleet_reader.py`: one thread per machine, ok/stale/unreachable on the reader clock, a 24 h ring buffer, and reader health. Widened by `d1`, `d2` and `d3`; hardened in review (`b4`). |
| `t8` | delivered | `heat_governor/mesh_server.py`: the page, `/api/fleet` and `/api/history` on 127.0.0.1:9480 by default. Review added a 503 limit past 4 concurrent history reads and tracebacks for unexpected errors. |
| `t9` | partial | `heat_governor/web/index.html` is built to the locked design, Polar night only, with React, htm and Lato vendored. Missing: the light compare palette fails the dataviz validator (`e25`), and the "not supported" machine state cannot be produced (`e8`). |
| `t10` | delivered | `heat_governor/cli/_commands/mesh.py`: `mesh overview`, `mesh status` and `mesh serve`, with `status` falling back to a direct read. Merged despite a surviving mutant, which was fixed minutes later (`l5`, `l6`). |
| `t11` | delivered | `heat_governor/mcp_server.py` and `cli/_commands/mcp.py`: read-only tools `mesh_status`, `machine_state` and `sense` over stdio. |
| `t12` | delivered | Docs, the harness mirrors and the version bump to 0.16.0 (CHANGELOG). `docs/HANDOFF-2026-11-09.md` was updated to say the fleet runs under core. |
| `t13` | delivered, then moved | Installed on cultureos in dry-run, with `mesh serve` verifying all five machines. On 2026-11-09 the operator moved the one fleet reader to core (see drift). cultureos's daemon still runs in dry-run. |

## Mid-work Decisions

- `d1` — add per-machine memory state to the fleet view: the reader carries mem used %, available, derived total and swap (already published by every daemon's /state sensors, 0.13.0 included) plus memory temperature where a DDR/DRAM/EMC/SDRAM thermal zone exists (VENTUNO Q ddrss-\*; not exposed on Jetson or GB10), stores memory % in the compact history, and the page, mesh status and the trend tooltip/table show a Memory fact with an (i) hint — user request on 2026-11-09 after the run; probe showed memory already in every daemon's state document, memory temperature only on the VENTUNO Q
- `d2` — trend history comes from each machine's own heat-governor record files (records/YYYYMMDD.jsonl, 18 days on the remote boxes): the reader runs a second fixed, read-only, niced downsampling command over the existing SSH connection (numeric arguments only) and cultureos reads its own files locally; results cached briefly; the reader's in-memory buffer becomes the fallback; the trend panel gains a 7d range. No daemon changes. — user on 2026-11-09: history must persist per machine; the reader-only buffer (q6) is wiped on every mesh serve restart while every daemon already records 1 Hz history to disk; decider 0.51 `ssh_backfill` / 0.47 plus persist, remote load acceptable P=0.78; user approved with a 7-day range
- `d3` — show each machine's GPU clock, GPU power, GPU utilisation and board power: the reader carries `gpu_mhz`, `gpu_power_w`, `gpu_util_pct` and `power_w` derived from the /state sensors (cultureos devfreq clock + ina232 board power; Spark sm clock, power, util; Jetson gpc clock); the page's GPU fact shows temp · clock · power · util and the clock ceiling only when one is set; the Power fact shows board power — user request 2026-11-09: cultureos GPU clock and power were read by its daemon but dropped by the reader (run-state follow-up 4); the page's GPU fact showed the ceiling helper status instead of the clock
- **The PR #11 review fixes were not in the plan.** Qodo raised 9 findings and all were fixed with tests written first, recorded as deltas `b3`, `b4` and `b8`:
  - the GPU helper is guarded on reload, recovery and hold;
  - SSH output is capped and history reads are coalesced, with 503 past 4 in flight;
  - a box never gets two pollers, and a running read never has its connection closed;
  - the dashboard's Fan and Zones facts are filled;
  - errors are logged with tracebacks.

  SonarCloud's 179 first-push issues were cleared, and 0 are open at merge. Why: the cicd skill requires every review thread to be answered and the Sonar gate to pass.
- **The fleet reader moved to core, tailnet-only (operator, 2026-11-09).** `mesh serve` runs on core (the EliteMini) with `--listen 100.95.106.3:9480`, so the page is at `http://core:9480` from any tailnet device and not on the LAN. cultureos's `heat-governor-mesh` was stopped once the core site was confirmed working. Why: the operator wanted to review the fleet from their own desk. Exposure is tailnet-only because the server has no authentication.
- **0.16.0 was rolled out to every box, one at a time (operator go-ahead, 2026-11-10).** Order: orin, thor, spark2, spark. Before each restart, the box's own config was checked with the new version. The root GPU helper was unchanged, so no sudo step was needed. The Spark tensor-parallel pair re-formed across mixed versions mid-rollout.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t7` (`d1`) | user request on 2026-11-09 after the run; probe showed memory already in every daemon's state document, memory temperature only on the VENTUNO Q | acceptable |
| `t7` (`d2`) | user on 2026-11-09: history must persist per machine; the reader-only buffer (q6) is wiped on every mesh serve restart while every daemon already records 1 Hz history to disk; decider 0.51 `ssh_backfill` / 0.47 plus persist, remote load acceptable P=0.78; user approved with a 7-day range | acceptable |
| `t7` (`d3`) | user request 2026-11-09: cultureos GPU clock and power were read by its daemon but dropped by the reader (run-state follow-up 4); the page's GPU fact showed the ceiling helper status instead of the clock | acceptable |
| `t9` | o7's four skins were deliberately not built: c18 replaced them with Polar night only (`e24` fail, `b6` removed) | acceptable |
| `t9` | The plan instruction said "re-run the dataviz validator for the two Polar night modes". Nothing records that it was run. Run on 2026-11-10, light `--c1..--c6` FAILS: `#eda100` is at 2.04:1 on the chart surface, below 2.5:1 (`e25`). | needs-follow-up |
| `t9` | o8: the page has a `state: "unsupported"` branch, but no fleet config or reader path can produce it, so a machine heat-governor cannot read never shows as "not supported" (`e8`) | needs-follow-up |
| `t13` | The one fleet reader now runs on core, not cultureos (operator, 2026-11-09). The map's hub is the reader's local machine (`b7`), and the page is bound to the tailnet address instead of loopback. No deviation record covers this; it was decided after the run. | acceptable |
| `t4`, `t6`, `t7`, `t8`, `t9` | PR #11 review hardening beyond the plan (`b3`, `b4`, `b8`); no deviation record covers it | acceptable |

## Evidence

- **All run timestamps are UTC.** Timestamps filed without a `Z` (the suite run) are UTC too.
- **Full test suite at `7f9e6e3`:** 841 passed, 1 skipped (`uv run pytest -n auto`, 2026-11-10T15:33:35Z). The skip is the report-only cross-repo check `tests/test_harness_registries.py:170`.
- **Red check on the pre-fix source (`ddb72a3`):** `tests/test_gpuclock_unsupported_paths.py` gave 11 failed and 3 passed. The 3 passes are the Spark controls.
- **Red check before the reader fixes:** `tests/test_fleet_reader_qodo.py` gave 9 failed and 1 passed (the different-ranges control). `tests/test_transport.py`'s flood tests hung on uncapped output.
- **Lint and gates on PR #11's last head (`e650cde`):**
  - black, isort, flake8 and bandit are clean;
  - `teken cli doctor . --strict` passes;
  - `scripts/scan-secrets.py` is clean;
  - harness-smoke passes;
  - SonarCloud has 0 open issues and 0 hotspots.
- **Dataviz validator** (`validate_palette.js`) on the page tokens, run at 2026-11-10T15:54:59Z:
  - light c1–c6: FAIL;
  - dark c1–c6: WARN (two colours too close under colour-blind simulation);
  - light s1–s2: WARN;
  - dark s1–s2: PASS.
- **Browser session on `http://core:9480`** (Chrome). The page's own "Updated" stamp read 15:35:54Z at the start and 15:38:14Z at the end, and records filed from it carry 15:38:14Z. Covered:
  - layout, map, platform filter, Rows/Grid and the Fan hint;
  - trend panel: ranges, tiles, crosshair, arrow-key stepping, level strip, admitting area, time-by-level and compare;
  - Light mode is cream; no theme picker;
  - no console errors.
- **Live fleet:**
  - all six machines run 0.16.0 and their daemons answer, so the reader shows `ok`. Five of them sense real temperatures. core's only zone is an ACPI `acpitz` stuck at 20 °C, so its row is a placeholder and core's thermal state is **not** monitored;
  - the cultureos dry-run is clean: `applying: false`, `NanoCpus=0`, and 0 pause/cap actions in 3 days (2026-11-10T15:54:59Z);
  - the daemon tick interval is unchanged with the reader polling: median 1.000 s and p99 ≈1.08 s, both on 2026-11-05/06 and on 2026-11-10 (spark2 records, 2026-11-10T15:55:05Z). A slow or hung reader was not exercised;
  - core keeps 5 persistent ControlMaster sockets in a 0700 directory (2026-11-10T15:55:00Z);
  - the Spark pair re-formed after each restart.
- **Commits:** `7f9e6e3` on `main` (the squash of `fleet-view/integrate`, last head `e650cde`).
- **PRs:** #11 (merged), with 9 Qodo threads all replied to and resolved. PyPI: heat-governor 0.16.0.

## Delivery Claims

Confidence is provisional until the operator adjudicates the cited evidence records. The proposed lapses are named where they bear on a claim; they are not evidence yet.

| Claim | Confidence | Evidence |
|-------|------------|----------|
| `c2` — the VENTUNO Q is detected from device-tree compatible and gets the `ventunoq` profile; other platforms are unchanged | high | `e10` · `tests/test_platform_ventunoq.py` · live: cultureos `ventunoq` |
| `c5` — the GPU clock is never driven on the VENTUNO Q; the root helper is never spawned on any path | high | `e11` (sensitivity: red at `ddb72a3`, green at `7f9e6e3`) · `tests/test_gpuclock_unsupported_paths.py` · `tests/test_gpuclock_platform.py` |
| `c7` — cultureos is installed and governed in dry-run; it never pauses or caps | high | `e26` · `tests/test_actuators_daemon.py::test_dry_run_changes_nothing` · live inspection on cultureos |
| `c8` — one reader polls every box over SSH; a hung box never delays the others | high | `e13` · `tests/test_fleet_reader.py::test_one_thread_per_machine_and_hung_box_does_not_delay_others` |
| `c9` — the fleet view is outside every daemon's control path; the tick is unchanged | low | `e27`: a live observation from records, reader running vs absent only. The "reader slow" case named in o14 was never exercised, and no automated test pins any case. |
| `c13` — the `mesh` noun and `mcp` verb keep the CLI contract; the strict rubric is green | high | `e15` · `tests/test_cli_mesh.py` · PR #11 lint job. The `l5`/`l6` gaps were fixed with tests before the merge. |
| `c14` — the MCP server is read-only over stdio, with only JSON-RPC on stdout | high | `e16` · `tests/test_mcp.py::test_tools_list_names_and_schemas` |
| `c17` — the page is built to the locked design (layout, list controls, trend panel, compare) | medium | `e21`, `e3`, `e6` (browser + tests) · `e22` and `e23` are partial: search, sort, count-chip filtering and the 6-machine compare limit were not exercised · `e4` is source-level only (critical animation not observed) · `e8` fails (no "not supported" state) |
| `c18` — the page ships Polar night only: cream light and slate dark, Auto plus Light/Dark, Lato, no theme picker | medium | Observed in the browser session (recorded in `e25`'s behaviour) · `tests/test_web_page.py::test_polar_night_tokens_both_modes`. c18's only obligation, `o9`, **failed** on the palette (`e25`, next row), so the claim is not rated high. |
| `c18` / `o9` — the chart palette was validated with the dataviz validator for both modes before shipping | unverified — failed | `e25`: light c1–c6 FAILS (`#eda100`). Not claimed done. |
| `c25` — every fleet host is validated; a host can never become an ssh option | medium | `e17` · `tests/test_fleet_config.py::test_bad_hosts_rejected` · ran green, but the tests were never shown to fail (`l2`, proposed) |
| `c26` — persistent, reader-managed SSH connections with BatchMode and timeouts | high | `e28` · `tests/test_transport.py::test_argv_shape` · 5 live sockets on core |
| `c28` — compact history bounded to 24 h per box | high | `e19` · `tests/test_fleet_reader.py::test_week_of_19_machines_stays_under_100mb` |
| `c29` — reader health is reported separately from thermal state | high | `e20` · `tests/test_fleet_reader.py::test_snapshot_is_json_and_carries_reader_health` |

Lapse ledger evidence:

pending approval (not yet evidence): `l1`, `l2`, `l3`, `l4`, `l5`, `l6`, `l7`

## Remaining Work / Follow-up

- **`t9` / `e25`: the light compare palette fails the dataviz validator.** Replace `#eda100` in light `--c4` and re-check `#1baf7a` and `#e87ba4` on the cream surfaces. Re-run the validator in both modes and keep the run as evidence. Owner: next heat-governor PR.
- **`t9` / `e8`: no machine can show as "not supported".** Either add a way to declare one in `fleet.toml` (shown with no reading), or remove the dead branch and amend o8. This needs the operator's decision.
- **Adjudication (operator):** confirm or reject evidence `e1`–`e28`, deltas `b1`–`b8` and lapses `l1`–`l7`, using `devague evidence|delta|lapse --confirm/--reject`. The superseded originals (`e1`, `e2`, `e5`, `e7`, `e9`, `e12`, `e14`, `e18`, `b1`, `b2`, `b5`) can simply be rejected. Until then every claim above stays provisional.
- **Unexercised parts of passed obligations:**
  - `o14`: the slow or hung reader case. Measure daemon tick intervals while a reader holds stalled ssh sessions and runs 7d history reads.
  - `o2`: search, the sort order, and count-chip filtering.
  - `o5`: the 6-machine compare limit.

  Each needs a check, or a behavioural test, before its claim can rise.
- **spark runs close to its reset point under the current inference load.** On 2026-11-10 it went critical for about 2% of an hour, with an effective peak of 94.0 °C against a reset point near 94.5 °C. Its only lever is CPU caps, because every container is `serving`. The 0.16.0 restart was not the cause: the raw peak of 93.6 °C came under 0.14.1, before the restart, and caps were back within about 1 s. Next step: decide whether `glm53-spark` or another workload should be `batch`, or tighten the bands. Owner: operator.
- **A trend-edge display issue.** The 60 s trend cache makes the newest minute show as "no data for Ns" on the chart's right edge, even though the records have no gap there. Next step: label it as cached, or use the live reading for the edge.
- **The Light/Dark switch can't be reached while the trend panel is open,** because the backdrop covers the header. Minor.
- **Measure VENTUNO Q limits under CPU, GPU and NPU load** before cultureos leaves dry-run (open vagueness; owner: operator).
- **Park v4 (upgrade release):** a bearer-token tailnet listener on each daemon and a daemon `GET /history` route. Park v5 (persisting the reader buffer) is answered by `d2`; resolve it with the operator.
- **Fleet naming:** core's fleet entry is named `elitemini`, while the operator calls the box `core`. Rename it in core's `fleet.toml` if wanted.

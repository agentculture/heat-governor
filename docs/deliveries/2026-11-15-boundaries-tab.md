# Delivery Summary — boundaries-tab

plan: `boundaries-tab` · run: `partial` · date: `2026-11-15`
baseline: `devague summary skeleton`

## Intent

> The fleet dashboard has a Boundaries tab: an operator reads every machine's memory-guard boundaries and their live state, and creates, changes and deletes them from the page, without logging in to each box (issue #18)

After: The dashboard has a Boundaries tab listing, per machine, the guard's action, limits, kills and every boundary with its matchers, usage and state; with a write token the operator adds, changes or deletes a boundaries.toml rule on any machine from the page or from heat-governor boundaries, sees which running processes it matches before saving, and the daemon applies it on its normal live reload

## Planned Work

- `t1` — PR 1: the daemon's `mem_guard` document carries each boundary's matchers and source, plus `act_over_pct`, `max_kills_per_hour` and `min_victim_mib`
- `t2` — PR 1: `fleet_reader` passes `mem_guard`, config and `config_error` through to /api/fleet
- `t3` — PR 1: mesh status shows the memory guard per machine and the MCP `machine_state` test asserts `mem_guard`
- `t4` — PR 1: read-only Boundaries tab in the dashboard, built with the page's existing styles
- `t5` — PR 1: docs, harness files, version bump and changelog for the read side
- `t6` — PR 1: live check of the read side on the fleet, with the user's go-ahead
- `t7` — PR 2: the config loader merges a sibling boundaries.toml, watches both files and rejects match-all rules
- `t8` — PR 2: boundaries.toml emitter and locked, versioned, backed-up writer
- `t9` — PR 2: a boundary-only change keeps the guard's fill-rate window and kill budget
- `t10` — PR 2: the daemon records and ships `memory_boundary_changed`, including changes made while it was down
- `t11` — PR 2: on-box preview of a draft rule against running processes
- `t12` — PR 2: the boundaries CLI noun (overview, list, add, set, rm, apply, preview) on the local box
- `t13` — PR 2: transports can run the fixed remote boundaries commands with a body on stdin
- `t14` — PR 2: the reader's boundaries service: version gate, remote call, audit log, lost-response handling, and --machine for the CLI
- `t15` — PR 2: mesh serve accepts token-guarded boundary reads, previews and writes
- `t16` — PR 2: the boundary editor sub-view and modal confirmations in the dashboard
- `t17` — PR 2: contract, working rules, harness files, version bump and changelog for the write side
- `t18` — PR 2: staged live check of the write path, with the user's go-ahead

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | PR #19 (0.18.0): matchers, source and the three thresholds in `mem_guard` |
| `t2` | delivered | PR #19: `mem_guard`, `config` and `config_error` through `/api/fleet` |
| `t3` | delivered | PR #19: guard column in `mesh status`, `mem_guard` in `--json` and the MCP test |
| `t4` | delivered | PR #19: read-only Boundaries tab; wording of the `min_victim_mib` note differs from the plan (`d1`) |
| `t5` | delivered | PR #19: docs, four harness files, 0.18.0 |
| `t6` | delivered | PR #20 (0.18.1): `docs/evidence/2026-11-16-boundaries-read-live.md`; the one-poll timing was not measured |
| `t7` | delivered | PR #21 (0.19.0): loader merge, pair of file times, match-all check (narrowed after review: a `comm` list is never match-all, globs are probed) |
| `t8` | delivered | PR #21: `heat_governor/boundaries_file.py` |
| `t9` | delivered | PR #21: `MemGuard.set_rule`; the 'fires no later' test does not discriminate |
| `t10` | delivered | PR #21: `memory_boundary_changed`, state in `boundaries-applied.json` |
| `t11` | delivered | PR #21: `heat_governor/mempreview.py` |
| `t12` | delivered | PR #21: `heat_governor/cli/_commands/boundaries.py`; the kill check counts only newly far-over processes (`d5`) |
| `t13` | delivered | PR #21: `boundaries()` on both transports, `bin` key in `fleet.toml` |
| `t14` | delivered | PR #21: `heat_governor/boundaries_service.py`; two audit lines per apply (`d4`) |
| `t15` | delivered | PR #21: token routes, `--token-file`, plus the additions in `d5` |
| `t16` | partial | PR #21: the editor and dialogs are in `heat_governor/web/index.html` and pass their source and script-harness tests; never seen with a token in a real browser, at phone width or in the dark theme; not written test-first |
| `t17` | delivered | PR #21: `docs/contracts/boundaries.md`, harness files, 0.19.0 |
| `t18` | partial | PR #22 (0.19.1, open): `docs/evidence/2026-11-17-boundaries-write-live.md`; the rule was saved from the CLI and through the HTTP routes with `curl` on the EliteMini and orin, not from the page as the task's first criterion says |

## Mid-work Decisions

- `d1` — t4's fourth criterion says a rule whose `max_mib` is under `min_victim_mib` renders as blamed-never-killed. The page instead says 'under 2.0 GiB (`min_victim_mib`) a match is blamed, never killed', because the guard compares each process's anonymous memory with `min_victim_mib` (memkill.py:255), not the rule's `max_mib`; a process over such a rule is killable once it passes `min_victim_mib`. — the plan text came from docs/contracts/memory.md:117-118, which states the rule loosely; the code is the authority
- `d2` — t1 edited tests/`test_memboundary.py`, tests/`test_memguard.py` and tests/`test_cli_memory_ship.py`, which its instruction did not list as owned: adding keys to the boundary entry broke exact-equality assertions in the first two, and it added the memory status test to the third. — the instruction's ownership list missed the existing exact-equality tests
- `d3` — t7 edited six files outside its ownership: cli/`_commands`/tuning.py and governing.py (cfg.mtime truthiness check now reads cfg.mtime\[0\]) and four test files (`test_units.py`, `test_gpuclock_unsupported_paths.py`, `test_daemon_memory.py`, `test_pair.py`) that did arithmetic on cfg.mtime — the plan makes Config.mtime a pair, which breaks every existing reader of the scalar; the edits are one line each and mechanical
- `d4` — an apply that reaches the box writes two audit lines (an attempt line before the call, the outcome after it), not one; a version-gate refusal writes one — the audit line must exist before a write is sent, so a failed audit can refuse the write; the outcome is only known afterwards
- `d5` — additions beyond the plan in the server and CLI: an unauthenticated GET /api/boundaries probe that returns only `writes_enabled`; a --server-name flag for accepted Host names; token routes refuse a wildcard listen address; a per-client-address failed-auth limit (429); the on-box kill check counts only processes the change newly puts far over, so an already far-over process does not block an unrelated edit — the page needs to know whether to offer a token field without spending a failed login; the Host rule needs the name the page is opened by; the kill check as first built blocked every edit while any process was far over

- No deviation record covers these; captured directly:
  - Sonnet built t7 to t14, Opus built t15 and t16, the main agent did t17 and t18 (`docs/plans/2026-11-15-boundaries-tab-split.md`).
  - The main agent rewrote the kill check and the failed-login limit after reading the agents' work, and fixed findings of the code review of PR #21 (commit `8480abf` on the PR branch, squashed into `1129a2e`): ssh failure and timeout handling after a lost response, the match-all rule, duplicate names after a rejected reload, a missed reload, a CLI crash on a recovered save, `list` hiding a rejected file, a kill on swapped-out rules, stale once-only marks, a double process scan, dead code.
  - The `ask-colleague` review could not run on either PR (its gateway rejected the API key).
  - Left as the plan had it, for the owner to decide: the daemon refuses to start on an invalid `boundaries.toml`, as it does for an invalid `config.toml`; the match-all check also applies to `config.toml`.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|------------------------|-----------------|
| `t4` (`d1`) | the plan text came from docs/contracts/memory.md:117-118, which states the rule loosely; the code is the authority | `acceptable` |
| `t1` (`d2`) | the instruction's ownership list missed the existing exact-equality tests | `acceptable` |
| `t7` (`d3`) | the plan makes Config.mtime a pair, which breaks every existing reader of the scalar; the edits are one line each and mechanical | `acceptable` |
| `t14` (`d4`) | the audit line must exist before a write is sent, so a failed audit can refuse the write; the outcome is only known afterwards | `acceptable` |
| `t15` (`d5`) | the page needs to know whether to offer a token field without spending a failed login; the Host rule needs the name the page is opened by; the kill check as first built blocked every edit while any process was far over | `acceptable` |
| `t7` | the match-all rule was narrowed after review: a `comm` list is never match-all because names are compared exactly, and globs are probed so a cgroup of `/*` is caught (delta `b1`, proposed) | `acceptable` |
| `t14` | after a lost response a timeout with the file unchanged is `unknown`, not `not_saved`, and ssh's own failure is recovered the same way (delta `b4`, proposed) | `acceptable` |
| `t16` | not written test-first; the editor was never used with a token in a real browser | `needs-follow-up` |
| `t18` | the live saves went through the CLI and the HTTP routes with `curl`, not through the page; only one of five remote boxes is upgraded (delta `b5`, proposed) | `needs-follow-up` |
| `t9` | the acceptance test for 'fires no later with a save midway' passes with the rule swap disabled | `risky` |

## Evidence

- tests: `uv run pytest -n auto` at `4454451` — 1716 passed, 1 skipped (the cross-repo harness-registry gap)
- tests per obligation: the node ids in evidence records `e3` to `e28` (`devague evidence --list --plan boundaries-tab`) — all pass at `4454451`; `e17` is 325 tests across four files
- observation: `e29` — the go-ahead rule is in all four harness files (search, no test)
- live: `docs/evidence/2026-11-16-boundaries-read-live.md` (`e1`, approved) and `docs/evidence/2026-11-17-boundaries-write-live.md` (`e2`, proposed)
- lint: black, isort, flake8, bandit, markdownlint, `teken cli doctor . --strict`, `scripts/scan-secrets.py`, `scripts/harness-smoke.py --stage config` — pass; `scripts/sonar-gate.py --pull-request 21` — no unaccepted issues
- commits: `100cc17` (PR #19), `a703c48` (PR #20), `1129a2e` (PR #21), branch `docs/boundaries-write-live` (PR #22)
- PRs / issues: #18 (closed by #21), #19, #20, #21, #22

## Delivery Claims

Rows are as `devague summary` renders them. "pending adjudication" means the evidence record
is filed but the owner has not confirmed it yet (`e2` to `e29`); only `c27` rests on approved
evidence. Three rows are weaker than a green test suggests:

- `c28`: the saves were not made from the page (see `t18`).
- `c33`: the 'fires no later' part is asserted weakly (see `t9`).
- `c39`: restoring a backup is documented but no test restores one.

| Claim | Confidence | Evidence |
|-------|------------|----------|
| `c2` — `fleet_reader` passes each daemon's `mem_guard` through to /api/fleet, mesh status and the MCP `machine_state`, tolerating its absence on daemons older than 0.17 (today extract() keeps only `THERMAL_FIELDS`, `heat_governor`/`fleet_reader.py`:79-98,225-243; mesh.py `_row` whitelists columns) | pending adjudication | `tests/test_fleet_reader.py::test_extract_passes_mem_guard_through_as_the_same_object; tests/test_cli_mesh.py::test_status_json_carries_mem_guard_summary; tests/test_mcp.py::test_machine_state_returns_mem_guard_unchanged; tests/test_fleet_reader.py::test_snapshot_carries_mem_guard_config_and_config_error` (run 2026-11-16) |
| `c3` — The daemon's `mem_guard` document gains what the page shows and /state lacks today: each boundary's matchers (comm, cmdline, cgroup) and source file, plus `act_over_pct`, `max_kills_per_hour` and `min_victim_mib`. Today an entry is exactly name, `max_mib`, scope, `usage_mib`, state (memboundary.py:165-172; docs/contracts/state.md:52), and adding keys is allowed ('clients ignore unknown fields') | pending adjudication | `tests/test_memguard_boundary.py::test_every_boundary_entry_carries_matchers_and_source_idle_and_scanned; tests/test_memguard_boundary.py::test_doc_carries_act_over_pct_kill_budget_and_min_victim_from_config` (run 2026-11-16) |
| `c4` — The config loader merges \[\[memory.allow\]\] rules from a sibling boundaries.toml into \[memory\] before validation, rejects a name that appears in both files, watches both files' mtimes, and keeps the last good boundaries when only that file is invalid (today load() reads one path, Config.path/mtime are scalars, changed() compares one mtime: config.py:885-909, daemon.py:502-540) | pending adjudication | `tests/test_config_memory_allow.py::test_rules_merge_from_both_files_tagged_with_their_source; tests/test_config_memory_allow.py::test_a_name_in_both_files_names_both_files; tests/test_config_memory_allow.py::test_invalid_boundaries_keep_the_last_good_while_a_config_edit_applies; tests/test_config_memory_allow.py::test_touching_only_boundaries_reloads_and_deleting_it_drops_the_rules` (run 2026-11-16) |
| `c5` — A stdlib emitter writes the boundaries.toml \[\[memory.allow\]\] shape, escaping control characters and newlines in matcher strings, and the write is atomic (temp file + os.replace) with a backup. No emitter exists: `_toml_value` (config.py:924-935) handles scalars/lists only and escapes only backslash and double quote; config init --write is a plain `write_text` (tuning.py:120) | pending adjudication | `tests/test_boundaries_file.py::test_emit_roundtrips_awkward_matchers; tests/test_boundaries_file.py::test_failure_midway_leaves_file_identical` (run 2026-11-16) |
| `c6` — A new boundaries CLI noun (overview, list, add, set, rm, a stdin apply used by the remote write, and a preview) follows the agent-first contract: register() in cli/`__init__.py`, overview verb and bare-noun overview, explain catalog entry, learn text, overview.`_VERBS` line, --json and exit codes | pending adjudication | `tests/test_cli_boundaries.py::test_bare_noun_prints_overview; tests/test_cli_boundaries.py::test_explain_resolves_every_boundaries_path; tests/test_cli_boundaries.py::test_apply_bad_request_text_goes_to_stderr; tests/test_cli_boundaries.py::test_exit_code_helper` (run 2026-11-16) |
| `c7` — The SSH and local transports gain one fixed, argv-only write path that can send a body on stdin and return the remote exit code and stderr (the validator's message), with its own timeout and a defined way to find heat-governor on the remote box | pending adjudication | `tests/test_transport.py::test_boundaries_remote_argv_is_fixed_and_body_free; tests/test_transport.py::test_boundaries_body_arrives_on_stdin; tests/test_transport.py::test_boundaries_nonzero_exit_returns_code_stdout_stderr` (run 2026-11-16) |
| `c8` — mesh serve accepts boundary writes: method dispatch for POST/PUT/DELETE, a capped JSON body reader, a bearer token compared in constant time (401 on a bad token, 403 when none is configured), Origin and Content-Type checks, If-Match with 409 on a stale version, and 4xx error mapping. `test_post_not_allowed`, the read-only docstrings and the mesh serve help text change with it | pending adjudication | `tests/test_mesh_server.py::test_no_token_file_refuses_every_list_preview_and_write_with_403; tests/test_mesh_server.py::test_missing_or_wrong_token_is_401_and_reaches_nothing; tests/test_mesh_server.py::test_stale_if_match_is_409_with_the_current_rules; tests/test_mesh_server.py::test_foreign_origin_is_refused; tests/test_mesh_server.py::test_wrong_content_type_is_415; tests/test_mesh_server.py::test_existing_routes_are_byte_for_byte_the_same_with_writes_enabled` (run 2026-11-16) |
| `c9` — The daemon, not the CLI, emits `memory_boundary_changed` when it reloads a changed boundaries file, and the kind joins recorder.`MEMORY_EVENTS` so it is recorded and shipped; the reader keeps its own audit log (token label, time, machine, old and new rule) in its state dir | pending adjudication | `tests/test_recorder_memory.py::test_reload_changing_merged_rules_emits_exactly_one_record_and_ships_it; tests/test_mesh_server.py::test_apply_saves_and_writes_audit_with_label; tests/test_boundaries_service.py::test_apply_writes_attempt_then_final_line_with_mode_0600` (run 2026-11-16) |
| `c10` — A preview runs on the target box: memprocs.scan + memboundary.assess for within/over/`far_over` per matching process, with the killer's names function, uid, protect list, `min_victim_mib` and action deciding whether a far-over process would really be killed; command lines pass through redact() and the container column comes from `container_id`(cgroup) | pending adjudication | `tests/test_mempreview.py::test_states_and_verdicts_equal_assess_and_refusal; tests/test_mempreview.py::test_cmdline_is_redacted` (run 2026-11-16) |
| `c11` — The Boundaries tab is built inside `heat_governor`/web/index.html as a second view of the one React/htm page: a hash-routed Sections nav (Machines \| Boundaries) in the header, reusing the existing classes (.pills button\[aria-current\], .chip, .status, .tag, .facts, .dt, .search, .eyebrow, .lede, Mark/rune, useFleet) and the html\[data-mode\] theme, not the design's .app.dark class or its anchor-styled pills | pending adjudication | `tests/test_web_page.py::test_sections_nav_is_driven_by_the_hash; tests/test_web_page.py::test_read_tab_defaults_to_machines; tests/test_web_page.py::test_references_vendored_scripts_and_fonts` (run 2026-11-16) |
| `c12` — New styles are limited to what the dashboard does not have yet: buttons (.btn, primary, danger), icon buttons, form fields (input, select, label, hint, error state), a usage bar with boundary and far-over marks, a left-aligned rules table, and a modal confirmation dialog; the design's inline stroke SVG icons (plus, pencil, bin, lock, warning, refresh) are a new vocabulary for the page | pending adjudication | `tests/test_web_page.py::test_every_editor_button_has_an_accessible_name; tests/test_web_page.py::test_editor_actions_are_real_controls_without_div_click_handlers; tests/test_web_page.py::test_modal_frame_is_a_labelled_modal_dialog; tests/test_web_page.py::test_modals_move_focus_trap_tab_and_close_on_escape` (run 2026-11-16) |
| `c13` — The page stays self-contained: Lato from /vendor, no Google Fonts link (the design's helmet loads fonts.googleapis.com), no new vendored library, no third-party runtime dependency, CSP unchanged | pending adjudication | `tests/test_web_page.py::test_csp_and_dependencies_unchanged; tests/test_mesh_server.py::test_csp_is_unchanged` (run 2026-11-16) |
| `c15` — The page covers states the design does not draw: a machine that is unreachable or stale, a daemon with no `mem_guard` (older than 0.17 or guard off), action = record (a far-over process is not killed), a save that fails or times out over SSH, a validator rejection for any field (the design shows it for `max_mib` only), and a rule whose `max_mib` is under `min_victim_mib` (blamed, never killed) | pending adjudication | `tests/test_web_page.py::test_fixture_states_render_distinctly; tests/test_web_page.py::test_save_outcomes_from_each_answer; tests/test_web_page.py::test_rejected_file_shows_its_message_and_the_last_good_rules_as_live` (run 2026-11-16) |
| `c16` — The page edits boundaries.toml rules only: it never changes \[memory\] action, `act_over_pct`, limits, \[ship\] or thermal settings, and rules written in config.toml show read-only (issue #18 Safety and Out of scope) | pending adjudication | `tests/test_cli_boundaries.py::test_apply_rejects_unknown_rule_keys_with_validator_message; tests/test_mesh_server.py::test_preview_with_extra_keys_is_400; tests/test_config_memory_allow.py::test_boundaries_accepts_only_memory_allow_tables; tests/test_boundaries_service.py::test_malformed_requests_are_invalid_and_send_nothing` (run 2026-11-16) |
| `c17` — GET /api/fleet, /api/history and the Machines view keep their behaviour, and remote daemons keep listening on loopback only, reached over the reader's SSH link (issue #18 acceptance criteria and Out of scope) | pending adjudication | `tests/test_mesh_server.py; tests/test_fleet_reader.py; tests/test_transport.py; tests/test_client_state.py` (run 2026-11-16) |
| `c27` — After PR 1, /api/fleet returns `mem_guard` for all 6 machines and the Boundaries tab shows each machine's guard state within 1 poll of the reader | `execution` | `docs/evidence/2026-11-16-boundaries-read-live.md (live read of /api/fleet and the Boundaries tab on the EliteMini reader)` (run 2026-11-15) |
| `c28` — After PR 2, a rule saved from the page on a local and on an SSH machine appears in that machine's `mem_guard`.boundaries in under 15 s, a write with no or a wrong token gets 403 or 401, and a stale version gets 409 with nothing written | pending adjudication | `docs/evidence/2026-11-17-boundaries-write-live.md (live saves on the EliteMini and on orin over SSH, from the CLI and through the token routes with curl)` (run 2026-11-16) |
| `c29` — The reader runs the remote write and preview by an explicit binary path, default ~/.local/bin/heat-governor with a per-machine override key in fleet.toml, and the on-box command writes boundaries.toml beside the config file the running daemon reports in /state (config, daemon.py:1061), not beside whatever the SSH shell's environment resolves (challenge pass, 2026-11-15) | pending adjudication | `tests/test_transport.py::test_boundaries_default_and_custom_bin; tests/test_boundaries_service.py::test_default_transport_passes_the_machine_bin; tests/test_boundaries_service.py::test_request_is_rebuilt_from_validated_fields` (run 2026-11-16) |
| `c30` — The reader refuses a write to a machine whose daemon version predates boundaries.toml support, with a message naming the version needed, and the page shows that machine as read-only until it is upgraded; a missing remote boundaries verb is reported as that same refusal, not as 'unreachable' (challenge pass, 2026-11-15) | pending adjudication | `tests/test_boundaries_service.py::test_old_or_unparseable_version_is_refused_before_any_transport_call` (run 2026-11-16) |
| `c31` — The version check and the write are one atomic step on the box: the on-box command takes an exclusive lock, compares the caller's base version with the hash of the file as it is now, and only then writes; the reader never decides a conflict on its own copy (challenge pass, 2026-11-15) | pending adjudication | `tests/test_boundaries_file.py::test_concurrent_writes_one_base_one_success; tests/test_boundaries_file.py::test_update_with_wrong_base_writes_nothing` (run 2026-11-16) |
| `c32` — The editor's rule list and base version come from the boundaries.toml file read on the box, not from /state, and the reader passes through the daemon's config path and `config_error` so the page can say when the file on disk was rejected and the daemon is still running the last good rules (challenge pass, 2026-11-15) | pending adjudication | `tests/test_fleet_reader.py::test_snapshot_carries_mem_guard_config_and_config_error; tests/test_web_page.py::test_edit_state_comes_from_the_file_read_on_the_box` (run 2026-11-16) |
| `c33` — A change that only touches boundary rules does not lose the guard's detection state: the fill-rate window and the kill budget survive it, either by swapping the rules in the running guard or by carrying the trigger window across the rebuild (challenge pass, 2026-11-15) | pending adjudication | `tests/test_daemon_memory.py::test_boundary_only_save_keeps_the_trigger_window; tests/test_daemon_memory.py::test_boundary_only_save_keeps_the_kill_budget; tests/test_daemon_memory.py::test_runaway_fires_no_later_with_a_boundary_save_midway` (run 2026-11-16) |
| `c34` — The kill confirmation is enforced on the box, not only drawn in the page: the on-box apply re-runs the preview at write time and refuses a change that would put a killable own-user process far over its boundary unless the request explicitly allows it, returning the processes concerned (challenge pass, 2026-11-15) | pending adjudication | `tests/test_cli_boundaries.py::test_apply_would_kill_writes_nothing_and_returns_process; tests/test_cli_boundaries.py::test_apply_allow_kill_writes` (run 2026-11-16) |
| `c35` — The validator rejects a boundary whose matchers are all bare wildcards, and the preview reports how many processes a rule matches, because a rule that matches everything with a large maximum makes every process 'within' and so switches the guard off for that box (challenge pass, 2026-11-15) | pending adjudication | `tests/test_config_memory_allow.py::test_a_match_all_rule_is_rejected_in_either_file; tests/test_cli_boundaries.py::test_preview_returns_matches_and_writes_nothing` (run 2026-11-16) |
| `c36` — Every endpoint that returns process command lines or accepts a change requires the token, including the preview; the token travels only in the Authorization header, never in a URL; mesh serve refuses to start with a token file readable by group or others; write requests are refused when the Host header is not the address the server listens on (challenge pass, 2026-11-15) | pending adjudication | `tests/test_mesh_server.py::test_missing_or_wrong_token_is_401_and_reaches_nothing; tests/test_mesh_server.py::test_token_in_query_string_is_ignored; tests/test_mesh_server.py::test_mesh_serve_exits_2_for_a_group_or_other_readable_token_file; tests/test_mesh_server.py::test_foreign_host_is_refused` (run 2026-11-16) |
| `c38` — A save whose response is lost is not left ambiguous: writes are whole-rule and idempotent, and after a timeout the page re-reads the file version and reports saved, not saved or conflict from what is on the box (challenge pass, 2026-11-15) | pending adjudication | `tests/test_web_page.py::test_a_lost_or_timed_out_save_is_decided_from_a_re_read; tests/test_boundaries_service.py::test_lost_response_after_apply_is_recovered_as_saved; tests/test_boundaries_service.py::test_repeating_a_recovered_request_changes_nothing` (run 2026-11-16) |
| `c39` — Recovery is specified: each write keeps a timestamped backup of the previous boundaries.toml, at most 5 are kept, and the contract documents how to restore one; deleting boundaries.toml is a valid state that means no page-managed rules (challenge pass, 2026-11-15) | pending adjudication | `tests/test_boundaries_file.py::test_update_with_right_base_backs_up_previous; tests/test_boundaries_file.py::test_backups_capped_at_five_newest_kept` (run 2026-11-16) |
| `c40` — The daemon remembers the hash of the boundaries it last applied in its state dir and emits `memory_boundary_changed` at startup when the file differs, so a change made while the daemon was down or restarting is still recorded and shipped (challenge pass, 2026-11-15) | pending adjudication | `tests/test_recorder_memory.py::test_restart_after_an_edit_made_while_down_emits_one_with_the_old_rules; tests/test_recorder_memory.py::test_restart_with_the_same_rules_emits_none` (run 2026-11-16) |
| `c41` — Writing a boundary changes a live machine: the first live write on any box, enabling a token file on the EliteMini mesh unit, and any test or probe against a real box need the user's go-ahead; tests use `tmp_path` config dirs, a fake transport and fake /proc trees and never write under a real ~/.config or signal a process they did not spawn (challenge pass, 2026-11-15) | pending adjudication | `search of CLAUDE.md, AGENTS.override.md, AGENTS.colleague.md and QWEN.md for the go-ahead rule and the token file; search of tests/ for paths under the home config directory` |

Lapse ledger evidence:

| Lapse | Code | What |
|-------|------|------|
| `l1` | `provenance-missing` | After export I backticked an HTML token in docs/specs/2026-11-15-boundaries-tab.md by hand instead of amending claim c11, so the frame state and the exported spec differed |
| `l2` | `assumption-for-measurement` | Spec claim c15, honesty h20 and plan task t4's fourth criterion say a rule whose `max_mib` is under `min_victim_mib` is blamed but never killed. I took that from docs/contracts/memory.md:117-118 and a subagent's summary without reading the code. MemKiller.refusal (memkill.py:255) compares each process's anonymous memory with `min_victim_mib`, so a process over such a rule is killable once it exceeds `min_victim_mib`. The t4 agent found this and reported it. |

## Remaining Work / Follow-up

- `t16`, `t18` — use the editor with the real token in a browser on the tailnet dashboard: create, change and delete a no-match rule, check phone width and the dark theme — owner
- `e2` to `e29`, `b1` to `b5` — confirm or reject (`devague evidence --confirm`, `devague delta --confirm`) — owner
- cultureos, spark, spark2, thor — upgrade to 0.19.0 before boundaries can be read with matchers or written there; needs the owner's go-ahead per box
- preview cost on a box short of memory (plan risk on t11) — measure on thor or a Spark once upgraded
- a rule that matches a real process, the would-kill refusal and the shipped copy of `memory_boundary_changed` — not yet seen live
- startup with an invalid `boundaries.toml` — decide whether the daemon should start without page-managed rules instead of refusing
- duplicated request and rule validation in `boundaries_service.py` and `cli/_commands/boundaries.py` — consolidate
- the CLI's `--json` error for a refused machine carries the outcome in `remediation` — fix
- `test_runaway_fires_no_later_with_a_boundary_save_midway` — make it fail when the swap is disabled
- PR #13 (VENTUNO Q limits) is still open against an older version

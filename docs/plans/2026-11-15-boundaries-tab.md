# Build Plan — boundaries-tab

slug: `boundaries-tab` · status: `exported` · from frame: `boundaries-tab`

> The fleet dashboard has a Boundaries tab: an operator reads every machine's memory-guard boundaries and their live state, and creates, changes and deletes them from the page, without logging in to each box (issue #18)

## Tasks

### t1 — PR 1: the daemon's `mem_guard` document carries each boundary's matchers and source, plus `act_over_pct`, `max_kills_per_hour` and `min_victim_mib`

- instruction: Owns `heat_governor`/memboundary.py (`_entry`, `idle_doc`, `_rule_doc`), `heat_governor`/memguard.py (`_build_doc`), docs/contracts/state.md and tests/`test_memguard_boundary.py`. source is the file name the rule came from; until boundaries.toml exists every rule reports config.toml, so add a source field to AllowRule with that default rather than hard-coding the string in the doc. Add keys only; never rename or drop a 0.17 key.
- covers: c3, h9
- acceptance:
  - tests/`test_memguard_boundary.py`: every entry of `mem_guard`.boundaries has comm, cmdline, cgroup and source beside name, `max_mib`, scope, `usage_mib`, state, for both the idle placeholder and a scanned rule
  - `mem_guard` has `act_over_pct`, `max_kills_per_hour` and `min_victim_mib` equal to the loaded \[memory\] values
  - docs/contracts/state.md lists every added key, and heat-governor memory status still prints the same lines from a 0.17-shaped document

### t2 — PR 1: `fleet_reader` passes `mem_guard`, config and `config_error` through to /api/fleet

- instruction: Owns `heat_governor`/`fleet_reader.py` and tests/`test_fleet_reader.py` only. Add the three keys to the pass-through in extract(); coerce `mem_guard` to a dict or None the way limits is coerced. Do not touch Ring.append.
- covers: c2, h8, h6, c32
- acceptance:
  - tests/`test_fleet_reader.py`: extract() of a /state document with `mem_guard` returns the same object under thermal.`mem_guard`; a document without it returns state ok with `mem_guard` None and raises nothing
  - config and `config_error` are passed through unchanged
  - the history Ring, /api/history and every existing assertion in tests/`test_fleet_reader.py` and tests/`test_mesh_server.py` are unchanged

### t3 — PR 1: mesh status shows the memory guard per machine and the MCP `machine_state` test asserts `mem_guard`

- instruction: Owns `heat_governor`/cli/`_commands`/mesh.py (`build_status`, `_row`), tests/`test_cli_mesh.py` and tests/`test_mcp.py`. `mcp_server`.`machine_state` already returns the whole entry, so it needs a test, not code.
- depends on: t2
- covers: c2, c23
- acceptance:
  - heat-governor mesh status --json carries each machine's `mem_guard` action, armed, `kills_last_hour` and boundary count, and the text table has one guard column that reads '-' when `mem_guard` is absent
  - tests/`test_mcp.py`: `machine_state` for a machine whose /state has `mem_guard` returns it unchanged

### t4 — PR 1: read-only Boundaries tab in the dashboard, built with the page's existing styles

- instruction: Owns `heat_governor`/web/index.html and tests/`test_web_page.py`. Source design: artifact FZzmdU4yBpTbboV4WFUX7x, board Main.dc.html (a copy is in the spec's scope notes). Reuse .pills button\[aria-current\], .chip, .status, .tag, .facts, .search, .eyebrow, .lede, Mark/rune() and useFleet(); take the nav and hash routing from docs/design/fleet-dashboard.html:1015-1029. New CSS only for the usage bar and a left-aligned rules table. No edit or delete controls in this PR, no Add button, no token text. Add the rune for e to rune(). Keep html\[data-mode\] theming and vendored Lato.
- depends on: t1, t2
- covers: c11, h17, c13, h19, c15, h20
- acceptance:
  - `heat_governor`/web/index.html has a Sections nav (Machines, Boundaries) driven by location.hash; an empty or unknown hash shows Machines; every existing test in tests/`test_web_page.py` passes unchanged
  - the Boundaries view lists per machine: guard action and armed state, free memory, limits and their source, kills in the last hour, last kill, and each rule with matchers, max, scope, usage bar, state and a source tag
  - usage-bar marks are computed from that machine's `act_over_pct`, usage is labelled as anonymous memory, and the guard badge comes from `mem_guard`.action and armed
  - fixture documents render distinctly for: unreachable, stale, no `mem_guard`, action = record, no rules, and a rule whose `max_mib` is under `min_victim_mib`
  - the page loads nothing from another origin, uses the shipped colour tokens unchanged, and pyproject.toml dependencies and the CSP string in `mesh_server.py` are unchanged

### t5 — PR 1: docs, harness files, version bump and changelog for the read side

- instruction: Use the version-bump skill (minor). Mark the write side as (planned) in CLAUDE.md. Open the PR through the cicd skill; the body references issue #18 and says it is part 1 of 2.
- depends on: t1, t2, t3, t4
- covers: c24, h3, c26, h23
- acceptance:
  - CLAUDE.md's state table has a row for the read-only Boundaries tab, and AGENTS.override.md, AGENTS.colleague.md and QWEN.md carry the same fact
  - the changelog entry states that before this release /api/fleet carried no `mem_guard` and no box had a \[\[memory.allow\]\] rule (docs/deliveries/2026-11-14-memory-guard.md:117)
  - version bumped by one minor in pyproject.toml and CHANGELOG.md; uv run pytest -n auto, black, isort, flake8, bandit, markdownlint, teken cli doctor --strict, scan-secrets and harness-smoke --stage config all pass

### t6 — PR 1: live check of the read side on the fleet, with the user's go-ahead

- instruction: Upgrading daemons and restarting the mesh unit changes live machines: ask the user before each step. Read-only otherwise. File devague evidence for obligation o16 with the outcome, pass or fail.
- depends on: t5
- covers: c27
- acceptance:
  - docs/evidence has a dated note showing /api/fleet on the EliteMini reader with `mem_guard` for all 6 machines and the Boundaries tab showing each machine's guard state within one 3 s poll
  - the note records which boxes were upgraded, to which version, and any machine that did not report `mem_guard`

### t7 — PR 2: the config loader merges a sibling boundaries.toml, watches both files and rejects match-all rules

- instruction: Owns `heat_governor`/config.py (load, Config, changed, `_validate_allow`\*) and the reload bookkeeping in `heat_governor`/daemon.py `maybe_reload` (lines 502-540) only. boundaries.toml sits beside the config path actually loaded (Config.path.parent), never a path re-derived from the environment. Config.mtime becomes a pair. Config.memory builds MemoryRule positionally (config.py:355-370): keep field order.
- depends on: t5
- covers: c4, h10, c35, h30
- acceptance:
  - tests/`test_config_memory_allow.py`: rules from config.toml and boundaries.toml are merged before validation, each tagged with its source file; a name in both files raises ConfigError naming both files
  - an invalid boundaries.toml keeps the last good boundaries and publishes the message in `config_error`, while a valid config.toml edit made at the same time still applies
  - touching only boundaries.toml triggers a reload within `reload_every_s`; deleting it means no page-managed rules; boundaries.toml accepts only \[\[memory.allow\]\] tables and rejects any other key
  - a rule whose matchers are all bare wildcards raises ConfigError, in either file

### t8 — PR 2: boundaries.toml emitter and locked, versioned, backed-up writer

- instruction: New module `heat_governor`/`boundaries_file.py` and tests/`test_boundaries_file.py`; touch no other file. Stdlib only (fcntl.flock on a sibling lock file, hashlib, os.replace). The version of a missing file is a fixed sentinel so 'create' also conflicts correctly. Validate rules with config's existing allow validators before writing. All tests under `tmp_path`.
- depends on: t5
- covers: c5, h11, c31, h26, c39, h33
- acceptance:
  - emit then tomllib.loads returns the same rules for matchers with quotes, backslashes, newlines, control characters and non-ASCII
  - write(`base_version`, rules) under an exclusive lock compares `base_version` with the sha256 of the current file; a mismatch writes nothing and returns the current rules; two concurrent writes with one base yield exactly one success
  - a successful write goes through a temp file and os.replace, leaves a timestamped backup of the previous file, and never keeps more than 5 backups; a failure midway leaves the previous file byte-identical

### t9 — PR 2: a boundary-only change keeps the guard's fill-rate window and kill budget

- instruction: Owns `heat_governor`/daemon.py `set_memory` (lines 767-781) and `heat_governor`/memguard.py. Prefer swapping the rule set inside the running guard under its lock; carrying the Trigger window across a rebuild is the fallback. No real /proc, no real signals.
- depends on: t7
- covers: c33, h28
- acceptance:
  - tests: after Daemon.`set_memory` with a MemoryRule that differs only in allow, the trigger's sample window and the killer's budget state are the same as before
  - a simulated runaway on a fake /proc fires no later with a boundary save in the middle than without one
  - a change to any other \[memory\] field still rebuilds the guard as today

### t10 — PR 2: the daemon records and ships `memory_boundary_changed`, including changes made while it was down

- instruction: Owns `heat_governor`/recorder.py (`MEMORY_EVENTS`), the event emission in `heat_governor`/daemon.py `apply_config`/`_route_memory_event`, docs/contracts/memory.md and tests/`test_recorder_memory.py`. The CLI never records events; the daemon is the only emitter, so a hand edit is covered by the same path.
- depends on: t9
- covers: c9, c40, h34, h15
- acceptance:
  - a reload that changes the merged boundary rules emits exactly one `memory_boundary_changed` record with old and new rules, written by the recorder and submitted to the shipper (fake S3)
  - the daemon stores the hash of the boundaries it last applied in its state dir; starting with a different file emits one event, starting with the same file emits none
  - docs/contracts/memory.md lists the event in the event table and the ship table

### t11 — PR 2: on-box preview of a draft rule against running processes

- instruction: New module `heat_governor`/mempreview.py and tests/`test_mempreview.py`. Pure function over memprocs.scan(`proc_root`) output; expose the killer's names function from memkill.py if needed but change no behaviour there. Never signals anything.
- depends on: t5
- covers: c10, h16, c35
- acceptance:
  - on a fake /proc tree, each matching process's state equals memboundary.assess and its killable verdict and reason equal MemKiller.refusal for the same table, rule, uid, protect list, `min_victim_mib` and action
  - the result carries the number of matching processes, each with pid, uid, user name when resolvable, redacted command line, container id from `container_id`(cgroup), anonymous memory and state
  - the preview uses the killer's names function, so comm matching agrees with the live guard

### t12 — PR 2: the boundaries CLI noun (overview, list, add, set, rm, apply, preview) on the local box

- instruction: New `heat_governor`/cli/`_commands`/boundaries.py and tests/`test_cli_boundaries.py`; wire register() in cli/`__init__.py`, ENTRIES in explain/catalog.py, learn.py text and JSON list, overview.py `_VERBS`. add/set/rm are thin wrappers that build the same request apply takes. Follow memory.py for the noun pattern and add boundaries to the parametrised noun test in tests/`test_cli_memory_ship.py`.
- depends on: t7, t8, t11
- covers: c6, h12, c16, h21, c34, h29, c29
- acceptance:
  - every verb takes --json, writes results to stdout and errors to stderr, exits 0/1/2 per cli/`_errors.py`; the bare noun prints the overview; explain resolves every boundaries path; uv run teken cli doctor . --strict passes
  - apply reads one JSON request on stdin (base version, operation, rule), accepts only name, comm, cmdline, cgroup, `max_mib`, scope, and rejects anything else with the validator's message and no write
  - apply re-runs the preview at write time: a change that would put a killable own-user process far over its boundary writes nothing and returns that process unless the request sets `allow_kill`
  - list, apply and preview act on the boundaries.toml beside the path given by --config, and list returns the rules with their file version

### t13 — PR 2: transports can run the fixed remote boundaries commands with a body on stdin

- instruction: Owns `heat_governor`/transport.py, `heat_governor`/fleet.py (`MACHINE_KEYS` gains bin), docs/fleet.example.toml and tests/`test_transport.py`. One new method, e.g. boundaries(verb, body), allowed verbs list|apply|preview only; build the argv from constants and validated values, never from the body. LocalTransport runs the same command with sys.executable -m or the local binary. Add a stdin-echo mode to the fake ssh.
- depends on: t5
- covers: c7, h13, c29, h24
- acceptance:
  - tests/`test_transport.py` with the fake ssh: the remote argv is identical for any rule content, the body arrives on stdin, and the caller gets exit code, stdout and stderr instead of Unreachable for a non-zero exit
  - the command starts with the machine's binary path, default ~/.local/bin/heat-governor, overridable by a bin key in fleet.toml, and passes --config with the path the daemon reported in /state
  - the write has its own timeout, separate from the 5 s fetch timeout; fetch() and history() behave exactly as before

### t14 — PR 2: the reader's boundaries service: version gate, remote call, audit log, lost-response handling, and --machine for the CLI

- instruction: New module `heat_governor`/`boundaries_service.py` and tests/`test_boundaries_service.py`; adds the --machine option to cli/`_commands`/boundaries.py. Reuse FakeTransport from tests/`test_fleet_reader.py`. The CLI path needs no token; its audit label is user@host.
- depends on: t12, t13
- covers: c30, h25, c9, h15, c38, c23, h2
- acceptance:
  - a write to a machine whose reported version is below the first boundaries release is refused before any transport call, with the needed version in the message
  - every write through the service appends one line (label or user@host, time, machine, old rule, new rule, outcome) to an append-only audit file with mode 0600 in the reader state dir
  - with a fake transport that applies and then times out, the service re-reads the file version and reports saved; repeating the same request changes nothing
  - heat-governor boundaries list|add|set|rm --machine M goes through this service, and its list --json equals the boundary data in mesh status --json and MCP `machine_state` for that machine

### t15 — PR 2: mesh serve accepts token-guarded boundary reads, previews and writes

- instruction: Owns `heat_governor`/`mesh_server.py`, `heat_governor`/cli/`_commands`/mesh.py (the --token-file flag and help text) and tests/`test_mesh_server.py`. Routes under /api/boundaries/MACHINE, where MACHINE is a fleet machine name. Add method dispatch and a JSON body reader; map service errors to 4xx explicitly instead of the catch-all 500. Update the read-only wording in the docstrings and help. Token fixtures in tests must pass scripts/scan-secrets.py.
- depends on: t14
- covers: c8, h14, c36, h31, c17, h22, h7
- acceptance:
  - tests/`test_mesh_server.py`: with no token file every write and preview gets 403; a missing or wrong token gets 401; a token in the query string is ignored; a stale If-Match gets 409 with the current rules; wrong Origin, Host or Content-Type is refused; bodies over the cap get 413
  - mesh serve --token-file reads label = token lines, exits 2 if the file is readable by group or others, compares with hmac.`compare_digest` and never logs a token
  - every GET that exists today returns the same status and body as 0.17.1, and all existing mesh server, fleet reader and transport tests pass unedited apart from `test_post_not_allowed`

### t16 — PR 2: the boundary editor sub-view and modal confirmations in the dashboard

- instruction: Owns `heat_governor`/web/index.html and tests/`test_web_page.py`. Source design: artifact FZzmdU4yBpTbboV4WFUX7x, boards Edit.dc.html and Confirm.dc.html. Process names is a list input (comm is a TOML list). The kill warning states the real conditions: own user, not protected, at least `min_victim_mib`, action = kill, acted on within about 12 s. New CSS: buttons, icon buttons, fields, modal. Inline stroke SVG icons are allowed; no new vendored file.
- depends on: t4, t15
- covers: c12, h18, c15, h20, c32, h27, c38, h32
- acceptance:
  - the hash routes #boundaries/MACHINE/RULE (a machine name and a rule name) and a new-rule route open a full sub-view with the form beside a live preview and a back link; rules from config.toml show as read-only
  - the form's rules and base version come from the file read on the box; a rejected file on disk shows its message with the last good rules marked as live
  - delete, a change that would kill a process, and a conflict each open a modal dialog with focus handling and Escape to close; without a token the page offers a token field and stays read-only; the token is kept for the tab session only and sent only in the Authorization header
  - every action is a button, input, select or label with an accessible name; validator messages show for any field; a failed or timed-out save ends in saved, not saved or conflict after a re-read

### t17 — PR 2: contract, working rules, harness files, version bump and changelog for the write side

- instruction: Use the version-bump skill (minor) and the cicd skill for the PR; the body closes issue #18. Run ask-colleague review on the diff before presenting it.
- depends on: t10, t16
- covers: c41, h35, c1, h1, c13, h19
- acceptance:
  - docs/contracts/boundaries.md documents the file, the merge and collision rule, the request shape, versions and conflicts, backups and how to restore one, tokens, the audit log and the event; docs/contracts/state.md and docs/integration.md are updated
  - CLAUDE.md, AGENTS.override.md, AGENTS.colleague.md and QWEN.md state that live boundary writes and enabling a token file need the user's go-ahead and that tests never write under a real config directory
  - the contract has a table mapping each acceptance criterion of issue #18 to the claim and test that covers it, or to out of scope
  - version bumped by one minor; pyproject.toml dependencies is \[\]; the full test suite and every lint and gate command in CLAUDE.md pass

### t18 — PR 2: staged live check of the write path, with the user's go-ahead

- instruction: This writes to live machines and enables a token on the EliteMini mesh unit: ask the user before each step. Order: local machine first, then one remote box. Never save a rule that matches a running process during this check. File devague evidence for obligation o17 with the outcome, pass or fail.
- depends on: t17
- covers: c28, h7, c25, h4
- acceptance:
  - docs/evidence has a dated note: a rule that matches no running process is created from the page on the local machine and then on one SSH machine, appears in that machine's `mem_guard`.boundaries in under 15 s, and is visible in heat-governor boundaries list --machine M
  - the note shows a write with no token refused with 403 or 401 and a stale version refused with 409 with the file unchanged, and ends with the test rule deleted

## Risks

- [follow_up] `heat_governor`/daemon.py is edited by t7, t9 and t10 and `heat_governor`/web/index.html by t4 and t16; they are chained by dependencies so no wave has two writers of one file, which makes PR 2's daemon work serial
- [unknown_nonblocking] The repo has no browser test harness: tests/`test_web_page.py` reads the HTML as text, so focus trapping, Escape and the session-kept token in t16 are checked by source assertions and by hand, not by a real browser (task t16)
- [unknown_nonblocking] The cost of an on-demand /proc scan for a preview on thor and the sparks at 8-10% free memory is unmeasured; t18 should measure it before previews are used freely (task t11)
- [follow_up] PR #13 (0.16.2, VENTUNO Q limits) is still open against an older version; both PRs here bump from main and must rebase if #13 merges first
- [follow_up] Downgrading a box below the first boundaries release silently drops its page-managed rules; the release notes of PR 2 must say so (task t17)

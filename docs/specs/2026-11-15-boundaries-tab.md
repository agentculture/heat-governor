# boundaries-tab

> The fleet dashboard has a Boundaries tab: an operator reads every machine's memory-guard boundaries and their live state, and creates, changes and deletes them from the page, without logging in to each box (issue #18)

## Audience

- The operator of the six-box fleet who opens the mesh serve dashboard on the EliteMini, and agents that read the fleet through heat-governor mesh status, the MCP tools and the boundaries CLI

## Before → After

- Before: Boundaries are \[\[memory.allow\]\] rules hand-edited in each box's config.toml over SSH; no box has one configured; the dashboard, mesh status and the MCP tools show no memory-guard data at all, and mesh serve is read-only and unauthenticated
- After: The dashboard has a Boundaries tab listing, per machine, the guard's action, limits, kills and every boundary with its matchers, usage and state; with a write token the operator adds, changes or deletes a boundaries.toml rule on any machine from the page or from heat-governor boundaries, sees which running processes it matches before saving, and the daemon applies it on its normal live reload

## Why it matters

- All six boxes run action = kill with no boundaries, so known large workloads (vLLM near full memory on thor and the sparks) are judged by growth alone; setting boundaries per box by hand is slow and unreviewed, and a wrong rule can make the guard kill a process

## Requirements

- `fleet_reader` passes each daemon's `mem_guard` through to /api/fleet, mesh status and the MCP `machine_state`, tolerating its absence on daemons older than 0.17 (today extract() keeps only `THERMAL_FIELDS`, `heat_governor`/`fleet_reader.py`:79-98,225-243; mesh.py `_row` whitelists columns)
  - honesty: A daemon document without `mem_guard` still yields state ok, and the history ring and /api/history are unchanged
- The daemon's `mem_guard` document gains what the page shows and /state lacks today: each boundary's matchers (comm, cmdline, cgroup) and source file, plus `act_over_pct`, `max_kills_per_hour` and `min_victim_mib`. Today an entry is exactly name, `max_mib`, scope, `usage_mib`, state (memboundary.py:165-172; docs/contracts/state.md:52), and adding keys is allowed ('clients ignore unknown fields')
  - honesty: docs/contracts/state.md documents every added `mem_guard` key, and a client that reads only the 0.17 keys still works
- The config loader merges \[\[memory.allow\]\] rules from a sibling boundaries.toml into \[memory\] before validation, rejects a name that appears in both files, watches both files' mtimes, and keeps the last good boundaries when only that file is invalid (today load() reads one path, Config.path/mtime are scalars, changed() compares one mtime: config.py:885-909, daemon.py:502-540)
  - honesty: A name present in both config.toml and boundaries.toml is rejected with a message naming both files, and an invalid boundaries.toml keeps the last good boundaries while a valid config.toml edit still applies
- A stdlib emitter writes the boundaries.toml \[\[memory.allow\]\] shape, escaping control characters and newlines in matcher strings, and the write is atomic (temp file + os.replace) with a backup. No emitter exists: `_toml_value` (config.py:924-935) handles scalars/lists only and escapes only backslash and double quote; config init --write is a plain `write_text` (tuning.py:120)
  - honesty: Emitting any valid rule set and loading it back with tomllib yields the same rules, including matchers containing quotes, backslashes and non-ASCII; a failed write leaves the previous file intact
- A new boundaries CLI noun (overview, list, add, set, rm, a stdin apply used by the remote write, and a preview) follows the agent-first contract: register() in cli/`__init__.py`, overview verb and bare-noun overview, explain catalog entry, learn text, overview.`_VERBS` line, --json and exit codes
  - honesty: uv run teken cli doctor . --strict passes and explain resolves every boundaries path
- The SSH and local transports gain one fixed, argv-only write path that can send a body on stdin and return the remote exit code and stderr (the validator's message), with its own timeout and a defined way to find heat-governor on the remote box
  - honesty: The remote command is a fixed argv with the rule data only on stdin, never interpolated into the command string, and the validator's message reaches the caller verbatim
- mesh serve accepts boundary writes: method dispatch for POST/PUT/DELETE, a capped JSON body reader, a bearer token compared in constant time (401 on a bad token, 403 when none is configured), Origin and Content-Type checks, If-Match with 409 on a stale version, and 4xx error mapping. `test_post_not_allowed`, the read-only docstrings and the mesh serve help text change with it
  - honesty: With no token file every write is refused with 403 and GET responses are byte-for-byte what they are today; tokens are compared with hmac.`compare_digest` and never logged
- The daemon, not the CLI, emits `memory_boundary_changed` when it reloads a changed boundaries file, and the kind joins recorder.`MEMORY_EVENTS` so it is recorded and shipped; the reader keeps its own audit log (token label, time, machine, old and new rule) in its state dir
  - honesty: A boundaries change made by hand on the box also produces `memory_boundary_changed`, and the reader's audit log is append-only with mode 0600
- A preview runs on the target box: memprocs.scan + memboundary.assess for within/over/`far_over` per matching process, with the killer's names function, uid, protect list, `min_victim_mib` and action deciding whether a far-over process would really be killed; command lines pass through redact() and the container column comes from `container_id`(cgroup)
  - honesty: The preview and the live guard agree: for the same process table and rule, the preview's state and killable verdict equal what MemKiller decides
- The Boundaries tab is built inside `heat_governor`/web/index.html as a second view of the one React/htm page: a hash-routed Sections nav (Machines | Boundaries) in the header, reusing the existing classes (.pills button\[aria-current\], .chip, .status, .tag, .facts, .dt, .search, .eyebrow, .lede, Mark/rune, useFleet) and the html\[data-mode\] theme, not the design's .app.dark class or its anchor-styled pills
  - honesty: tests/`test_web_page.py` still passes, #machines renders exactly as today, and the page loads nothing from another origin
- New styles are limited to what the dashboard does not have yet: buttons (.btn, primary, danger), icon buttons, form fields (input, select, label, hint, error state), a usage bar with boundary and far-over marks, a left-aligned rules table, and a modal confirmation dialog; the design's inline stroke SVG icons (plus, pencil, bin, lock, warning, refresh) are a new vocabulary for the page
  - honesty: Every new control is a real button, input, select or label reachable by keyboard, and the modal traps focus and closes on Escape
- The page covers states the design does not draw: a machine that is unreachable or stale, a daemon with no `mem_guard` (older than 0.17 or guard off), action = record (a far-over process is not killed), a save that fails or times out over SSH, a validator rejection for any field (the design shows it for `max_mib` only), and a rule whose `max_mib` is under `min_victim_mib` (blamed, never killed)
  - honesty: Each listed state has a rendering covered by a page test or a fixture document
- The reader runs the remote write and preview by an explicit binary path, default ~/.local/bin/heat-governor with a per-machine override key in fleet.toml, and the on-box command writes boundaries.toml beside the config file the running daemon reports in /state (config, daemon.py:1061), not beside whatever the SSH shell's environment resolves (challenge pass, 2026-11-15)
  - honesty: On every fleet machine the write succeeds with heat-governor absent from the non-interactive SSH PATH, and a daemon started with --config elsewhere gets its boundaries.toml beside that file
- The reader refuses a write to a machine whose daemon version predates boundaries.toml support, with a message naming the version needed, and the page shows that machine as read-only until it is upgraded; a missing remote boundaries verb is reported as that same refusal, not as 'unreachable' (challenge pass, 2026-11-15)
  - honesty: A write aimed at a 0.17.x daemon is refused before any file is touched on the box
- The version check and the write are one atomic step on the box: the on-box command takes an exclusive lock, compares the caller's base version with the hash of the file as it is now, and only then writes; the reader never decides a conflict on its own copy (challenge pass, 2026-11-15)
  - honesty: Two concurrent applies with the same base version produce exactly one success and one conflict, and a hand edit between read and save is reported as a conflict
- The editor's rule list and base version come from the boundaries.toml file read on the box, not from /state, and the reader passes through the daemon's config path and `config_error` so the page can say when the file on disk was rejected and the daemon is still running the last good rules (challenge pass, 2026-11-15)
  - honesty: With an invalid boundaries.toml on a box the page shows the rejection message and the last good rules as live, and an edit starts from the file's content
- A change that only touches boundary rules does not lose the guard's detection state: the fill-rate window and the kill budget survive it, either by swapping the rules in the running guard or by carrying the trigger window across the rebuild (challenge pass, 2026-11-15)
  - honesty: A test saves a boundary while a simulated runaway is filling memory and the trigger fires no later than it does without the save
- The kill confirmation is enforced on the box, not only drawn in the page: the on-box apply re-runs the preview at write time and refuses a change that would put a killable own-user process far over its boundary unless the request explicitly allows it, returning the processes concerned (challenge pass, 2026-11-15)
  - honesty: An apply without the allow flag that would make a running own-user process far over writes nothing and returns that process; the same request with the flag writes
- The validator rejects a boundary whose matchers are all bare wildcards, and the preview reports how many processes a rule matches, because a rule that matches everything with a large maximum makes every process 'within' and so switches the guard off for that box (challenge pass, 2026-11-15)
  - honesty: A rule with cmdline = '\*' and no other matcher is rejected with a message, on the page and in the CLI
- Every endpoint that returns process command lines or accepts a change requires the token, including the preview; the token travels only in the Authorization header, never in a URL; mesh serve refuses to start with a token file readable by group or others; write requests are refused when the Host header is not the address the server listens on (challenge pass, 2026-11-15)
  - honesty: An unauthenticated request to the preview endpoint gets 401 or 403 and no process data, and a token file with mode 0644 stops mesh serve with an error
- A save whose response is lost is not left ambiguous: writes are whole-rule and idempotent, and after a timeout the page re-reads the file version and reports saved, not saved or conflict from what is on the box (challenge pass, 2026-11-15)
  - honesty: With a fake transport that applies the write and then times out, the page ends in 'saved' and a repeat of the same request changes nothing
- Recovery is specified: each write keeps a timestamped backup of the previous boundaries.toml, at most 5 are kept, and the contract documents how to restore one; deleting boundaries.toml is a valid state that means no page-managed rules (challenge pass, 2026-11-15)
  - honesty: After 7 writes exactly 5 backups exist and restoring the newest one reproduces the previous rules after a reload
- The daemon remembers the hash of the boundaries it last applied in its state dir and emits `memory_boundary_changed` at startup when the file differs, so a change made while the daemon was down or restarting is still recorded and shipped (challenge pass, 2026-11-15)
  - honesty: Stopping the daemon, changing boundaries.toml and starting it again yields one `memory_boundary_changed` event with the old and new rules

## Honesty conditions

- Every acceptance criterion of issue #18 maps to a confirmed claim in this frame or is listed as out of scope
- pyproject.toml dependencies stays \[\] and the CSP string in `mesh_server.py` is unchanged
- The write endpoint and boundaries apply accept only \[\[memory.allow\]\] rule fields; any other key is rejected by the validator
- The existing mesh server, fleet reader and transport tests pass unmodified except `test_post_not_allowed`
- The page, mesh status --json, the MCP `machine_state` tool and boundaries list --json all return the same boundary data for a machine
- On 2026-11-15 /api/fleet on the EliteMini reader carried no `mem_guard` key and tests/`test_mesh_server.py`::`test_post_not_allowed` asserts POST is refused
- A rule created from the page on one machine is visible in heat-governor boundaries list --machine M and in that machine's /state without any manual step on the box
- docs/deliveries/2026-11-14-memory-guard.md:117 states 'No \[\[memory.allow\]\] rule is configured on any box yet'
- A test feeds a /state document with and without `mem_guard` through `fleet_reader`.extract and gets the key through unchanged or absent, never an error
- Tests cover 401, 403, 409 and a successful write through a fake transport and a localhost server, and the live check on one SSH box is recorded under docs/evidence before the write path is called live
- CLAUDE.md, AGENTS.override.md, AGENTS.colleague.md and QWEN.md carry this rule next to the existing run --apply and \[memory\] action rules, and no test touches the real config directory

## Success signals

- After PR 1, /api/fleet returns `mem_guard` for all 6 machines and the Boundaries tab shows each machine's guard state within 1 poll of the reader
- After PR 2, a rule saved from the page on a local and on an SSH machine appears in that machine's `mem_guard`.boundaries in under 15 s, a write with no or a wrong token gets 403 or 401, and a stale version gets 409 with nothing written

## Scope / boundaries

- The page stays self-contained: Lato from /vendor, no Google Fonts link (the design's helmet loads fonts.googleapis.com), no new vendored library, no third-party runtime dependency, CSP unchanged
- The page edits boundaries.toml rules only: it never changes \[memory\] action, `act_over_pct`, limits, \[ship\] or thermal settings, and rules written in config.toml show read-only (issue #18 Safety and Out of scope)
- GET /api/fleet, /api/history and the Machines view keep their behaviour, and remote daemons keep listening on loopback only, reached over the reader's SSH link (issue #18 acceptance criteria and Out of scope)
- Writing a boundary changes a live machine: the first live write on any box, enabling a token file on the EliteMini mesh unit, and any test or probe against a real box need the user's go-ahead; tests use `tmp_path` config dirs, a fake transport and fake /proc trees and never write under a real ~/.config or signal a process they did not spawn (challenge pass, 2026-11-15)

## Non-goals

- The Survey and Decisions tabs of docs/design/fleet-dashboard.html are not restored, and there are no user accounts or roles beyond the bearer token

## Assumptions

- Where the design hard-codes sample values the page computes them: bar marks from each machine's `act_over_pct` (the design fixes them at 58.8% and 88.2%, which is `act_over_pct` 50 only), the guard badge from `mem_guard`.action and armed (the design always shows 'armed · kill'), the rune from rune(name), and usage labelled as anonymous memory, the metric the guard judges
- The dashboard stays plain HTTP: the tailnet (WireGuard) is the transport encryption for the token, and mesh serve is never bound to a non-tailnet, non-loopback address while a token file is configured (challenge pass, 2026-11-15)
- The boundaries CLI run directly on a box or with --machine uses the operator's own SSH access as its authority and needs no token; its audit entries name user@host instead of a token label (challenge pass, 2026-11-15)

## Scope exploration

- `s1` — `heat_governor/fleet_reader.py + cli/_commands/mesh.py + mcp_server.py`: extract() keeps only `THERMAL_FIELDS` (`fleet_reader.py`:79-98,225-243) so `mem_guard` published at daemon.py:1091 is dropped; history Ring stores compact tuples so passing it through adds no storage; MCP `machine_state` returns the whole entry (`mcp_server.py`:104-116) but mesh status `_row` whitelists columns (mesh.py:178-197)
  - seeds: `c2`
- `s2` — `heat_governor/memguard.py + memboundary.py + docs/contracts/state.md`: MemGuard.`_build_doc` (memguard.py:228-241) publishes action, armed, `avail_mib`, `last_kill`, `kills_last_hour`, limits{`floor_mib`,`soft_mib`,source}, boundaries; entries carry no matchers, no source, and `act_over_pct` / the kill-budget cap are not published, yet the design draws matcher chips, a source tag, the far-over mark and 'budget of 3 per hour (1 used)'
  - seeds: `c3`
- `s3` — `heat_governor/config.py + daemon.py reload`: Single-file loader and single-mtime reload (config.py:885-909; daemon.py:502-540, `reload_every_s` = 2.0); `_validate_allow` checks name uniqueness within one list only (config.py:580-590), and `applied_rule` breaks ties by smallest `max_mib` then list order (memboundary.py:63-69), so merge order and cross-file collisions must be defined; a rejected file today keeps the whole last good config
  - seeds: `c4`, `c5`
- `s4` — `heat_governor/cli/_commands + explain/catalog.py`: No boundaries noun exists; memory has overview/status/top/replay (cli/`_commands`/memory.py:321-390); config verbs live in tuning.py and only init --write mutates; noun wiring is asserted by tests/`test_cli_memory_ship.py`:40-50, tests/`test_cli.py`:111 and tests/`test_mcp.py`:282
  - seeds: `c6`
- `s5` — `heat_governor/transport.py + fleet.py`: SshTransport only runs curl 127.0.0.1:9476/state and python3 -c `HISTORY_SCRIPT` (transport.py:127-137); `_run` pins stdin to DEVNULL (transport.py:155); `_check` collapses any non-zero exit into Unreachable (transport.py:181-185); it never invokes heat-governor remotely and fleet.toml `MACHINE_KEYS` is only name, host, transport (fleet.py:21); the 5 s fetch timeout would also bound a write
  - seeds: `c7`
- `s6` — `heat_governor/mesh_server.py + tests/test_mesh_server.py`: GET/HEAD only: `do_POST` = `do_PUT` = `do_DELETE` = `do_PATCH` = `_not_allowed` (`mesh_server.py`:160), `_route` branches on path only (97-108), `_guarded` maps every exception to 500 (144-149), MeshServer(reader, host, port) carries no token or audit settings, mesh serve has no --token-file (cli/`_commands`/mesh.py:337-361); no Origin/Authorization handling anywhere; the page's CSP (default-src 'self') already allows same-origin fetch
  - seeds: `c8`
- `s7` — `heat_governor/recorder.py + ship.py + daemon.py event routing`: Only the daemon holds Recorder and Shipper; shipping is enumerated via `SHIP_EVENTS` = `MEMORY_EVENTS` (daemon.py:117, 937-946; recorder.py:165), so a non-daemon boundaries apply cannot record or ship an event; the reader state dir (`default_data_dir`, state.py:32-34) holds only ssh/\*.sock today
  - seeds: `c9`
- `s8` — `heat_governor/memprocs.py + memkill.py (preview)`: scan() and assess() are pure and daemon-free (memprocs.py:99; memboundary.py:180-192) and memory top already uses scan; killability lives in MemKiller.refusal (memkill.py:144-156,247-259): other-uid, protected names, below `min_victim_mib` (default 2048), root daemon; Proc has a numeric uid and no username or container field (memprocs.py:33-42); default names omits the exe basename the killer adds (memkill.py:126)
  - seeds: `c10`
- `s9` — `heat_governor/web/index.html vs artifact Main.dc.html`: Shipped App() renders only Machines with no Sections nav (index.html:1318-1333); the nav with location.hash routing exists in docs/design/fleet-dashboard.html:1015-1029 (tabs Machines, Survey, Decisions) and was dropped when the page shipped. The design matches the shipped layout: header, 74rem main, eyebrow + h1, summary chips, filters row, 16px-radius surface cards, .tag, .facts, rune marks. It restyles things that already exist: .badge duplicates .status, span chips with a .dot duplicate button chips with ::before, .code duplicates code
  - seeds: `c11`
- `s10` — `heat_governor/web/index.html component inventory`: The shipped page has no form controls beyond .search, no solid buttons (.addbtn, .linkbtn, .xbtn, .clear only), no modal (the drawer is role=dialog with a non-blocking scrim, index.html:1205), no inline icons (grep for 14-20px svg finds none), and table.dt is right-aligned numeric (index.html .dt th/td text-align:right); .scale is the heat-gradient thermometer, not a usage bar
  - seeds: `c12`
- `s11` — `heat_governor/web/vendor + mesh_server CSP`: index.html:11-15 loads Lato from /vendor/\*.woff2 and the CSP is default-src 'self' (`mesh_server.py`:47-50), so the design's Google Fonts stylesheet would be blocked; pyproject dependencies = \[\] per CLAUDE.md
  - seeds: `c13`
- `s12` — `artifact Main.dc.html sample data vs memboundary.py`: classify() uses usage > max\*(1+pct/100) with `act_over_pct` a global \[memory\] key, range 1-1000 (memboundary.py:72-78; config.py:262-263,550); usage is RssAnon not RSS (memboundary.py:208-219); the design gives elitemini the rune for e, which rune() does not map (index.html:285 falls back to the f rune); the design's comm field is comma-separated text while comm is a TOML list (config.py:147-154)
  - seeds: `c14`
- `s13` — `artifact Edit/Confirm boards vs kill semantics`: The kill warning says the guard kills 'within about 10 seconds'; from code it is up to 2 s reload (daemon.py:106) plus up to `top_every_s` 10 s in normal memory (memguard.py:300-310), and no kill happens when action = record (memkill.py:368-369), the uid differs, the name is protected or the process is under `min_victim_mib` (memkill.py:144-156,247-259); the Machines view already draws unreachable, stale and unsupported (index.html .lane.unreachable/.stale/.unsupported)
  - seeds: `c15`
- `s14` — `issue #18 (scope, safety, out of scope)`: Issue #18 limits writes to boundary rules, keeps GET unchanged, keeps daemons on loopback and excludes roles beyond a bearer token; the shipped page header says it was built from the locked design 'Machines view, Polar night skin only' (index.html:6-8)
  - seeds: `c16`, `c17`, `c18`
- `s15` — `challenge pass / adjacent-systems lens: live probe of arduino, spark, spark2, thor, orin over ssh (read-only)`: command -v heat-governor fails in a non-interactive ssh shell on 4 of 5 boxes (only spark has it on PATH); the binary is at ~/.local/bin/heat-governor on all 5 and every unit runs 'run --config %h/.config/heat-governor/config.toml'; all 5 report heat-governor 0.17.0; config dirs already hold ad-hoc config.toml.bak-\* files
  - seeds: `c29`
- `s16` — `challenge pass / lifecycle lens: version skew between reader and daemons`: all six boxes run 0.17.x today and the reader already carries each daemon's version (`THERMAL_FIELDS`, `fleet_reader.py`:79-98); a file written for a daemon that does not read boundaries.toml would be accepted and silently ignored; transport.`_check` turns any non-zero exit into Unreachable (transport.py:181-185)
  - seeds: `c30`
- `s17` — `challenge pass / concurrency lens: page, CLI and hand edits on one boundaries.toml`: three writers exist (page via reader, boundaries CLI, an editor on the box) and the spec's 409 was specified only at `mesh_server`; no file locking exists anywhere in config.py or tuning.py (config init --write is a plain `write_text`, tuning.py:120)
  - seeds: `c31`
- `s18` — `challenge pass / data-flow lens: /state vs the file on disk`: `mem_guard`.boundaries reflects the rules the guard loaded, and a rejected edit keeps the last good config (daemon.py:510-518) while `config_error` is published at daemon.py:1064 but dropped by `fleet_reader`.extract; an ETag computed from /state would not describe the file being overwritten
  - seeds: `c32`
- `s19` — `challenge pass / failure-mode lens: heat_governor/daemon.py set_memory + memtrigger.py`: `set_memory` stops and restarts the whole guard on any MemoryRule inequality and carries only killer.`export_state`() (daemon.py:767-781); Trigger keeps its 10 s slope window in a deque on the instance (memtrigger.py:60-72) and needs `MIN_SAMPLES` = 3, so every boundary save from the page resets runaway detection on that box
  - seeds: `c33`
- `s20` — `challenge pass / security + reversibility lens: preview-to-save gap`: the design's kill warning is a page dialog fed by a preview taken seconds earlier; a SIGKILL cannot be undone and the guard acts within reload 2 s + scan up to 10 s (daemon.py:106; memguard.py:300-310); nothing in the spec made the confirmation binding for the CLI path or for a process that appears after the preview
  - seeds: `c34`
- `s21` — `challenge pass / security lens: what a boundary write can do (docs/contracts/memory.md Boundaries)`: within its boundary a process is never blamed or killed (issue #18 background; memboundary `applied_rule`, memboundary.py:63-69), so the write path can disable the guard as well as trigger kills; `_validate_allow_fields` requires only one non-empty matcher (config.py:600-607)
  - seeds: `c35`
- `s22` — `challenge pass / security lens: heat_governor/mesh_server.py + the EliteMini unit`: the server has no auth, Origin or Host handling and its CSP allows inline script (`mesh_server.py`:47-50); the live unit binds 100.95.106.3:9480 over plain HTTP (systemctl --user cat heat-governor-mesh.service); the spec guarded writes but left the preview, which returns command lines of another box, unclassified
  - seeds: `c36`, `c37`
- `s23` — `challenge pass / failure-mode lens: half-completed write over SSH`: the reader's `timeout_s` is 5 s in the live fleet.toml and SshTransport kills the child on expiry (transport.py `_collect`); a write can therefore succeed on the box while the reader reports a failure
  - seeds: `c38`
- `s24` — `challenge pass / rollback + observability lens: backups and the change event`: the spec said 'atomic + backup' without a count or a restore path, and the boxes already accumulate unbounded config.toml.bak-\* files (live probe); the change event was tied to a reload (c9), and `maybe_reload` only compares against the config the running process loaded (daemon.py:502-540), so a restart between change and reload emits nothing
  - seeds: `c39`, `c40`
- `s25` — `challenge pass / operations + overlooked-actors lens: CLAUDE.md working rules and the CLI path`: CLAUDE.md requires the user's go-ahead for run --apply, \[memory\] action = kill and \[ship\] and keeps tests off real /sys and /proc, but has no rule for a remote config write; the spec's token model covered the page only, while c6 gives the same write to a CLI that talks SSH directly
  - seeds: `c41`, `c42`
- `s26` — `challenge pass / reversibility lens: PR 1 (read side)`: clean: PR 1 only adds keys to /state (docs/contracts/state.md says clients ignore unknown fields) and to /api/fleet, adds no write path, and the history ring is untouched (`fleet_reader.py`:442-482); reverting it removes the tab and nothing else
- `s27` — `challenge pass / unexamined surfaces`: not examined: the two sparks' config.pair.toml and whether \[pair\] reload interacts with a second watched file; the cost of a preview process scan on thor under full AI load; real browser behaviour of the modal and the session-kept token (the repo has no browser test harness, tests/`test_web_page.py` reads the HTML as text); shipping the new event to R2 or AWS; scripts/scan-secrets.py against token fixtures in tests and docs

## Decisions

- Colours: both tabs use the tokens already in `heat_governor`/web/index.html; the Machines view does not change, and the contrast of small coloured text is a follow-up (user, 2026-11-15)
- Editor: create and change open as a full sub-view under #boundaries (form beside the live preview, back link); delete, kill warning and conflict are real modal dialogs (user, 2026-11-15)
- Tokens: the token file holds several labelled tokens; the audit log and the conflict dialog name the label; the browser keeps the pasted token for the tab session only (user, 2026-11-15)
- Delivery: PR 1 is the read side (`mem_guard` pass-through, richer /state, a read-only Boundaries tab); PR 2 is the write side (boundaries.toml, the boundaries CLI noun, the transport write path, auth, the editor and confirmations) (user, 2026-11-15)

## Hard questions

- Delivery slicing: ship the read-only tab first (`mem_guard` pass-through, richer /state, the tab) and the write path (boundaries.toml, CLI noun, transport write, auth, editor) in a second PR, or everything in one? (resolved: User (2026-11-15): read first, then write: PR 1 is `mem_guard` pass-through, richer /state and a read-only Boundaries tab; PR 2 is boundaries.toml, the CLI noun, the transport write path, auth, the editor and confirmations)
- Token model: the design shows token labels ('Writes enabled, token ops-laptop'; 'saved 09:41 from token spark-ops'), which implies several named tokens; the issue proposes one --token-file. One token, or a file of labelled tokens? And does the browser keep the pasted token in memory only or in sessionStorage? (resolved: User (2026-11-15): labelled tokens: the token file holds several label = token entries, the audit log and conflict dialog name the label, and the browser keeps the pasted token for the tab session only)
- Colour tokens: the design's light theme uses darker cool/warm/hot/accent (#2f7fa6 / #a8661a / #c4371f / #c75a1f) and a darker --ink-faint (#8a8477) than the shipped page (#3a8fb8 / #c27a24 / #d23f25 / #e06a2a, #9f998c), and different dark-mode --ink-soft, --ink-faint and --hot. Keep the shipped tokens for both tabs, adopt the design's darker ones for both, or let the tabs differ? (resolved: User (2026-11-15): keep the shipped tokens in web/index.html for both tabs; the Machines view does not change; contrast of small coloured text is a follow-up)
- Edit surface: the design draws create/change as a separate full page (two columns: form + live preview) and the four confirmations as cards on a third page. The shipped page has one pattern for secondary content, the right-hand drawer (min(800px,100vw), index.html .drawer). Open the editor as a full sub-view under #boundaries, or in the existing drawer with the preview stacked under the form? And are confirmations true modal dialogs (new to the page)? (resolved: User (2026-11-15): the editor is a full sub-view under #boundaries (form beside the live preview, back link); confirmations are real modal dialogs)

## Open parks

- [unknown_nonblocking] The conflict dialog names what changed and which token saved it; a hand edit on the box has no token, so the reader can only attribute changes it wrote itself (its audit log) and must show 'changed on the box' otherwise
- [unknown_nonblocking] Downgrade: a box rolled back to a version that does not read boundaries.toml silently loses its page-managed boundaries, so protected workloads fall back to growth-only judgement; whether to warn in the release notes or migrate rules into config.toml on downgrade is undecided
- [unknown_nonblocking] A preview runs a full /proc scan on the target box on demand; its cost on thor and the sparks at 8-10% free memory is unmeasured, and a rate limit per machine may be needed
- [follow_up] Light-theme contrast: shipped --warm #c27a24 and --ink-faint #9f998c are used as small text on --surface; whether they meet 4.5:1 is unmeasured and bears on the token question
- [follow_up] Plan-side: PR 2's live check on one SSH box must be staged (dry local machine first, then one remote box with a rule that matches nothing running) and recorded under docs/evidence

## Resolved vagueness

- [unknown_nonblocking] How the reader finds heat-governor on a remote box for the write and preview commands (PATH over non-interactive ssh, a uv tool path, or a per-machine key in fleet.toml) is unverified on the six boxes — resolved: User (2026-11-15) confirmed c29 after a read-only probe: the binary is at ~/.local/bin/heat-governor on all five remote boxes and on PATH for non-interactive ssh on only one, so the reader calls it by explicit path with a per-machine override in fleet.toml

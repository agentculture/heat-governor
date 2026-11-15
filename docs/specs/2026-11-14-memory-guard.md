# memory guard

> heat-governor catches a runaway process before it takes the machine down: when memory is about to run out it kills the biggest own-user grower, records who used the memory and what started it, and keeps that record in S3 so a dead machine's last minutes survive off-box.
> instruction: Acceptance = success signals c26 and c27 pass and 'heat-governor memory status' / 'ship test' work on the EliteMini.

## Audience

- The operator (Ori) and the agents that run heavy jobs on the fleet (Claude Code sessions, culture mesh agents, sparkrun/lobes clients); first the EliteMini, where a runaway test reset the box on 2026-11-14.
  - instruction: Check the README and docs/integration.md name the operator and agent clients as the readers of memory-guard state and evidence.

## Before → After

- Before: Before: heat-governor recorded mem:\* and psi:\* every second but acted on none of it and kept no per-process data. On 2026-11-14 a test grew to ~41 GB five times in 17 min, the kernel OOM killer fired late twice, the box thrashed 16 min and was reset, and the only evidence of what ran was the kernel's OOM tables. The governor's last record was 16 min before the reset, and it stayed on the box.
  - instruction: Compare against the incident timeline in the 2026-11-14 records (records/20261114.jsonl 19:34-19:51 local) and journalctl -b -1 -k.
- After: After: a runaway own-user process is SIGKILLed before available memory reaches zero; the box stays up and responsive; the records name the process, its growth, its parent chain and the moment of the kill; and the same evidence plus a rolling black box is in S3 within seconds, readable with 'heat-governor ship fetch' from any machine even if the box never comes back.
  - instruction: Verify end-to-end with the controlled allocation test (success signal) and 'heat-governor ship fetch --host elitemini' from another box.

## Why it matters

- A box that thrashes into a reset loses every agent session and service on it (mesh IRCd, tunnels, mongo, dashboards) and leaves nothing to learn from; killing one runaway costs one test run.
  - instruction: Confirm by listing the services that restarted after the 2026-11-14 reboot (systemctl --user list-units, docker ps).

## Requirements

- A memory guard runs in its own thread, reading /proc/meminfo every ~0.25 s, independent of the 1 s tick: the tick is single-threaded and its docker discover blocked (records 2026-11-14 18:48:30Z 'docker socket TimeoutError'; tick gaps of 133 s and 52 s while memory filled).
  - instruction: Test: a fake tick that sleeps 30 s while the guard thread still polls and fires on a fake /proc/meminfo.
  - honesty: A blocked docker socket or slow tick never delays a guard poll by more than one poll interval.
- Trigger on the MemAvailable slope plus a floor, not on PSI: kill when available < floor (default 5% of MemTotal), or when projected exhaustion (available / fill rate) < horizon (default 10 s) and available < 20%. On 2026-11-14 memory PSI stayed ~0 until available fell below ~2 GB, while each wave took 36-62 s at up to ~1.2 GB/s.
  - instruction: Covered by the replay success signal; unit tests for floor, horizon and slope edge cases with a fake clock.
  - honesty: The trigger fires on all five 2026-11-14 waves with >= 15 s to spare and never on 2026-11-10..13.
- Victim: the own-uid process whose RSS grew most over the last ~10 s, with RSS >= `min_victim_mib` (default 2048), skipping a protect list (the governor and its parent chain, pid 1, systemd, sshd, gnome-shell, Xwayland, tailscaled, dockerd, containerd and any \[memory\] protect entries). Sent SIGKILL (no grace: at 1.2 GB/s a SIGTERM grace period costs GBs). os.kill and `proc_root` are injected for tests.
  - instruction: Unit tests over a fake `proc_root` with a fake kill callable; one test spawns a real 'sleep' child and confirms SIGKILL reaches only it.
  - honesty: The guard never signals a protected process, a process of another uid, its own process or its parent chain, or anything under `min_victim_mib`.
- Per-process tracking in the records: a 'top' snapshot (top-N by RSS: pid, ppid, uid, comm, cmdline redacted and truncated, `rss_mib`, growth MiB/s, start time, cgroup) every 10 s normally and every guard poll once available < 20%; a kill writes an event row with the victim, its parent chain to the session, the last 60 s of memory, and the top list at that moment.
  - instruction: Replay test: write a synthetic run, then a 'memory top --since' read prints the grower with its parent chain.
  - honesty: From the records alone, an operator can name the process that filled memory, its command line and what started it.
- OOM-kill events: read /proc/vmstat `oom_kill` and the delegated cgroups' memory.events each guard poll; a delta writes an event row (counter, cgroup if found), so kernel and memcg kills are on record even when the guard did not act.
  - instruction: Fake /proc/vmstat and memory.events counters stepped in a test; one event row per delta.
  - honesty: Every kernel or memcg OOM kill during the daemon's life appears as an `oom_kill` event row.
- Off-box shipping to S3 with a stdlib SigV4 signer (hmac, hashlib, urllib.request PUT; no boto3): on a kill or OOM event, PUT the event JSON immediately; every 60 s (every 10 s while available < 20%) PUT a rolling black box (last 10 min of records, gzipped) to `<prefix>/<host>/<boot_id>/blackbox.jsonl.gz`; on start after an unclean boot, PUT a postmortem. Runs in its own thread with a bounded queue; failures spool to disk and retry, never blocking the guard.
  - instruction: Test against a localhost ThreadingHTTPServer fake S3 (port 0): success, 500, timeout, server down then up; check the spool drains.
  - honesty: An upload failure or a slow network never blocks the guard or the tick; events are uploaded at least once, retried after a restart.
- Credentials and target from config: \[ship\] bucket, region, prefix, optional profile (read from ~/.aws/credentials and ~/.aws/config via configparser) or `AWS_`\* environment variables, optional endpoint for S3-compatible stores. Secrets are never written to records, logs or uploads.
  - instruction: Test the signer against the AWS SigV4 test-suite vectors (built at runtime, no AKIA literals); grep test output for the fake secret.
  - honesty: Credentials never appear in records, logs, uploads or the state doc, and the SigV4 signature matches AWS's published test vectors.
- Config: new strict \[memory\] (action = record|kill, `floor_pct`, `horizon_s`, `window_s`, `min_victim_mib`, protect, `poll_s`, `top_n`) and \[ship\] sections, added to `TOP_KEYS` with validators and documented in config template and explain config; live-reloadable like the rest.
  - instruction: config check tests for each key's type/range and unknown keys.
  - honesty: An invalid \[memory\] or \[ship\] section is rejected with a specific message, as every other section is.
- CLI: noun 'memory' (overview, status, top) and noun 'ship' (overview, test, fetch), each a module with register(sub) wired in `_build_parser`, listed in overview `_VERBS`, learn text and JSON, and explain catalog ENTRIES; tests copy tests/`test_cli_mesh.py`:266-301.
  - instruction: `test_every_catalog_path_resolves` plus a `test_cli_memory`/ship copy of tests/`test_cli_mesh.py`:266-301; run teken cli doctor . --strict.
  - honesty: Every new path resolves in explain and teken cli doctor . --strict passes.
- report gains memory: for each reboot, the last 60 s minimum available, swap used and peak memory PSI, plus any `memory_kill`/`oom_kill` events before it, so an OOM-driven reset reads as memory, not as 'max 20.0 C'.
  - instruction: Fixture built from the incident shape; assert the reboot entry carries min avail, swap and peak PSI.
  - honesty: report on the 2026-11-14 records says memory, not 'max 20.0 C' only.
- The governor stays responsive under memory pressure: best-effort mlockall(`MCL_CURRENT`|`MCL_FUTURE`) via ctypes when the \[memory\] guard is enabled (logged if refused), and the 'unit' verb emits MemoryMin= for the daemon's unit.
  - instruction: Unit test with a fake mlockall; check 'unit' output contains MemoryMin=.
  - honesty: Under memory pressure the governor's own pages stay resident (mlockall accepted or MemoryMin set) and the result is logged at start.
- The state doc gains an optional `mem_guard` object (action, armed, `last_kill` {t, pid, comm, `rss_mib`}); level, admit and `concurrency_factor` stay thermal-only.
  - instruction: Run tests/`test_client_state.py` against a state doc with `mem_guard` added.
  - honesty: Existing clients (client.read/wait/Gate) behave identically with `mem_guard` present.
- The kill pins its target: open a pidfd (os.`pidfd_open`) for the chosen pid, re-read `/proc/<pid>/stat` start time and uid through it, and signal with signal.`pidfd_send_signal`, so a pid that exited and was reused between the scan and the kill is never signalled.
  - instruction: Unit test: the fake process exits and a new process takes its pid between scan and kill; assert no signal. Probe: python 3.13 here has os.`pidfd_open` and signal.`pidfd_send_signal`; requires-python >= 3.12.
  - honesty: A pid recycled between selection and kill is never signalled.
- Victim growth and size use anonymous memory (RssAnon from `/proc/<pid>/status`), not total RSS, so mmap-heavy services whose file pages the kernel can drop (mongod, mongot) never look like runaways.
  - instruction: Unit test with a fake status file: large RssFile + flat RssAnon is never chosen; growing RssAnon is.
  - honesty: A process whose growth is file-backed pages is never chosen as the victim.
- Repeat offenders are named: the guard keys kills by (uid, exe, cmdline hash); a second kill of the same key within 30 min writes 'repeat': N on the event and on `mem_guard`.`last_kill`, so the relaunching agent or operator can see the loop rather than a series of unrelated kills.
  - instruction: Unit test: three kills of the same cmdline in 30 min yield repeat 1, 2, 3; a different cmdline resets.
  - honesty: A relaunch loop is visible as one repeating offender in the records, state doc and S3.
- Kill budget: at most `max_kills` (default 3) per rolling hour; past the budget the guard drops to record-only, writes a `memory_budget_exhausted` event and ships it, so a wrong threshold cannot kill legitimate work indefinitely.
  - instruction: Unit test with a fake clock: the 4th trigger in an hour records `memory_would_kill` and `memory_budget_exhausted`, never signals.
  - honesty: A misconfigured guard can kill at most `max_kills` processes per hour.
- An http:// endpoint is allowed only for loopback or tailnet (100.64.0.0/10) hosts; any other plain-HTTP endpoint is rejected by config check. SigV4 protects integrity, not confidentiality; tailscale's WireGuard covers the MinIO-on-spark hop.
  - instruction: config check tests: <http://127.0.0.1> and <http://100.x> accepted, <http://example.org> rejected, https accepted; the one plain-HTTP call site carries '# NOSONAR python:S5332 -- loopback/tailnet only'.
  - honesty: Records never travel in clear text over a network the fleet does not own.
- Object keys use the configured machine name (\[ship\] machine, defaulting to the fleet name), never the kernel hostname: here the hostname is 'ori-EliteMini-Series', which carries the operator's name into any bucket (later R2).
  - instruction: Unit test: with machine unset and hostname 'x-Personal-Name', keys use the configured default, not the hostname; config template documents the key.
  - honesty: No object key contains the kernel hostname unless the operator configured it.
- Uploads that fail with RequestTimeTooSkewed (or any 403 within the first minutes after boot), 5xx, timeouts or connection errors stay spooled and retry with backoff; only 4xx errors the config caused (NoSuchBucket, InvalidAccessKeyId) are logged once per hour, not retried in a tight loop.
  - instruction: Fake S3 tests returning RequestTimeTooSkewed then 200, 503 then 200, NoSuchBucket forever; assert spool and log behaviour.
  - honesty: The postmortem written at boot survives a not-yet-synced clock and lands once the clock is right.

## Honesty conditions

- All three parts ship together: kill, per-process records, and S3 upload; none of them depends on the thermal path.
- history.py and report() read the new records unchanged.
- The guard refuses, rather than tries and fails, to target processes of other uids.
- No uploaded object contains a value matching the scan-secrets patterns.
- CI passes: lint, bandit, scan-secrets, teken doctor, version-check, sonar gate with zero unaccepted issues.
- The memory guard's state and evidence are reachable without a shell on the box (state doc `mem_guard` + S3).
- The before-state numbers match the records: 5 waves, 36-62 s each, peak ~1.2 GB/s, gaps 133/52/942 s.
- Killing one own-user process is always cheaper than a reset of the whole box.
- The guard kills before the kernel OOM killer would and before the box thrashes, at the measured fill rates.
- The controlled test is safe to run: it runs inside a MemoryMax scope so a guard failure is contained by the cgroup, not the box.
- The recorded 1 Hz mem:`avail_mib` series is a faithful input for the trigger (same /proc/meminfo field the guard reads).

## Success signals

- In a controlled test on the EliteMini, a process allocating 1.2 GB/s is killed with available memory still >= 2 GB, the governor records no tick gap > 2 s during the run, and the `memory_kill` event is in S3 within 10 s of the kill.
  - instruction: Run a capped allocator (python bytearray growth at ~1.2 GB/s, under systemd-run --user --scope -p MemoryMax=30G as a safety net) with \[memory\] action=kill; check records for min mem:`avail_mib`, max tick gap, and the S3 object timestamp vs the event t.
- Replaying the 2026-11-14 records through the trigger fires on every one of the five waves at least 15 s before available memory fell below 2 GB, and fires 0 times across the 2026-11-10..13 records (no false kills on a normal week).
  - instruction: Add a replay test/command that feeds recorded mem:`avail_mib` series into the trigger and asserts fire times per wave and zero fires on the quiet days.

## Scope / boundaries

- New record rows carry an 'event' key (`memory_kill`, `memory_would_kill`, `oom_kill`) and tick rows gain only an optional 'top' field, so the fleet view's history reader ignores them unchanged.
  - instruction: Run the existing history/report tests over a fixture containing `memory_kill`/`oom_kill` rows and top fields.
- The guard can only signal processes its own uid owns; root and container processes (other uids, e.g. uid 10001 containers here) are out of reach, and docker kill of opted-in containers is not part of this change.
- Uploaded evidence must not leak secrets: cmdlines are truncated and redacted (token/key/secret/password-style arguments), environment variables are never captured, and the credentials never leave the signer.
  - instruction: Test: redact() on cmdlines with --token=, --api-key, PASSWORD=, then run scripts/scan-secrets.py patterns over an uploaded fixture.
- CI constraints: no AKIA literals in tests, '# nosec B310' on urlopen, NOSONAR with a rule id on any hotspot (e.g. plain-HTTP custom endpoint), version bump + CHANGELOG, coverage >= 60%.
  - instruction: Run the CLAUDE.md lint set locally plus 'python3 scripts/sonar-gate.py --pull-request N' on the PR.

## Non-goals

- Memory does not change the thermal level, admit or `concurrency_factor` in this change (state.md:29 defines them thermally); a memory-driven admission hold is a later decision.
- Machine-level containment (MemoryMax/oomd on user-1000.slice or SSH session scopes, a hardware watchdog, kernel.panic) is host configuration, not this repo; the guard is a backstop, documented alongside those recommendations.
- The fleet view (dashboard, mcp, mesh) is not changed in this change; it already shows `mem_used_pct` and ignores event rows. Surfacing kills there is a follow-up.

## Scope exploration

- `s1` — `heat_governor/daemon.py tick()/_act()`: tick() senses, publishes, then runs `_act`() inline; discover() can block on the docker socket, so a guard inside the tick inherits every stall. Records on 2026-11-14 show tick gaps of 133 s, 52 s and 942 s while memory filled.
  - seeds: `c2`
- `s2` — `records 20261114.jsonl (19:34-19:51 local)`: five fill waves, 35 GB -> <2 GB in 62/46/49/38/36 s; peak 1246 MiB/s; psi:`memory_full_avg10` 0.0 until avail < ~2 GB, then 27-98. Slope gives 30+ s warning at 1 Hz; PSI gives seconds.
  - seeds: `c3`
- `s3` — `heat_governor/sense.py SystemProbe`: SystemProbe(`proc_root`, `sys_root`, clock) is the injection pattern (sense.py:118-121); scanning /proc/\*/statm for 422 processes took 6.7 ms here, cheap enough for a sub-second guard.
  - seeds: `c4`
- `s4` — `os.kill precedent (whole package)`: no os.kill or signal sent to another process anywhere in `heat_governor`; only signal.signal handler registration (daemon.py:929-930). The kill path is new code and needs an injected kill callable.
  - seeds: `c4`
- `s5` — `AskUserQuestion answer 2026-11-14`: user chose 'Biggest grower, own user (Recommended)': SIGKILL the ori-owned process that grew most, >= 2 GB, not protected; on for this box, off by default elsewhere.
  - seeds: `c5`
- `s6` — `incident attribution 2026-11-14`: naming the 41 GB process needed the kernel OOM tables (journalctl -k: pid 43985 MainThread under session-545.scope, sibling claude.exe, bash, head) and the sshd log (session from cultureos 100.97.170.7); the governor's records had no per-process data at all.
  - seeds: `c6`
- `s7` — `/proc/vmstat and memory.events (live box)`: this boot already shows `oom_kill` 2: memcg kills of 'node --test js/test/search-panel.test.js' run under systemd-run scopes with MemoryMax (20:10:39, 20:11:40). The governor recorded neither.
  - seeds: `c7`
- `s8` — `heat_governor/history.py`: history skips any row with 'event' (history.py:118) and reads only t, `temp_c`, gpu.`temp_c`, level and system mem:`used_pct` (history.py:118-145); new event rows and a 'top' field cannot break it. Larger tick rows add scan cost (stride/DEADLINE at history.py:96-110).
  - seeds: `c8`
- `s9` — `fleet view (fleet_reader.py, transport.py, mesh.py)`: nothing ships data off-box today: the fleet view is pull-only (SSH curl of /state, LocalTransport loopback urlopen at transport.py:298-307); no S3-capable client exists to reuse.
  - seeds: `c9`
- `s10` — `pyproject.toml dependencies`: dependencies = \[\] (pyproject.toml:16) and CLAUDE.md keeps the runtime dependency-free, so S3 needs a hand-written SigV4 signer, not boto3.
  - seeds: `c9`
- `s11` — `~/.aws on the EliteMini (names only)`: no aws CLI; ~/.aws/credentials holds one profile and ~/.aws/config names \[profile league-evals\]; no bucket is configured anywhere yet.
  - seeds: `c10`
- `s12` — `heat_governor/config.py`: `TOP_KEYS` = {platform, profile, daemon, containers, units, pair} (config.py:126) and validation is strict, so unknown sections are rejected; `DAEMON_KEYS`/`_validate_daemon` (config.py:96-108, 340-353) is the validator pattern.
  - seeds: `c11`
- `s13` — `heat_governor/cli (+ explain/catalog.py, learn.py, overview.py)`: nouns register in `_build_parser` (cli/`__init__.py`:64-98, mesh at 97); every noun needs an overview verb (mesh.py:300-345); `_VERBS` in overview.py:27-35; learn `_TEXT` and commands list (learn.py:42-45, 80-103); catalog ENTRIES (catalog.py:387-425) checked by `test_every_catalog_path_resolves` (tests/`test_cli.py`:111-115).
  - seeds: `c12`
- `s14` — `heat_governor/recorder.py report()`: `_reboot_entry` (recorder.py:78-93) carries only temperature, level, duty, cpus, gpu and zones; the 2026-11-14 reset reported 'max 20.0 C in the last 60 s; last level cool'. `cmd_report` (governing.py:429-445) prints a fixed format.
  - seeds: `c13`
- `s15` — `heat_governor/cli/_commands/governing.py unit verb`: `_UNIT` (governing.py:508-520) has no Memory\*, OOMScoreAdjust or OOMPolicy; a user unit cannot set a negative OOMScoreAdjust, but MemoryMin= works (memory controller delegated to `user@1000.service` here) and `RLIMIT_MEMLOCK` here is ~5.6 GB, so mlockall of a ~50 MB process is possible.
  - seeds: `c14`
- `s16` — `docs/contracts/state.md + client.py`: admit is defined purely by thermal level (state.md:29); reason is the temperature trend (state.md:45); client rule 4 (state.md:97-98) says unknown fields are ignored, so an optional `mem_guard` field needs no schema bump.
  - seeds: `c15`
- `s17` — `docs/contracts/state.md admit semantics`: conflating memory with the thermal level would change what every client's Gate means; kept out.
  - seeds: `c16`
- `s18` — `OOM process tables 2026-11-14`: the victims were uid 1000; container processes in the same table ran as uid 10001 and root, which a user daemon cannot signal.
  - seeds: `c17`
- `s19` — `scripts/scan-secrets.py`: the scanner flags AKIA keys (L52) and \*`access_key`/`secret_key` = <20+ chars> (L68-79), with no inline allowlist; test fixtures must build fake keys at runtime.
  - seeds: `c18`
- `s20` — `CI (.github/workflows/tests.yml, sonar-gate.py)`: sonar-gate fails on any unresolved issue or `TO_REVIEW` hotspot (sonar-gate.py:81-95); precedent is inline '# NOSONAR python:S5332 -- loopback' (transport.py:299); bandit does not skip B310 (pyproject.toml:70-72); version-check blocks unbumped PRs.
  - seeds: `c19`
- `s21` — `systemd-oomd / user-1000.slice (live box)`: oomctl watches only `user@1000.service`; `session-*.scope` and `user-1000.slice` have MemoryMax=infinity; no /dev/watchdog, kernel.panic=0. The memcg-capped re-runs since the reboot show per-command MemoryMax already contains the test.
  - seeds: `c20`
- `s22` — `web/index.html + mcp_server.py`: dashboard shows Memory `used_pct` (index.html:539, 576, 666-674); mcp tools `mesh_status`/`machine_state`/sense carry the same memory object (`mcp_server.py`:32-65); no surface shows events today.
  - seeds: `c21`
- `s23` — `spark (ssh, read-only 2026-11-14)`: no S3-compatible server running (no minio/garage/seaweed containers or listeners on 9000/3900/8333/7480); 520 GB free on /; memory 121 GB total with only 11 GB available at idle-serving, so low MemAvailable is normal there (supports v3: record-only default off the EliteMini).
  - seeds: `c28`, `c5`
- `s24` — `challenge pass / depth`: rigorous: c19 escalation signals present: destructive operation (SIGKILL), security-sensitive (credentials, cmdlines shipped off-box), concurrency (guard thread + shipper thread + tick), live hardware (EliteMini kills, MinIO on spark).
- `s25` — `challenge pass / concurrency lens: kill path vs pid reuse`: selection (scan /proc) and kill are separate moments; at 1.2 GB/s churn the victim may die first. Probe: requires-python >= 3.12 (pyproject.toml), installed 3.13.15 has os.`pidfd_open` + signal.`pidfd_send_signal`.
  - seeds: `c30`
- `s26` — `challenge pass / unstated-assumptions lens: RSS as the size measure`: statm/RSS counts file-backed pages; probe: mongod here has RssAnon 136 MB vs RssFile 93 MB; the 2026-11-14 victim was anon-rss 41 GB (kernel OOM line). Anonymous memory is what exhausts MemAvailable.
  - seeds: `c31`
- `s27` — `challenge pass / failure-modes lens: relaunching caller`: 2026-11-14: the same test was relaunched 5 times in 17 min (records waves at 19:34, 19:36, 19:45, 19:49, 19:51); killing each relaunch alone leaves a loop nobody sees.
  - seeds: `c32`
- `s28` — `challenge pass / containment lens: wrong thresholds on the wrong box`: spark probe: 121 GB total, 11 GB available at idle serving (~9%); a floor tuned on the EliteMini would fire constantly there. action=kill is opt-in, but a bad value still needs a bound.
  - seeds: `c33`
- `s29` — `challenge pass / failure-modes lens: multi-process growers`: the 2026-11-14 grower was one process (41 GB), but node --test, pytest -n auto and vitest fan out to workers; with `min_victim_mib` = 2048, N workers under 2 GB each sum past MemTotal with no eligible victim. Needs a user decision (question).
- `s30` — `challenge pass / security lens: MinIO on spark over HTTP`: MinIO's default listener is plain HTTP on 9000; the precedent for allowed plain HTTP is loopback/LAN with NOSONAR (transport.py:299, pair.py:63).
  - seeds: `c34`
- `s31` — `challenge pass / security lens: object key naming`: probe: hostname = ori-EliteMini-Series; fleet.toml names this machine 'elitemini'.
  - seeds: `c35`
- `s32` — `challenge pass / operations lens: boot-time uploads`: the postmortem is uploaded at start after an unclean boot, which is exactly when the clock may be unsynced (systemd-timesyncd starts at 20:07:20, same second as the daemon's dependencies); SigV4 rejects requests skewed > 15 min.
  - seeds: `c36`
- `s33` — `challenge pass / observability lens: who learns of a kill`: `mem_guard` in /state is read by the fleet reader only through its `THERMAL_FIELDS` allow-list (`fleet_reader.py`:78-98), so it would not appear on the dashboard without fleet-view work, which c21 excludes.
- `s34` — `challenge pass / lifecycle lens: object growth`: event objects are KB-sized; blackbox.jsonl.gz is overwritten per machine and boot (10 min of ~1 KB rows at 1 Hz, ~0.6 MB raw); daily record files are 74 MB raw and are not shipped by this change.
- `s35` — `challenge pass / adjacent-systems lens: credential store`: probe: ~/.aws/credentials is mode 600 with one profile; adding a named profile keeps MinIO keys out of the governor config and the repo.
- `s36` — `challenge pass / migration/reversibility lens: config and records`: clean pass: \[memory\]/\[ship\] are new optional sections (absent = today's behaviour); new record rows are additive and ignored by history.py:118; turning action back to 'record' or removing the section reverts with no state to undo. Residual: a kill itself is irreversible by nature, bounded by c4/budget.
- `s37` — `challenge pass / concurrency lens: recorder writes from three threads`: the Recorder (recorder.py:37-66) is not thread-safe today (one file handle, write+flush+fsync); guard and shipper must hand rows to one writer (queue or lock). Folded into the plan as an implementation constraint, not a spec change.

## Decisions

- Kill scope: own-user processes only, biggest grower; on for the EliteMini, off (record-only) by default elsewhere. (User choice, 2026-11-14.)
- Storage target: bucket 'heat-governor', backend-agnostic over the S3 API (custom endpoint, path-style addressing, region configurable incl. R2's 'auto'), so AWS S3, Cloudflare R2 and a self-hosted store all work through the same code path. First target: a self-hosted store on spark. (User decision 2026-11-14.)
- The self-hosted store on spark is a MinIO container (S3 API, data on spark's disk, port 9000 reachable on the tailnet only). Installing it on spark is a live change made with the user's go-ahead at deploy time. (User decision 2026-11-14.)
- When no single own-uid process is >= `min_victim_mib`, the guard groups own-uid processes by their top-level ancestor below the login session or systemd scope, picks the group whose anonymous memory grew fastest, and kills that group's biggest member each poll until the trigger clears, within the same kill budget. (User decision 2026-11-14.)

## Open parks

- [unknown_nonblocking] Thresholds (floor 5%, horizon 10 s, window 10 s, min victim 2 GB, poll 0.25 s) are untuned; replaying the 2026-11-14 records through the trigger should show when it would have fired.
- [unknown_nonblocking] Spark / GB10 boxes share memory with the GPU and run vLLM near full memory in containers; whether the trigger misfires there (low available is normal) is unmeasured, so the default stays record-only off the EliteMini.
- [unknown_nonblocking] S3 retention: events accumulate per boot (small) and the black box is overwritten; no lifecycle rule or daily archive is set. Decide retention when moving beyond spark (R2 bills storage).
- [follow_up] Branch base: origin/main is 0.16.1 with PR #13 (0.16.2) open; this work branches from origin/main and takes the next minor (0.17.0), rebasing on #13 if it merges first.
- [follow_up] A box should not ship its only black box to itself: spark's own daemon needs a target other than spark (R2 or another box), or its dead-box evidence is lost with it.
- [follow_up] Kill events land in records, the state doc and S3, but nobody is notified; a culture-mesh or dashboard alert on `memory_kill` is a follow-up (the fleet view is out of scope, c21).
- [follow_up] MinIO credentials on spark (root vs a scoped 'heat-governor' user with write access to one bucket) and where they live on the EliteMini (a \[heat-governor\] profile in ~/.aws/credentials, mode 600) are deployment steps for the plan.
- [follow_up] Recorder is single-threaded today (recorder.py:37-66); the guard and shipper add writers, so writes must be serialized. Plan-side constraint.

## Resolved vagueness

- [unknown_nonblocking] The S3 bucket, region and profile are not named yet (user said 'existing bucket, I'll name it'); config and the live probe upload wait on it. — resolved: Bucket 'heat-governor'; any S3-API store (AWS, R2, self-hosted) via \[ship\] endpoint/region; first target self-hosted on spark (user, 2026-11-14).

# record retention

> heat-governor keeps a bounded, configurable window of records on every box: days older than a day are gzip-compressed, days older than the retention window (default 7) are deleted, so no machine's disk fills with telemetry
> instruction: tests/`test_retention.py` over `tmp_path` with a fake clock; never the real ~/.local/state

## Audience

- the operator running the six-box fleet (EliteMini, cultureos, spark, spark2, thor, orin), and every reader of record files: the reboot report, the postmortem, the dashboard's trend history, replay
  - instruction: grep for '\*.jsonl' and '.jsonl' readers in `heat_governor` before merge; list any new one

## Before → After

- Before: record files and governor.log are kept forever: 1.4-3.7 GB of records per box after 16-33 days, growing 87-224 MB/day; cultureos reached 88% of its 55 GB eMMC (6.5 GB free) and needed a manual delete on 2026-11-24
  - instruction: cite docs/evidence when the PR lands
- After: each box holds today's records plus `keep_days` (default 7) complete earlier days — today's and yesterday's raw, older days as .jsonl.gz — plus a size-bounded governor.log; every record reader works on both forms; 'records prune' does the same on demand
  - instruction: live check on the EliteMini first (ls -la records/), then the fleet; record in docs/evidence

## Why it matters

- a governor that is there to keep a box alive must not be the thing that fills its disk; on cultureos the records alone would fill the free eMMC in about a month
  - instruction: recheck with df on arduino before the release note

## Requirements

- the daemon prunes <`data_dir`>/records/YYYYMMDD.jsonl past a configurable retention (proposed default `keep_days` = 7) — today these files are never deleted: recorder.py:47-55 opens one file per UTC day in append mode and nothing removes them (1.4 GB on the EliteMini, 3.0-3.7 GB on the other five boxes, 16-33 files)
  - instruction: unit test with day files on both sides of the boundary, `keep_days` 1, 7 and 30
  - honesty: a day older than `keep_days` (by its YYYYMMDD name, UTC) is deleted whether raw or .gz; a day inside the window is never deleted
- completed day files older than one day are gzip-compressed in place to YYYYMMDD.jsonl.gz with the stdlib gzip module (no new dependency, pyproject dependencies = \[\]); measured on 20261113.jsonl: 74.5 MB -> 2.86 MB at gzip -6 in 0.40 s (26x), gzip -1 4.36 MB in 0.27 s; zstd -3 is no smaller (2.91 MB) and would need a third-party module
  - instruction: test: gunzip(.gz) == original bytes; inject a failure between write and rename and assert the raw file survives
  - honesty: a compressed day decompresses byte-identical to the raw file it replaced, and the raw file is removed only after the .gz is fsync'd and renamed into place
- every reader of record files accepts .jsonl.gz as well as .jsonl: recorder.`iter_records` (report, `sensor_stats`) globs \*.jsonl only (recorder.py:155-157), daemon postmortem globs \*.jsonl (daemon.py:1349), and history.py's remote `HISTORY_SCRIPT` opens YYYYMMDD.jsonl by name and sizes the stride from os.path.getsize (history.py:80-90) — without this, compression silently makes trend history and reboot reports go blank
  - instruction: parametrised tests over the same fixture stored raw and gzipped; history.py checked through `run_local`
  - honesty: `iter_records`, report, `sensor_stats`, the daemon postmortem, history.py's remote script and replay / memory replay give the same output over a directory with gzipped days as over the raw one
- retention is configurable in the \[daemon\] table beside record/fsync/`data_dir` (config.py:98-111 `DAEMON_KEYS`), with strict validation, a commented line in the generated config, and live reload (not added to Daemon.`RESTART_ONLY`, daemon.py:637)
  - instruction: config tests + a daemon reload test asserting the new value is used at the next rollover
  - honesty: `keep_days` and `log_max_mib` are strictly validated, appear commented in the generated config, and a live reload changes them without a restart
- a 'records' CLI noun: 'records prune \[--dry-run\] \[--keep-days N\] \[--json\]' compresses and deletes exactly as the daemon would and prints what it did / would do, plus 'records overview' (rubric `overview_cli_noun_exists`) and explain-catalog entries
  - instruction: CLI tests on `tmp_path`; uv run teken cli doctor . --strict
  - honesty: 'records prune --dry-run' changes nothing on disk and lists exactly the files a real pass changes; the verb follows the CLI contract (stdout/stderr split, --json, exit codes) and teken cli doctor --strict passes
- governor.log rotates when it exceeds a cap (proposed `log_max_mib` = 10 in \[daemon\]): the current file becomes governor.log.1.gz, one rotated generation kept; `_file_logger` (cli/`_commands`/governing.py:231-239) stays append-and-close per line
  - instruction: test with a tiny cap (e.g. 1 KiB via a test hook) on `tmp_path`
  - honesty: governor.log never exceeds `log_max_mib` plus one line; after rotation exactly one governor.log.1.gz exists and holds the previous contents
- pruning never runs on the tick loop (the thermal controller must keep its poll cadence): it runs on its own thread and compresses by streaming zlib in 1 MiB chunks (wbits 31, gzip format), never gzip.compress of a whole file as ship.py:599 does — a whole cultureos day is 224 MB
  - instruction: re-measure on cultureos with the shipped code (time + RSS) and record it in docs/evidence
  - honesty: measured on cultureos 2026-11-24: streaming compress of the 224 MB 20261123.jsonl took 2.9 s CPU at nice 19 with 17 MB RSS; gzip -6 output 9.4 MB
- the daemon's pass and 'records prune' can run at the same time safely: the temp file name is unique per process (`YYYYMMDD.jsonl.gz.PID.tmp`), the final rename is atomic, removing the raw file tolerates it already being gone, and a stray .tmp is removed only when it is older than 1 hour (so one pass never deletes the other's in-progress file)
  - instruction: grep for `_file_logger` and Recorder( before merge
  - honesty: governor.log and records/ each have exactly one writer, the daemon (governing.py:267, daemon.py:191), so the only concurrent actor is the CLI prune verb
- a pass deletes expired days before it compresses anything, and a failed compress (ENOSPC, I/O error) leaves the raw day in place, is logged, and does not stop the rest of the pass — so a nearly full disk still gets space back
  - instruction: covered by the ENOSPC test
  - honesty: deleting needs no free space while compressing needs room for the .gz (~4-9 MB per day); cultureos was at 88% when found
- every pass that changes something writes one '`records_pruned`' event row (files deleted, files compressed, bytes freed, errors) to the records and a line to governor.log; a pass that fails never raises into the daemon
  - instruction: assert the row appears in `iter_records` after a pass
  - honesty: without an event the only trace of a deletion is a missing file; the operator needs to see what retention did and when
- a pass deletes nothing when the wall clock looks wrong: if the newest day file is more than 1 day older than today's UTC date (clock jumped forward), or a day file is dated in the future, the pass logs it and skips deletion
  - instruction: covered by the fake-clock test
  - honesty: a forward clock jump would otherwise delete every record at once; all six boxes have an RTC and NTP sync today and no skewed filename exists, so this is cheap insurance, not an observed fault
- history.py sizes its stride from the uncompressed size of a .gz day (the gzip ISIZE trailer, the last 4 bytes) instead of os.path.getsize, so a 7d request does not parse ~25x more lines than intended and miss its 9-18 s DEADLINE
  - instruction: assert ISIZE == raw size for the largest fixture
  - honesty: day files stay far below 4 GiB (largest seen 224 MB), so the 32-bit ISIZE is exact

## Honesty conditions

- on a tmp data dir with 10 day files and `keep_days` = 7, one prune pass leaves 8 files: today and yesterday raw, six .jsonl.gz (replaces h1)
- prune never compresses or deletes today's or yesterday's file (UTC) for any `keep_days` >= 1, and `keep_days` = 0 or negative is a user error (replaces h5)
- boundaries-audit.jsonl, boundaries-applied.json, adaptive.json, ledger.json, spool/ and ssh/ are untouched after a prune pass
- the spec names every reader of record files found by the scope survey (s4) and nothing in the fleet relies on files older than 7 days
- the before numbers come from the 2026-11-24 read-only survey (s1, s3) and the cultureos cleanup output
- cultureos's 9.1 GB free / ~200 MB per day raw holds as the arithmetic behind 'about a month'
- after the release has run one rollover on a box, records/ matches the after state
- the measured sizes after one week on the fleet meet the stated limits
- a stray .tmp is removed only once it is older than 1 hour, so a concurrent pass's in-progress file survives (replaces h15's unconditional 'the next pass removes it')
- the data dir is owned by the daemon's user, so this guards mistakes (a hand-made symlink, a misconfigured `data_dir`) rather than an attacker

## Success signals

- on a box with `keep_days` = 7, records/ holds <= 8 day files (today + 7), at most 2 uncompressed; on the 100 MB/day boxes records/ stays < 300 MB and on cultureos < 600 MB; governor.log stays under its cap; report, postmortem, history and replay return the same rows on a gzipped day as on the raw file (test)
  - instruction: du -sh records/ and ls -la governor.log\* on all six boxes 7 days after rollout

## Scope / boundaries

- prune never compresses or deletes today's or yesterday's file (UTC), whatever `keep_days` is; `keep_days` < 1 is refused by validation (`keep_days` = 1 keeps exactly today + yesterday)
  - instruction: test around a fake midnight with `keep_days` 1 and 7; config test that `keep_days` = 0 is refused
- boundaries-audit.jsonl is an append-only audit log (`boundaries_service.py`:27, mode 0600) and is not touched by record retention
  - instruction: test: populate a tmp data dir with each, prune, assert unchanged mtimes and contents
- retention only touches files named YYYYMMDD.jsonl / YYYYMMDD.jsonl.gz in <`data_dir`>/records and governor.log(.1.gz); any other file in the data dir is never deleted; compression writes to a temp file and renames, so a crash leaves either the raw day or the finished .gz, never a torn one
  - instruction: covered by c25's interleaved-pass test
- a pass acts only on regular files directly inside <`data_dir`>/records (and governor.log beside it): it never follows a symlink, never recurses, and never touches a path outside that directory
  - instruction: covered by the symlink test

## Non-goals

- the ship spool and the MinIO buckets are not part of this: the spool is already capped (ship.py `max_spool_files` = 5000, deleted after a successful PUT) and the buckets are 580 KB (arduino) and 1.6 MB (spark) because \[ship\] uploads black boxes and events, not the day files
- the user journald is not managed by heat-governor (spark's user journal is 1.6 GB, arduino's 237 MB) — journald has its own SystemMaxUse limits
- no archive or upload of a day before deletion: days older than `keep_days` are gone (this includes the 2026-10-23 records still on spark, spark2, thor and orin once the release lands)

## Assumptions

- a week is not too much once compressed: today + yesterday raw and five gzip days is ~230 MB on the 100 MB/day boxes and ~450 MB on cultureos (177-224 MB/day raw, ~7-9 MB/day gzip); a raw week on cultureos would be ~1.4 GB

## Scope exploration

- `s1` — `heat_governor/recorder.py (Recorder)`: one append-only file per UTC day (recorder.py:47-55), no retention anywhere in the package (grep for prune/retention/rotate finds only memkill's kill-budget window); measured 100-106 MB/day on EliteMini, spark, spark2, orin; 87 MB thor; 177 MB cultureos
  - seeds: `c2`
- `s2` — `compression trial on EliteMini records/20261113.jsonl`: gzip -6 74.5 MB -> 2.86 MB (26x, 0.40 s, ~2 MB RSS); zstd -3 2.91 MB; stdlib gzip already used by ship.py:599 for the black box
  - seeds: `c3`
- `s3` — `fleet disk survey (ssh, read-only, 2026-11-24)`: cultureos (arduino) root eMMC 55G, 47G used, 6.5G free (88%), records 3.0G; spark 86% of 3.7T; spark2 24%; thor 70%; orin 66%; EliteMini /home 77% (50G free)
  - seeds: `c4`
- `s4` — `record readers (recorder.py, daemon.py postmortem, history.py, replay/memory replay CLI)`: four readers assume plain .jsonl: `iter_records` glob, postmortem glob of the newest N files, history.py builds the path by name and estimates lines from compressed-unaware getsize; replay and memory replay take an explicit path (governing.py:513, memory.py:381)
  - seeds: `c5`
- `s5` — `heat_governor/recorder.py tail + ship.py black box`: tail reads directory/YYYYMMDD.jsonl for the days spanning the window (recorder.py:85); ship.py:597 calls it for the black box every cadence
  - seeds: `c6`
- `s6` — `heat_governor/boundaries_service.py AuditLog`: audit log lives in the reader state dir beside records/, 4 KB on the EliteMini; its contract says append-only
  - seeds: `c7`
- `s7` — `ship.py spool + MinIO on arduino and spark`: spool dirs 4-8 KB on all boxes; MinIO data ~/minio/heat-governor/data 580K on arduino and 1.6M on spark
  - seeds: `c8`
- `s8` — `journalctl --user --disk-usage on arduino and spark`: arduino 237.1M, spark 1.6G; the daemon's stderr lands there as well as in governor.log (governing.py:235)
  - seeds: `c9`
- `s9` — `heat_governor/config.py DAEMON_KEYS + daemon.py RESTART_ONLY`: record/fsync/`data_dir` are `DAEMON_KEYS` and restart-only; a retention knob has no reason to be restart-only
  - seeds: `c10`
- `s10` — `governor.log (cli/_commands/governing.py:231-239)`: `_file_logger` appends every action line to <`data_dir`>/governor.log with no rotation; 19,950,790 bytes on spark, 15,020 on arduino, 8 KB on EliteMini
- `s11` — `cultureos cleanup (ssh arduino, 2026-11-24 21:14)`: deleted 14 day files 20261109-20261122 with the user's go-ahead; kept 20261123 (224 MB) and 20261124; root went 6.5G -> 9.1G free (83%)
  - seeds: `c11`
- `s12` — `challenge pass / adjacent-systems lens: mesh_server.py RANGES + docs/integration.md history`: dashboard and /api/history offer 15m|1h|6h|24h|7d; a 7d request spans 8 UTC day files whenever it is not exactly midnight, so retention of 7 files including today under-fills it
- `s13` — `challenge pass / failure-mode lens: probe on cultureos (gzip -6 and streaming zlib of 20261123.jsonl, output to /dev/null)`: 8 cores, 224 MB day -> 9.4 MB in 2.86 s (gzip CLI) / 2.9 s compress + 0.3 s decompress (python zlib streaming, 17 MB RSS); load 1.1 during the probe
  - seeds: `c24`
- `s14` — `challenge pass / concurrency lens: writers of records/ and governor.log`: one Recorder (daemon.py:191) and one `_file_logger` (governing.py:267), both in the daemon; the CLI prune verb (c20) is the second actor; c23's 'stray .tmp the next pass removes' would delete a live pass's temp file without an age gate
  - seeds: `c25`
- `s15` — `challenge pass / recovery lens: disk-full ordering`: spec c3/c23 define compress safety but not pass order or failure handling; on the box that motivated this (cultureos, 88%) the order matters
  - seeds: `c26`
- `s16` — `challenge pass / observability lens: recorder events (MEMORY_EVENTS, recorder.py:162-168)`: the records already carry event rows (start, `memory_kill`, `oom_kill`, `memory_boundary_changed` ...); retention has no event in the spec, so deletions would be invisible
  - seeds: `c27`
- `s17` — `challenge pass / failure-mode lens: clock skew probe (ls records/ + timedatectl on arduino, spark, spark2, thor, orin; EliteMini local)`: no filename outside 202610xx/202611xx on any box; all report CanNTP=yes NTPSynchronized=yes and /dev/rtc present; Recorder names files by each row's own t (recorder.py:58), so a skewed clock would write skewed filenames
  - seeds: `c28`
- `s18` — `challenge pass / security lens: delete path`: c23 limits by filename pattern only; a symlink named YYYYMMDD.jsonl would be followed by a naive unlink-after-compress or open()
  - seeds: `c29`
- `s19` — `challenge pass / adjacent-systems lens: history.py stride (lines 86-91)`: stride = size / avg line length from the first 64 KiB; with a .gz day size is ~4% of the real bytes and the 64 KiB head is compressed bytes, both wrong; this sharpens parked v1
  - seeds: `c30`
- `s20` — `challenge pass / adjacent-systems lens: ship.py black box (recorder.tail, ship.py:597)`: clean: the black box reads only the last minutes via tail over today's and yesterday's raw files, which c6 keeps raw; shipped kill events and postmortems are independent of local retention
- `s21` — `challenge pass / operations lens: governor.log rotation (governing.py:231-239)`: clean: `_file_logger` opens, appends one line and closes per call, so renaming the file between calls is safe and the next line creates a fresh governor.log; the daemon's stderr also goes to the user journal, which stays out of scope (c9)
- `s22` — `challenge pass / docs lens: explain catalog + harness files`: explain/catalog.py:355 still shows ~/.local/share/heat-governor/records (the real default is ~/.local/state, state.py:32-34); CLAUDE.md state table, AGENTS.override.md, AGENTS.colleague.md, QWEN.md and docs/integration.md's history paragraph need the retention facts — plan-side tasks, not spec claims
- `s23` — `challenge pass / not examined`: not examined: the dashboard's 7d query time over gzipped days on the slower boxes (parked v1, needs the code), and behaviour under a full disk while the daemon's own Recorder.write fails (pre-existing, outside retention)

## Decisions

- cultureos one-off cleanup: keep the last 2 day files (yesterday + today) for testing, delete the older ones now
- default retention `keep_days` = 7
- governor.log gets size-bounded rotation as part of this work
- retention runs in two places: inside the daemon at the UTC day rollover, and as a CLI verb for on-demand / one-off cleanup
- `keep_days` counts complete UTC days before today: `keep_days` = 7 keeps today's file plus the 7 previous day files (8 files), so the dashboard's 7d range (`mesh_server.py`:69) is always fully covered

## Open parks

- [unknown_nonblocking] fleet-reader trend history over long spans reads compressed days: history.py's stride estimate uses compressed getsize and its 9-18 s DEADLINE may be hit decompressing gzip on the slower boxes — unmeasured
- [unknown_nonblocking] first rollout is a one-time burst: on spark, spark2, thor and orin the first pass deletes ~25 day files and compresses 5 (~15 s of CPU on the slower boxes, off the tick loop); expected, but worth watching once
- [unknown_nonblocking] a downgrade after compression: an older heat-governor reads only raw .jsonl days, so report / postmortem / history skip the gzipped ones until it is upgraded again; no data is lost
- [unknown_nonblocking] a row whose own t is older than yesterday (recorder.py:47-58 picks the file by the row's t) would recreate a raw YYYYMMDD.jsonl beside its .gz; readers would see that day split in two files. No current writer backdates rows (daemon and memguard stamp the current clock), so this only bites a future writer
- [follow_up] records disk usage per machine in mesh status / the dashboard, so retention is visible fleet-wide without ssh

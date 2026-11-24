# record retention

> heat-governor keeps a bounded, configurable window of records on every box: days older than a day are gzip-compressed, days older than the retention window (default 7) are deleted, so no machine's disk fills with telemetry
> instruction: tests/`test_retention.py` over `tmp_path` with a fake clock; never the real ~/.local/state

## Audience

- the operator running the six-box fleet (EliteMini, cultureos, spark, spark2, thor, orin), and every reader of record files: the reboot report, the postmortem, the dashboard's trend history, replay
  - instruction: grep for '\*.jsonl' and '.jsonl' readers in `heat_governor` before merge; list any new one

## Before → After

- Before: record files and governor.log are kept forever: 1.4-3.7 GB of records per box after 16-33 days, growing 87-224 MB/day; cultureos reached 88% of its 55 GB eMMC (6.5 GB free) and needed a manual delete on 2026-11-24
  - instruction: cite docs/evidence when the PR lands
- After: each box holds at most `keep_days` (default 7) of records — today's and yesterday's raw, older days as .jsonl.gz — plus a size-bounded governor.log; every record reader works on both forms; 'records prune' does the same on demand
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

## Honesty conditions

- on a tmp data dir with 10 day files and `keep_days` = 7, one prune pass leaves 7 files: the two newest raw, five .jsonl.gz
- prune never compresses or deletes today's or yesterday's file (UTC), whatever `keep_days` is (`keep_days` < 2 is refused by validation)
- boundaries-audit.jsonl, boundaries-applied.json, adaptive.json, ledger.json, spool/ and ssh/ are untouched after a prune pass
- the spec names every reader of record files found by the scope survey (s4) and nothing in the fleet relies on files older than 7 days
- the before numbers come from the 2026-11-24 read-only survey (s1, s3) and the cultureos cleanup output
- cultureos's 9.1 GB free / ~200 MB per day raw holds as the arithmetic behind 'about a month'
- after the release has run one rollover on a box, records/ matches the after state
- the measured sizes after one week on the fleet meet the stated limits
- a crash at any point of compress leaves either the raw day or a complete .gz (plus possibly a stray .tmp that the next pass removes); files outside the named patterns survive

## Success signals

- on a box with `keep_days` = 7, records/ holds <= 7 day files, at most 2 uncompressed; on the 100 MB/day boxes records/ stays < 300 MB and on cultureos < 600 MB; governor.log stays under its cap; report, postmortem, history and replay return the same rows on a gzipped day as on the raw file (test)
  - instruction: du -sh records/ and ls -la governor.log\* on all six boxes 7 days after rollout

## Scope / boundaries

- the open (today's) file and yesterday's are never compressed or deleted: Recorder holds today's file open for append, and recorder.tail (recorder.py:75-90, the shipper's black box) reads today and yesterday across midnight
  - instruction: test around a fake midnight; config test that `keep_days` = 1 is a user error
- boundaries-audit.jsonl is an append-only audit log (`boundaries_service.py`:27, mode 0600) and is not touched by record retention
  - instruction: test: populate a tmp data dir with each, prune, assert unchanged mtimes and contents
- retention only touches files named YYYYMMDD.jsonl / YYYYMMDD.jsonl.gz in <`data_dir`>/records and governor.log(.1.gz); any other file in the data dir is never deleted; compression writes to a temp file and renames, so a crash leaves either the raw day or the finished .gz, never a torn one
  - instruction: test that kills the step between write, fsync and rename; assert a following pass cleans up

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
- `s7` — `ship.py spool + MinIO on arduino and spark`: spool dirs 4-8 KB on all boxes; MinIO data /home/arduino/minio/heat-governor/data 580K, /home/spark/minio/heat-governor/data 1.6M
  - seeds: `c8`
- `s8` — `journalctl --user --disk-usage on arduino and spark`: arduino 237.1M, spark 1.6G; the daemon's stderr lands there as well as in governor.log (governing.py:235)
  - seeds: `c9`
- `s9` — `heat_governor/config.py DAEMON_KEYS + daemon.py RESTART_ONLY`: record/fsync/`data_dir` are `DAEMON_KEYS` and restart-only; a retention knob has no reason to be restart-only
  - seeds: `c10`
- `s10` — `governor.log (cli/_commands/governing.py:231-239)`: `_file_logger` appends every action line to <`data_dir`>/governor.log with no rotation; 19,950,790 bytes on spark, 15,020 on arduino, 8 KB on EliteMini
- `s11` — `cultureos cleanup (ssh arduino, 2026-11-24 21:14)`: deleted 14 day files 20261109-20261122 with the user's go-ahead; kept 20261123 (224 MB) and 20261124; root went 6.5G -> 9.1G free (83%)
  - seeds: `c11`

## Decisions

- cultureos one-off cleanup: keep the last 2 day files (yesterday + today) for testing, delete the older ones now
- default retention `keep_days` = 7
- governor.log gets size-bounded rotation as part of this work
- retention runs in two places: inside the daemon at the UTC day rollover, and as a CLI verb for on-demand / one-off cleanup

## Open parks

- [unknown_nonblocking] fleet-reader trend history over long spans reads compressed days: history.py's stride estimate uses compressed getsize and its 9-18 s DEADLINE may be hit decompressing gzip on the slower boxes — unmeasured

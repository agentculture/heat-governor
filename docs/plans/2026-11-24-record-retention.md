# Build Plan — record retention

slug: `record-retention` · status: `exported` · from frame: `record-retention`

> heat-governor keeps a bounded, configurable window of records on every box: days older than a day are gzip-compressed, days older than the retention window (default 7) are deleted, so no machine's disk fills with telemetry

## Tasks

### t1 — records readers accept .jsonl.gz in recorder.py (`iter_records`, report, `sensor_stats`, replay)

- instruction: Add a small `open_day`(path) helper in `heat_governor`/recorder.py that opens .jsonl or .jsonl.gz as text (gzip.open(..., 'rt')); glob both suffixes in `iter_records` and sort by the date stem; replay(path) uses `open_day`. Touch only recorder.py and its tests. Do not change tail()/`_rows_between` (they read today/yesterday raw).
- covers: c5, h4
- acceptance:
  - tests: a fixture stored raw and as .jsonl.gz gives identical `iter_records` rows, report() entries, `sensor_stats`() and replay() results
  - `iter_records` orders days by YYYYMMDD whatever the suffix; tail() and Recorder writing are unchanged (existing tests pass)

### t2 — history.py reads gzipped days and sizes its stride from the gzip ISIZE trailer

- instruction: `HISTORY_SCRIPT` is a self-contained string run remotely with python3: keep it stdlib-only and inside the template. For each day look for YYYYMMDD.jsonl then YYYYMMDD.jsonl.gz; open the .gz with gzip.open; for size use the last 4 bytes (little-endian ISIZE) and estimate avg line length from the first 64 KiB of decompressed text. Touch only `heat_governor`/history.py and its tests.
- covers: c30, h22, c5
- acceptance:
  - tests via `run_local` over tmp `XDG_STATE_HOME`: history samples for a raw day and the same day gzipped are identical
  - the stride computed for a .jsonl.gz day equals the stride for the raw day; ISIZE equals the raw size for the largest fixture

### t3 — retention core: `heat_governor`/retention.py `prune_pass` and `compress_day`

- instruction: New module, stdlib only. `prune_pass`(`records_dir`, now, `keep_days`, \*, `dry_run`=False, compress=`compress_day`) -> dict. Select only regular files (lstat, not symlinks) directly in `records_dir` matching ^\d{8}\.jsonl(\.gz)?$. Order: clock sanity check, delete expired, remove .tmp older than 1 h, then compress days older than yesterday. `compress_day` streams with zlib.compressobj(6, DEFLATED, 31) in 1 MiB chunks to `YYYYMMDD.jsonl.gz.PID.tmp` (PID = os.getpid()), fsync, os.replace, then unlink the raw file tolerating FileNotFoundError. Never touch the real ~/.local/state in tests.
- covers: c1, h24, c2, h2, c3, h3, c6, h25, c7, h6, c23, h23, c25, h17, c26, h18, c28, h20, c29, h21, c24
- acceptance:
  - tests/`test_retention.py` on `tmp_path` with a fake clock: 10 day files and `keep_days` 7 leave 8 files, today and yesterday raw, six .jsonl.gz
  - every behaviour in obligations o1, o2, o4, o5, o9, o11, o12, o14, o15 and the streaming part of o10 has a test that passes
  - `prune_pass`(`dry_run`=True) returns the same planned actions and changes no file; the result is a dict with deleted, compressed, `bytes_freed`, errors, warnings

### t4 — config: `keep_days` and `log_max_mib` in \[daemon\]

- instruction: Add both keys to `DAEMON_KEYS` and the daemon settings dataclass in `heat_governor`/config.py with validation beside record/fsync; add the commented lines to the generated config. Touch only config.py and config tests; the daemon wiring is a later task.
- covers: c10, h7, c6, h25
- acceptance:
  - tests: `keep_days` defaults to 7, accepts ints >= 1, refuses 0, negatives, bools and floats with ConfigError; `log_max_mib` defaults to 10 and refuses <= 0
  - the generated config text has a commented `keep_days` and `log_max_mib` line in the \[daemon\] table; neither key is in Daemon.`RESTART_ONLY`

### t5 — memory replay accepts a .jsonl.gz records file

- instruction: Use recorder.`open_day` for the records paths in `heat_governor`/cli/`_commands`/memory.py (lines ~146-189); CSV input stays plain. Touch only memory.py and its tests.
- depends on: t1
- covers: c5
- acceptance:
  - tests: memory replay over a records fixture and the same fixture gzipped prints identical results

### t6 — governor.log rotation at `log_max_mib`

- instruction: In `heat_governor`/cli/`_commands`/governing.py keep `_file_logger` append-and-close per line; before appending, if size >= cap, stream-gzip the file to governor.log.1.gz.tmp, os.replace onto governor.log.1.gz, then unlink governor.log. Pass the cap from cfg.daemon.`log_max_mib`. Touch only governing.py (logger part) and its tests.
- depends on: t4
- covers: c21, h14
- acceptance:
  - tests on `tmp_path` with a tiny cap: the log never exceeds cap plus one line; the previous contents are in governor.log.1.gz; a second rotation replaces it
  - a rotation failure is written to stderr and logging continues

### t7 — daemon: retention thread at UTC rollover, `records_pruned` event, gz-aware postmortem, live reload

- instruction: In `heat_governor`/daemon.py: start a daemon thread that calls retention.`prune_pass`(self.recorder.directory, time.time(), cfg.daemon.`keep_days`) at start (after the postmortem scan) and when the UTC date changes; write its result through the existing recorder path on the tick thread side as {event: `records_pruned`, ...}. Extend the postmortem glob (daemon.py:1349) to both suffixes via recorder.`open_day`. Skip when record is off. Tests use tmp data dirs and fake clocks.
- depends on: t1, t3, t4
- covers: c24, h16, c27, h19, c10, h7, c1
- acceptance:
  - tests: a pass runs once at daemon start and once per UTC day rollover on its own thread; ticks keep their cadence while a slow fake compressor runs
  - a pass that changed something writes one `records_pruned` row returned by `iter_records` and one log line; an exception inside the pass is logged and the daemon keeps ticking
  - the postmortem reads the newest day files whether raw or gzipped; a reload that changes `keep_days` is used by the next pass without a restart

### t8 — records CLI noun: overview and prune

- instruction: New `heat_governor`/cli/`_commands`/records.py with register(sub) (`parser_class`=type(p)); wire it in `_build_parser`(); `keep_days` defaults from the loaded config, else 7. Add ENTRIES in explain/catalog.py and fix the stale ~/.local/share records path at catalog.py:355 to ~/.local/state. Touch records.py, cli/`__init__.py`, explain/catalog.py, learn.py and tests.
- depends on: t3, t4
- covers: c20, h13
- acceptance:
  - tests/`test_cli_records.py`: prune --dry-run changes no file and lists exactly what a real prune then does; --json output matches `prune_pass`'s dict; exit 1 on a bad --keep-days, 2 when the records dir is unreadable
  - uv run teken cli doctor . --strict passes; explain resolves records, records overview, records prune; learn mentions the records noun

### t9 — docs and harness files carry the retention facts; version bump

- instruction: Keep docs grounded in what is on disk; mark the live check (planned). Run uv run python scripts/harness-smoke.py --stage config.
- depends on: t5, t6, t7, t8
- covers: c15, h8
- acceptance:
  - a grep for '.jsonl' readers in `heat_governor` finds none that ignore .jsonl.gz (list each reader and its test in the PR description)
  - CLAUDE.md state table, AGENTS.override.md, AGENTS.colleague.md, QWEN.md, docs/integration.md history paragraph and the config docs describe `keep_days`, `log_max_mib`, gzip days and records prune, marked unvalidated live; markdownlint and harness-smoke pass
  - pyproject.toml and CHANGELOG.md bumped (minor)

### t10 — live check: EliteMini first, then the fleet, recorded in docs/evidence

- instruction: Deleting records on a live box needs the user's go-ahead for each box; spark, spark2, thor and orin lose their 2026-10-23 days on the first pass (accepted, c22). Use the fleet ssh hosts from fleet.toml.
- depends on: t9
- covers: c18, h11, c19, h12, c16, h9, c17, h10, h16
- acceptance:
  - with the user's go-ahead per box: records prune --dry-run then the release on the EliteMini; ls -la records/ shows today and yesterday raw and older days .gz
  - cultureos: time and RSS of one real compress measured with the shipped code; df on arduino recorded
  - 7 days after rollout on all six boxes: <= 8 day files, <= 2 raw, records/ < 300 MB (cultureos < 600 MB), governor.log under its cap; written to `docs/evidence/YYYY-MM-DD-record-retention-live.md`

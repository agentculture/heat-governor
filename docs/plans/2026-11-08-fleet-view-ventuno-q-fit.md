# Build Plan — fleet view + VENTUNO Q fit

slug: `fleet-view-ventuno-q-fit` · status: `exported` · from frame: `fleet-view-ventuno-q-fit`

> heat-governor runs natively on the VENTUNO Q, governs all five mesh boxes (cultureos, spark, spark2, thor, orin), and one React dashboard plus an MCP server and CLI on cultureos show every box live

## Tasks

### t1 — VENTUNO Q fake sysfs fixture in tests/fakes.py

- instruction: Extend `make_sys` with keyword-only arguments; default None keeps today's behaviour. Copy trip values from docs/specs/2026-11-08-fleet-view-ventuno-q-fit.md scope entry s2. No test may touch the real /sys.
- covers: c6, h6
- acceptance:
  - tests/fakes.py `make_sys` accepts compatible=, devfreq= and ina232= and writes proc/device-tree/compatible (NUL-separated), class/devfreq/3d00000.gpu/{`cur_freq`,`max_freq`,`available_frequencies`} and an hwmon named ina232 with `power1_input`, all under the given `tmp_path` root
  - a `VENTUNOQ_ZONES` constant mirrors the 2026-11-08 trips (cpu-\* passive 126/130/135, gpuss-\*/nsp-\* 115/130/135, ddrss 130/135, pmm86\*-therm passive 105 critical 125)
  - existing `SPARK_ZONES`/`THOR_ZONES`/`ORIN_ZONES` callers are unchanged and the full suite still passes

### t2 — detect() reads device-tree compatible and resolves a built-in ventunoq profile

- instruction: Read proc/device-tree/compatible after model, split on NUL, match exact tokens; check it before the nvidia-smi probe. Add VENTUNOQ to BUILTIN and update the module docstring and resolve() error text. Tests go in tests/`test_platform_ventunoq.py`.
- depends on: t1
- covers: c2, h2, c3, h3, c24, h20
- acceptance:
  - detect() returns 'ventunoq' for a tree whose compatible holds the exact token arduino,monza or qcom,qcs8300, and still returns thor/orin for the real Thor (nvidia,p4071-0000+p3834-0008 nvidia,tegra264) and Orin (nvidia,p3737-0000+p3701-0005 nvidia,tegra234) strings and spark when nvidia-smi reports GB10
  - the ventunoq profile watches only cpu, cpuss, gpuss, nsp and ddrss zone families, its crit limit is below 115 C, and its provenance says declared from trip points, not measured
  - an arduino,imola (UNO Q) tree resolves to generic

### t3 — sense reads the Adreno GPU via devfreq and board power via ina232; fix fan ownership text

- instruction: Add a gpu mode for the profile (e.g. 'devfreq') and accept it in config.py's profile.gpu validator. Read power only when an hwmon named ina232 exists. Tests in tests/`test_sense_ventunoq.py`.
- depends on: t1, t2
- covers: c4, h4
- acceptance:
  - on a VENTUNO Q fake tree sense --json reports the GPU clock in MHz from class/devfreq/3d00000.gpu and board power in watts from ina232 `power1_input`
  - capabilities() reports the emc2305 fan as kernel-owned (a thermal cooling device), not 'firmware/nvfancontrol owned', when the fan is a cooling device
  - Spark, Thor and Orin sense output is byte-identical to before for their fixtures

### t4 — gpu-clock refuses unsupported platforms before touching the root helper

- instruction: Gate on the resolved profile name or profile.gpu before probe(); keep the error in the CliError format. Tests in tests/`test_gpuclock_platform.py`.
- depends on: t2
- covers: c5, h5
- acceptance:
  - heat-governor gpu-clock lock and unlock on a ventunoq profile exit 1 with an unsupported-platform error and a hint, and the helper subprocess is never spawned (asserted with a fake runner)
  - Spark, Thor and Orin gpu-clock behaviour is unchanged

### t5 — fleet.toml loader with strict host validation

- instruction: New module `heat_governor`/fleet.py, stdlib tomllib. Do not touch config.py's `TOP_KEYS` (decision c34). Ship a commented example in the docs, not a committed JSON. Tests in tests/`test_fleet_config.py`.
- covers: c16, h14, c25, h21
- acceptance:
  - a fleet.toml under the config dir lists machines (name, host, transport ssh or local) and loads into frozen dataclasses with strict unknown-key errors
  - a host entry such as '-oProxyCommand=touch /tmp/x', one with spaces or shell metacharacters, or an empty host fails load with exit 1 and a hint
  - no file under `heat_governor`/ contains spark, spark2, thor or orin as a hostname, and scripts/scan-secrets.py passes

### t6 — SSH transport with reader-managed persistent connections

- instruction: subprocess.run with an argv list, never shell=True; annotate the bandit/Sonar exceptions the way pair.py does. Backtick-free: no user text reaches the remote command. Tests in tests/`test_transport.py` with a fake ssh executable in `tmp_path`.
- depends on: t5
- covers: c26, h22
- acceptance:
  - the transport runs ssh with BatchMode=yes, ConnectTimeout, ServerAliveInterval, ControlMaster=auto, ControlPersist and a ControlPath under the reader's state dir, then '--', the validated host and the fixed remote command curl -s -m <timeout> 127.0.0.1:9476/state
  - a host that hangs is reported unreachable after the per-poll timeout and never blocks the caller beyond it (tested with a fake ssh script on PATH)
  - closing the transport tears down its control sockets

### t7 — fleet reader: concurrent polling, staleness, compact 24 h history, reader health

- instruction: Reuse pair.PeerLink's staleness approach (reader clock, `MAX_PEER_STALE_S` cap) rather than client.read. Local machine uses the state file or 127.0.0.1. Tests in tests/`test_fleet_reader.py` with fake transports.
- depends on: t5, t6
- covers: c8, h8, c9, h9, c28, h24, c29, h25
- acceptance:
  - one polling thread per machine; a stopped or hanging box is marked stale then unreachable on the reader's own clock without delaying the others, and admit is never reported for an unreachable box
  - history keeps compact samples (time, hottest temp, GPU temp, level, admit factor) bounded to 24 h per box; a simulated week of polling for 19 machines stays under 100 MB
  - the snapshot carries reader health (per-box last poll time, last transport error, reader uptime) separately from thermal state
  - the reader imports nothing from daemon.py and never writes to any remote box

### t8 — mesh serve HTTP server: same-origin page, JSON API, 127.0.0.1 by default

- instruction: Stdlib ThreadingHTTPServer as in state.py. Serve files via importlib.resources from `heat_governor`/web/. Tests in tests/`test_mesh_server.py`.
- depends on: t7
- covers: c10, h10
- acceptance:
  - GET / serves the dashboard from package data; GET /api/fleet and GET /api/history?machine=&range= serve JSON from the reader; everything else 404
  - it binds 127.0.0.1 by default and only an explicit --listen changes it (decision c32); no daemon state server gains CORS headers
  - the page and API share one origin; tests fetch them with urllib against an ephemeral port

### t9 — dashboard page built to the locked design, Polar night only, vendored assets

- instruction: Start from docs/design/fleet-dashboard.html; delete the other skins, the Survey/Decisions tabs, the example fleets and the generated history; keep the 'Example history' label until real history backs the chart (o6). Re-run the dataviz validator for the two Polar night modes.
- depends on: t8
- covers: c11, h11, c22, h18
- acceptance:
  - `heat_governor`/web/index.html reproduces docs/design/fleet-dashboard.html's Machines view with Polar night only (no theme picker) and reads /api/fleet and /api/history instead of embedded data
  - React 18.3.1, htm 3.1.1 and Lato are vendored under `heat_governor`/web/vendor/ with their licence files, the page makes no request to any other host, and the built wheel contains them
  - obligations o1-o6, o8 and o9 hold: fleet map on top, filters, single band label, aura only on critical moving, trend panel with compare, not-supported and unreachable states
  - pyproject.toml still declares dependencies = \[\]

### t10 — mesh noun: overview, status, serve; status queries mesh serve and falls back to a direct read

- instruction: Follow `_commands`/cli.py's noun pattern with `parser_class`=type(p). Tests in tests/`test_cli_mesh.py`.
- depends on: t7, t8
- covers: c13, h12, c19, h15, c21, h17, c23, h19
- acceptance:
  - heat-governor mesh overview|status|serve exist, each takes --json, results on stdout and errors on stderr with exit codes 0/1/2
  - mesh status reads the running mesh serve's /api/fleet and, when it is not running, does a one-shot direct read through the same reader code; both paths report each machine's age in seconds
  - explain catalog entries, learn text and overview verbs list the new paths; `test_every_catalog_path_resolves` and uv run teken cli doctor . --strict pass

### t11 — heat-governor mcp: read-only MCP server over stdio

- instruction: Hand-roll JSON-RPC 2.0 with stdlib json; keep the loop thin around a testable dispatch function so coverage holds. Register the verb and its catalog entry after t10's changes. Tests in tests/`test_mcp.py`.
- depends on: t10
- covers: c14, h13
- acceptance:
  - initialize, tools/list and tools/call work over newline-delimited JSON-RPC on stdio; tools are `mesh_status`, `machine_state` and sense, all read-only
  - stdout carries only JSON-RPC frames (asserted by feeding a scripted session); diagnostics go to stderr; mcp --help returns and mcp accepts --json
  - tools query mesh serve when it runs and fall back to a direct read otherwise

### t12 — docs, harness mirrors and version bump

- instruction: Use the version-bump skill. Keep the lobes-cli attribution only in .pi/SYSTEM.md.
- depends on: t3, t4, t9, t11
- acceptance:
  - README CLI table, docs/integration.md, CLAUDE.md current-state table, AGENTS.override.md, AGENTS.colleague.md and QWEN.md describe the ventunoq profile, mesh status/serve, mcp and fleet.toml; nothing ahead of the code is described without (planned)
  - pyproject.toml version and CHANGELOG.md bumped; markdownlint and scripts/harness-smoke.py --stage config pass

### t13 — install on cultureos in dry-run, run mesh serve, verify all five machines

- instruction: LIVE MACHINE CHANGE: get the user's explicit go-ahead before installing anything. Write the baseline evidence first, then install with uv tool and a user unit like the other four boxes (apply = false). No change on spark, spark2, thor or orin.
- depends on: t2, t3, t10, t11
- covers: c1, h1, c7, h7, c20, h16
- acceptance:
  - docs/evidence/2026-11-08-ventunoq-baseline.md records the before state (generic profile 102/106/110, 48 zones, curl to each box's :9476 failing from cultureos)
  - on cultureos systemctl --user is-active heat-governor prints active and heat-governor state reports applying false
  - mesh serve runs as a user unit on 127.0.0.1, and heat-governor mesh status --json lists 5 of 5 machines with readings no older than 10 s

## Risks

- [unknown_nonblocking] teken cli doctor --strict may invoke verbs beyond --help; a long-running stdio verb (mcp) could hang the rubric — unverified in the scope survey (s13) (task t11)
- [follow_up] VENTUNO Q limits are declared, not measured; dry-run on cultureos stays until a CPU+GPU+NPU load run tunes them (frame park v1) (task t13)
- [out_of_scope] the upgrade release (token listener + /history route, frame park v4) and the buffer persistence question (v5) are out of this plan

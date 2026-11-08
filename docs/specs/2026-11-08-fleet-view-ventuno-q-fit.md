# fleet view + VENTUNO Q fit

> heat-governor runs natively on the VENTUNO Q, governs all five mesh boxes (cultureos, spark, spark2, thor, orin), and one React dashboard plus an MCP server and CLI on cultureos show every box live

## Audience

- the operator who runs the five-box mesh from cultureos, plus the agents and tools on cultureos that read fleet thermal state through the CLI or MCP (Claude Code sessions, lobes, sparkrun)

## Before → After

- Before: today cultureos (VENTUNO Q) runs no governor and would fall to the generic profile (102/106/110 °C over all 48 zones, hottest zone a memory PMIC); the four mesh daemons answer only on their own 127.0.0.1:9476, so checking the fleet means ssh-ing into each box
- After: from cultureos, one command (heat-governor mesh status), one MCP server (heat-governor mcp) and one web page (heat-governor mesh serve, which is the fleet reader the other two query) show every machine's live temperature, level, admission and trends, and the VENTUNO Q is detected and watched with its own profile in dry-run

## Why it matters

- the Sparks reach thermal resets under heavy AI work (spark read critical at 89.1 °C during this scope survey); seeing every box at once, with the hot ones first, is how the operator keeps that work running without resets

## Requirements

- platforms.detect() recognises the VENTUNO Q from proc/device-tree/compatible (arduino,monza / qcom,qcs8300) and resolves a built-in 'ventunoq' profile; today detect() reads only device-tree/model ('Qualcomm Technologies, Inc. Monaco Monza addons', platforms.py:158-181) so this box falls to 'generic'
  - honesty: detect() returns 'ventunoq' for a `tmp_path` tree whose proc/device-tree/compatible contains arduino,monza, and the existing Spark, Thor and Orin fixtures still resolve unchanged
- a built-in 'ventunoq' profile watches cpu-\*, cpuss-\*, gpuss-\*, nsp-\* (NPU) and ddrss-\* zones only, with declared (unmeasured) limits derived from the live passive trips (CPU 126 C, GPU/NPU 115 C); today generic watches all 48 zones, picks `pmm8650au_1_sdram` as hottest and sets warm/hot/crit 102/106/110 from the PMIC 125 C critical trip, so it would essentially never act
  - honesty: the ventunoq profile's crit limit sits below the 115 °C GPU/NPU passive trip, and its provenance text says the limits are declared from trip points, not measured
- sense reads the Adreno GPU via devfreq 3d00000.gpu (steps 479/599/780/877 MHz, no 'load' file) and board power from hwmon ina232 `power1_input` (12.96 W idle); capabilities stops calling the emc2305 fan 'firmware/nvfancontrol owned' (sense.py:470) because here it is kernel `cooling_device7` (`emc2305_fan`, state 5/10)
  - honesty: on cultureos, sense --json reports the GPU clock from devfreq 3d00000.gpu and board power in watts from ina232, and capabilities no longer calls the emc2305 fan firmware-owned
- tests fake a VENTUNO Q tree: tests/fakes.py `make_sys` gains compatible=, devfreq and ina232 hwmon support plus a `VENTUNOQ_ZONES` fixture, under `tmp_path`, never the real /sys
  - honesty: no test reads the real /sys: every VENTUNO Q test builds its tree under `tmp_path` through tests/fakes.py
- heat-governor is installed on cultureos (uv tool + user unit + config like the other four) so the VENTUNO Q is the fifth governed, observable box, running in dry-run (apply = false) until the declared VENTUNO Q limits are checked under load; today 'which heat-governor' is empty and the unit does not exist
  - honesty: on cultureos, systemctl --user is-active heat-governor prints active, and heat-governor state reports applying false
- a fleet reader on cultureos polls all five boxes server-side: the four remote daemons through a persistent SSH connection per box (`ssh <box> curl 127.0.0.1:9476/state`, no change on any box) and cultureos locally; it tracks per-box ok/stale/unreachable on the reader's own clock, reusing pair.PeerLink's staleness logic, keeps a rolling buffer of the polled documents for trends, and never uses client.read's admit-on-unknown default
  - honesty: stopping one remote daemon makes the reader mark that box stale and then unreachable within its stale window, without blocking the other boxes; no remote config file or listen address changes
- the dashboard is served same-origin by heat-governor on cultureos (page + aggregated JSON from one process), so no daemon needs CORS headers; StateServer sends only Content-Type/Content-Length (state.py:72-77)
  - honesty: the dashboard page fetches only relative URLs from mesh serve, and no daemon's state server gains CORS headers
- the dashboard is one prebuilt static HTML file shipped as package data (hatch ships non-py files under `heat_governor`/), with React 18.3.1, htm 3.1.1 and Lato vendored alongside it so it renders offline and no node build step, built to the locked design in docs/design/fleet-dashboard.html with the Polar night theme only (cream light, slate dark)
  - honesty: the built wheel contains the dashboard HTML, mesh serve serves it from package data, and pyproject.toml still declares dependencies = \[\]
- a 'mesh' noun (overview, status \[--json\], serve) and a top-level 'mcp' verb are added through register(sub) in `_build_parser` with `parser_class`=type(p), ENTRIES in explain/catalog.py, the learn `_TEXT`/`_as_json_payload` lists and overview `_VERBS`, so teken cli doctor --strict and `test_every_catalog_path_resolves` stay green
  - honesty: uv run teken cli doctor . --strict passes and `test_every_catalog_path_resolves` covers mesh overview, mesh status, mesh serve and mcp
- 'heat-governor mcp' speaks MCP over stdio with read-only tools (mesh status, one box's state, sense) and owns stdout for JSON-RPC framing: all logging goes to stderr via `emit_diagnostic`; it still accepts --json and 'mcp --help' returns
  - honesty: an MCP client that adds 'heat-governor mcp' lists only read-only tools, a mesh status tool call returns all five machines, and stdout carries nothing but JSON-RPC frames
- the fleet reader validates every host in the fleet list as a plain hostname or IP (no leading '-', no spaces or shell metacharacters), passes '--' before the host and runs a fixed remote command, so a host entry can never become an ssh option such as -oProxyCommand
  - honesty: a fleet entry '-oProxyCommand=touch /tmp/x' is rejected at config load with exit 1 and ssh is never spawned for it
- the reader manages its own persistent SSH connections (ControlMaster/ControlPersist with a ControlPath under its state dir), with BatchMode=yes, ConnectTimeout, ServerAliveInterval and a per-poll timeout, independent of the operator's ~/.ssh/config; today ~/.ssh/config sets only IdentityFile and a fresh poll measured 0.55 s
  - honesty: with ControlMaster up, a spark poll completes in well under the measured 0.55 s fresh-handshake time, and a box that stops answering is marked unreachable after the per-poll timeout rather than hanging the reader
- the reader's history buffer stores compact samples (time, hottest temperature, GPU temperature, level, admit factor) bounded to 24 h per box, not raw /state documents; probe: /state is 2-3 KB, so raw docs at a 2 s poll would be about 130 MB per box per day
  - honesty: with 19 machines polled for 24 h the reader's resident memory stays under 100 MB
- mesh status, the MCP tools and the page report the reader's own health (when each box was last polled, reader uptime, transport errors) separately from each box's thermal state, so a dead reader never looks like five stale boxes
  - honesty: stopping the reader's ssh for every box shows a reader or transport error, not five independent 'stale' machines

## Honesty conditions

- all five machines (cultureos, spark, spark2, thor, orin) appear in heat-governor mesh status --json and on the mesh serve page, each with a fresh reading or an explicit stale, unreachable or not-supported state
- heat-governor gpu-clock lock on a ventunoq box exits 1 with an unsupported-platform error and never invokes the root helper
- the state schema stays 1 with only optional fields added, and a daemon's tick interval is unchanged while the fleet reader is down or slow
- python3 scripts/scan-secrets.py passes and no file under `heat_governor`/ contains the names spark, spark2, thor or orin as hostnames
- the CLI, MCP and page outputs are all usable without SSH access to the remote boxes, so an agent on cultureos needs no per-box credentials
- the before state matches the 2026-11-08 survey: sense --json on cultureos reported platform generic with limits 102/106/110, and curl from cultureos to each box's :9476 failed
- each of the three surfaces (mesh status, mcp, mesh serve) reads the same fleet reader, so they never disagree about a machine's level
- the dashboard sorts hottest first by default and the heat aura marks warm, hot and critical boxes without opening any panel
- the 10 s freshness bound holds with the SSH transport at the reader's poll interval for all four remote boxes at once
- the zone families come from the ventunoq profile, not from a hardcoded list in sense.py
- if the key ever needs a prompt, BatchMode makes the poll fail and the box shows unreachable; nothing waits on interactive input
- detect() on `tmp_path` trees carrying the real Thor (nvidia,p4071-0000+p3834-0008 nvidia,tegra264) and Orin (nvidia,p3737-0000+p3701-0005 nvidia,tegra234) compatible strings still returns thor and orin

## Success signals

- on cultureos, heat-governor mesh status --json reports 5 of 5 machines with readings no older than 10 s, and the mesh serve page renders all 5 in under 2 s
  - instruction: on cultureos run `heat-governor mesh status --json` and count machines whose age is 10 s or less (expect 5); open the mesh serve URL and time the first render
- on cultureos, heat-governor sense --json reports platform ventunoq and watches 5 zone families (cpu, cpuss, gpuss, nsp, ddrss) instead of all 48 zones
  - instruction: run heat-governor sense --json on cultureos and check platform and watched

## Scope / boundaries

- heat-governor does not drive the GPU clock on the VENTUNO Q in this work: gpuclock gains an explicit unsupported-platform guard, because the shell helper's `is_jetson`() (gpuclock.py:109) would fall through to nvidia-smi -lgc, which does not exist here
- the fleet view is read-only and outside every daemon's control path: state schema 1 is unchanged (no renamed/removed fields, top-level level/admit/`concurrency_factor`/cpus keep their meaning, pair shown separately) and a dead box never delays any daemon tick
- remote hostnames (spark/spark2/thor/orin and their tailnet names) live in a fleet host list, not hardcoded in package code, and no committed JSON carries a non-localhost url/host key

## Non-goals

- no third-party Python runtime dependencies and no node/npm toolchain in this repo: pyproject keeps dependencies = \[\], the MCP server is hand-rolled JSON-RPC over stdio, the HTTP server is stdlib ThreadingHTTPServer
- the MCP server and dashboard never pause, cap, apply or change config on any box: no write tools, no --apply passthrough

## Assumptions

- the reader's SSH key (~/.ssh/`id_ed25519`) needs no agent or passphrase, so a systemd user unit can use it; probe: BatchMode ssh to spark and thor succeeded from an empty environment (env -i HOME=... ssh -o BatchMode=yes)
- matching the exact compatible tokens arduino,monza or qcom,qcs8300 cannot select ventunoq on any current mesh box: probe found no device tree on spark or spark2, and Thor/Orin report nvidia,\* tokens

## Scope exploration

- `s1` — `heat_governor/platforms.py detect()`: detect() reads only proc/device-tree/model (platforms.py:158-181); here it is 'Qualcomm Technologies, Inc. Monaco Monza addons' while compatible is 'arduino,monza qcom,monaco-monza qcom,qcs8300', so the box resolves to 'generic'
  - seeds: `c2`
- `s2` — `live sysfs trips + uv run heat-governor sense/capabilities --json`: 48 zones; cpu-\* trips passive 126/130/135, gpuss-\*/nsp-\* 115/130/135, PMIC pmm86\*-therm passive 105 critical 125; generic profile limits 102/106/110 and empty watched list
  - seeds: `c3`
- `s3` — `heat_governor/sense.py read_gpu_jetson/read_fans/capabilities`: `read_gpu_jetson` globs class/devfreq/\*gpu\* but needs devices/platform/\*gpu\*/load (absent); no ina232 reader exists; `read_fans` reads only pwm1/`fan1_input`; fan text at sense.py:470 is a Jetson/Spark assumption
  - seeds: `c4`
- `s4` — `heat_governor/gpuclock.py`: probe() only checks helper + sudo (gpuclock.py:53-68); no platform guard; devfreq `max_freq` on 3d00000.gpu is writable but kernel already throttles via `cooling_device12`
  - seeds: `c5`
- `s5` — `tests/fakes.py + tests/test_sense_platforms.py`: `make_sys`(root, zones, model=, fan=) (fakes.py:34-55) writes only device-tree/model and a pwmfan hwmon; `test_detect_jetson` parametrises Thor/Orin model strings (lines 62-73)
  - seeds: `c6`
- `s6` — `cultureos install state (which/systemctl --user/uv tool list)`: heat-governor absent on cultureos; the four mesh boxes all use uv tool install + ~/.config/systemd/user/heat-governor.service running 'run --config %h/.config/heat-governor/config.toml' with \[daemon\] apply=true
  - seeds: `c7`
- `s7` — `heat_governor/pair.py + client.py`: PeerLink.status gives ok|stale|unreachable with skew-free staleness capped at `MAX_PEER_STALE_S`=600; PeerLink/PairRule are single-peer and bound to \[pair\]; client.read returns admit=True with reason no-state on failure (client.py:92-95)
  - seeds: `c8`
- `s8` — `docs/contracts/state.md + docs/integration.md`: schema bumps on incompatible change; unknown fields ignored; top-level decision stays the box's own (pair section); integration.md: keeps governing with no network; state server read-only, unauthenticated, bind only on a private link
  - seeds: `c9`
- `s9` — `heat_governor/state.py StateServer`: routes GET /state, /, /healthz only; no CORS, no OPTIONS, no history route; ThreadingHTTPServer in a daemon thread
  - seeds: `c10`
- `s10` — `culture-rules/web (style source)`: Vite+React18+TS, plain CSS custom properties; tokens.css pinned to agentculture/org b4d939ba, dawn light / hour-before-dawn dark via prefers-color-scheme; fonts are Fraunces (display) + Albert Sans (body), Lato not used; lane-row statistics board and pill tabs are the dashboard pattern
  - seeds: `c11`
- `s11` — `pyproject.toml + CLAUDE.md no-deps rule`: dependencies = \[\], hatchling wheel packages=\[`heat_governor`\], requires-python >=3.12; CI has no node except markdownlint
  - seeds: `c12`
- `s12` — `heat_governor/cli (+ explain/catalog.py, learn.py, overview.py)`: `_build_parser` at cli/`__init__.py`:64-99; noun pattern `_commands`/cli.py:30-43; catalog map catalog.py:336-367; learn/overview verb lists are hand-maintained, not parser-derived
  - seeds: `c13`
- `s13` — `CLI output contract + rubric (_output.py, governing.py run loop)`: results stdout / diagnostics stderr (`_output.py`:1-6); long-running verbs already exist (run, wait) with --json; whether teken's rubric invokes verbs beyond --help is unverified
  - seeds: `c14`
- `s14` — `MCP surface (absent today)`: zero hits for mcp/dashboard/cors in code or docs; no .mcp.json; a .mcp.json with a non-localhost url/host key would trip scan-secrets (`_scan_endpoints`, scan-secrets.py:216-230), a stdio command entry would not
  - seeds: `c15`
- `s15` — `scripts/scan-secrets.py`: endpoint check only parses valid JSON files and flags url/host/endpoint/`base_url` keys with http(s) to non-localhost; .py/.md/.toml are not endpoint-scanned
  - seeds: `c16`
- `s16` — `mesh deployment (ssh spark/spark2/thor/orin, read-only)`: all four run heat-governor with \[daemon\] apply=true and listen=127.0.0.1:9476; sparks also bind 10.77.77.1/.2 for \[pair\]; curl from cultureos to :9476 returns no connection on all four; names resolve to tailscale 100.x IPs; spark/spark2 are 0.14.1, thor/orin 0.13.0; spark sampled level=critical 89.1 C
- `s17` — `challenge pass / security lens: ssh argv from fleet host list (c8, c16)`: hosts are operator config that reaches subprocess argv; seeded the host-validation requirement
  - seeds: `c25`
- `s18` — `challenge pass / operations lens: ssh transport cost (probe: 0.52-0.55 s per fresh ssh poll to spark; no ControlMaster in ~/.ssh/config)`: persistent connection in c8 will not happen by itself; seeded the reader-managed ControlMaster requirement
  - seeds: `c26`
- `s19` — `challenge pass / unstated-assumptions lens: ssh auth under a user unit (probe: env -i ssh -o BatchMode=yes spark/thor true -> ok)`: the reader can run as a service with today's key; recorded as an assumption with the probe as evidence
  - seeds: `c27`
- `s20` — `challenge pass / data-flow lens: /state document size (probe: spark 2979 B, thor 1953 B)`: raw-doc buffering would cost about 130 MB per box per day; seeded the compact-sample requirement
  - seeds: `c28`
- `s21` — `challenge pass / observability lens: reader failure vs box failure (c8, c21)`: nothing in the frame distinguished a reader outage from box outages; seeded the reader-health requirement
  - seeds: `c29`
- `s22` — `challenge pass / adjacent-systems lens: process model of mcp vs mesh serve (c14, c21/h17)`: a stdio MCP server is spawned per client and cannot share an in-memory reader; routed to a user question (q7)
- `s23` — `challenge pass / security lens: mesh serve bind address (c10)`: the frame fixed same-origin serving but not where it listens; routed to a user question (q8)
- `s24` — `challenge pass / operations lens: page runtime dependencies (c11; probe: cdnjs, unpkg, fonts.googleapis.com -> 200)`: the dashboard needs internet to render; routed to a user question (q9)
- `s25` — `challenge pass / migration lens: fleet list vs strict config validation (config.py TOP_KEYS, c16)`: a new top-level key breaks older daemons' strict validation; routed to a user question (q10)
- `s26` — `challenge pass / adjacent-systems lens: detect() order on mesh boxes (probe: no /proc/device-tree on spark, spark2)`: compatible-based detection is safe for current boxes; recorded as an assumption with probe evidence
  - seeds: `c30`
- `s27` — `challenge pass / concurrency lens: daemon tick vs reader (pair.py background-thread pattern, c9/h9)`: the reader lives in a separate process and the daemon never waits on it; covered by c9/h9; no new finding
- `s28` — `challenge pass / hardware lens: ventunoq profile on cultureos (c3, c7)`: dry-run means no pause or cap on cultureos; the emc2305 fan stays kernel-owned; residual risk is the unmeasured limits (v1); no new finding
- `s29` — `challenge pass / reversibility lens: cultureos install (c7)`: rollback is systemctl --user disable --now heat-governor plus uv tool uninstall; nothing else on the box changes; no new finding

## Decisions

- The heat-governor fleet dashboard design is locked (2026-11-08) to artifact JMe4tUb7V64zQhii6bmsS4 version 1794166791-7eb1, kept as docs/design/fleet-dashboard.html; the real 'heat-governor mesh serve' page is built to it, except its theme picker: the shipped theme is Polar night only (c18, obligation o9), which replaces the four-skin obligation o7
- The fleet dashboard ships Polar night as its theme (cream light mode, slate dark mode, Auto follows the host theme); Geothermal, Kiln and Aurora remain in the locked reference only
- mesh serve is the one fleet reader: it polls the boxes and keeps history; mesh status and heat-governor mcp query its localhost JSON and fall back to a one-shot direct read when it is not running
- mesh serve listens on 127.0.0.1 by default (view on cultureos or through ssh -L); only an explicit --listen exposes it elsewhere
- the dashboard's React 18.3.1, htm 3.1.1 and Lato are vendored into package data so the page renders with no internet
- the fleet host list is a separate fleet.toml on cultureos read only by the reader; daemon config.toml and its strict validation are unchanged

## Open parks

- [unknown_nonblocking] VENTUNO Q limits are declared from trip points, not measured; a load run (CPU+GPU+NPU) is needed to tune warm/hot/crit and duty, like the Spark measurements in docs/HANDOFF-2026-10-23.md
- [unknown_nonblocking] UNO Q (arduino,imola) profile: no UNO Q is available to read sysfs from; don't assume the VENTUNO Q numbers; detect it and fall back to generic until measured
- [unknown_nonblocking] whether the reader should persist its history buffer to ~/.local/state across restarts; q6 accepted gaps while it is down, but every restart (updates) also wipes up to 24 h of trends
- [follow_up] upgrade release: add bearer-token checking to the state server and a token-protected listener on each box's tailnet IP (replaces SSH, q1), and a read-only GET /history route downsampled from records/\*.jsonl (replaces the reader-only buffer where present, q6); boxes upgraded one at a time with the operator's go-ahead (q4)

## Resolved vagueness

- [unknown_nonblocking] Dashboard history: no history route exists on any daemon (records are local JSONL in ~/.local/state/heat-governor/records); v1 charts only what the reader buffers while running, unless a /history route is added later — resolved: reader buffer now, /history route in the upgrade (q6)

# Using heat-governor from lobes, sparkrun, mjolnir or a script

heat-governor runs **one daemon per host**. Other tools integrate in one of two ways, and neither needs a
code dependency:

1. **Label the containers** so the daemon governs them.
2. **Read the admission signal** before starting new work.

## 1. Run the daemon

```bash
uv tool install heat-governor          # or: pip install heat-governor
heat-governor capabilities             # what this host lets us read and change
heat-governor sense                    # one reading of every zone
heat-governor run --once               # one dry-run tick: what it would do now
heat-governor unit -- --apply > $XDG_CONFIG_HOME/systemd/user/heat-governor.service
systemctl --user daemon-reload && systemctl --user enable --now heat-governor
loginctl enable-linger "$USER"         # keep it running without a login session
```

Without `--apply` the daemon still senses, records and publishes the signal, but changes no container.
Platform detection is automatic (DGX Spark, Jetson AGX Thor, Jetson AGX Orin, otherwise `generic`);
`--platform` and `--config file.toml` override it:

```toml
platform = "spark"
[profile]
hot_c = 91.0
duty_curve = [[88, 0.0], [90, 0.3], [92.5, 0.8], [93, 1.0]]
```

## 2. Label containers

| Label | Effect |
|---|---|
| `heat-governor.role=batch` | Duty-cycle paused as the host heats (fully paused at `critical`), and CPU-capped. For benchmarks, quantization, training, sweeps: anything that can be frozen for a second without breaking. |
| `heat-governor.role=serving` | **Never paused.** Observed only, unless also `heat-governor.cpu-cap=true`, which lets the daemon lower its CPU quota. For model servers clients are talking to. |
| (none) | Left alone. |

Containers started without labels can be named at the daemon instead: `--batch NAME`, `--serving NAME`
(add `--serving-cpu-cap` to cap them).

The daemon only ever undoes a pause it made itself. A pause it finds already in place is left alone, and a
restarted daemon releases exactly what its ledger says it did.

### sparkrun / mjolnir

Add the label to the container the recipe launches, e.g. `docker run --label heat-governor.role=batch ...`
for a benchmark or quantization job. A serving recipe that clients depend on should use
`heat-governor.role=serving` (optionally with `heat-governor.cpu-cap=true`). Nothing else changes: the
host's daemon picks the container up within 5 seconds.

### sparkrun tensor-parallel pair

When sparkrun runs ONE model across two boxes with tensor parallelism (TP=2), each box runs one container
(a rank) and the two step in lockstep over NCCL. Each box keeps its own daemon, but the ranks are one job:

- a CPU cap on one rank slows both (every token waits for the slower rank) and cools only one box;
- a thermal reset of either box kills the job on both, so the pair's risk is the hotter box's;
- pausing a rank hangs NCCL on the other box.

So `[pair]` makes the two daemons govern the ranks together. Each reads the other's `GET /state` every
`poll_s` and caps its rank by the **hotter** box: the tighter of the two daemons' decided CPU caps (the
band after lookahead, hysteresis and ramp), with the pair's level the higher of the two. Both compute the
same thing from the same two documents, so they move to the same band within about one poll. A peer that
cannot be read, or whose document stops advancing for `stale_after_s`, counts as one band hotter than
local, and loss and recovery are logged once each. Pair containers are always governed as `serving` (never
paused): a `[[containers]]` rule with role `batch` that matches a pair pattern is a config error, and a
`batch` label or `--batch` on one is overridden (logged once). Every other container, user unit and the
host's own signal keep using local readings only.

Two DGX Spark GB10 boxes, `spark` (10.77.77.1) and `spark2` (10.77.77.2), on a private management link.
On `spark`:

```toml
platform = "spark"

[daemon]
apply = true
listen = "127.0.0.1:9476"     # local clients, unchanged
gpu_ceiling_mhz = 2200        # keep it the same on both boxes

[pair]
peer = "10.77.77.2:9476"      # spark2's [pair] listen
listen = "10.77.77.1:9476"    # this daemon's state, for spark2 to read
containers = ["sparkrun_*"]   # the rank's container name or a glob; check `docker ps`
stale_after_s = 10            # peer silent this long -> one band hotter
poll_s = 1.0
cpu_cap = true                # cap the rank by the shared band
```

On `spark2`, the mirror image:

```toml
[pair]
peer = "10.77.77.1:9476"
listen = "10.77.77.2:9476"
containers = ["sparkrun_*"]
```

Notes:

- `peer` and `listen` are plain `HOST:PORT` (no `http://`, no path). A wildcard `[daemon] listen`
  (`0.0.0.0:9476`) already serves the peer: then leave `[pair] listen` out, or use another port, since the
  same port with a wildcard on either side is rejected (the second bind would fail). `run --once` binds no
  listener, so a preview works next to the running daemon.
- The peer counts as lost when its document has not changed for the larger of `stale_after_s` and the
  peer's own published `stale_after_s`, timed on this box's clock.
- `[pair] listen` binds a second state server and needs a daemon restart to change; every other `[pair]`
  key reloads live. The state server is read-only and unauthenticated (temperatures and container names),
  so bind it only on the private link. Port 9476 must be reachable between the two boxes on that link.
- sparkrun names its containers itself; match them with a glob on a stable part of the name (the job or
  recipe name). `heat-governor run --once` lists the pair containers it found.
- The operator GPU ceiling should be the same on both boxes: the slower rank sets the pair's pace. A
  mismatch is logged once per change (`pair: WARNING operator GPU ceiling differs ...`) and shown as
  `pair.gpu_operator_match = false`; the daemon never changes a clock because of the peer.
- Admission for the pair's work: the top-level `admit` stays this box's own signal. A client sending
  requests to the pair reads the pair's signal instead (the hotter box): `heat-governor wait --pair`,
  `client.wait(pair=True)`, `client.Gate(n, pair=True)`, or `pair.admit` in the document. Either box
  publishes the same pair decision.
- `heat-governor state` prints a `pair:` line: the pair's level, cap, basis (`local`, `peer`, `equal`,
  `peer_lost`) and the peer's status.

The fields are in [`contracts/state.md`](contracts/state.md#the-pair-section).

### lobes

The lobes fleet lanes are **serving** containers (never paused). Today they are named at the daemon
(`--serving model-gear-vllm-primary ...`); the lobes compose templates can carry the labels instead. A
benchmark or side job lobes starts is a **batch** container.

## 3. Read the signal before new work

### Shell

```bash
heat-governor wait --timeout 1800 && start-next-job      # exit 3 if still held at the timeout
heat-governor state --json                               # one look
heat-governor wait --source http://OTHER-HOST:9476/state     # another host's daemon (--listen there)
```

### Python

```python
from heat_governor import client

client.wait(timeout=1800)                 # block until new work is admitted
gate = client.Gate(base_concurrency=4)    # in flight: 4 cool, 2 warm, 0 (hold) hot
with gate.slot():
    send_one_request()
```

`heat_governor/client.py` is stdlib-only and self-contained, so a project that prefers not to depend on
the package can cite (copy) it. The JSON contract it reads is `docs/contracts/state.md`; any language can
read the file directly.

## 4. Tune it

All thresholds, curves, bands, the per-level concurrency factors, the poll interval and the container list
live in `$XDG_CONFIG_HOME/heat-governor/config.toml` (`heat-governor config init --write` starts one). The daemon
re-reads it within ~2 s; an invalid edit is rejected and logged, and the last good settings stay. Every tick
is recorded, so `heat-governor replay` can check a change against real traces before it governs.

GPU clock ceilings need a one-time root install of a small helper (`heat-governor gpu-clock helper` prints
it and the sudoers line), then `gpu_clock = true` under `[daemon]`.

## What it does not do

- It does not change power modes (`nvpmodel`) or fans; those stay with NVIDIA's own tools.
- It does not stop work that is running. Admission gates new work; duty-cycling only pauses batch
  containers.
- It needs no network and no LLM to keep governing.

## Reading the fleet

The per-host daemon contract ([`docs/contracts/state.md`](contracts/state.md)) is unchanged. To see all
machines at once:

- `heat-governor mesh status --json` asks a running `mesh serve` and falls back to a direct concurrent
  read of every machine in `fleet.toml`.
- `GET /api/fleet` on `mesh serve` (default `127.0.0.1:9480`) returns the same snapshot;
  `GET /api/history?machine=<name>&range=15m|1h|6h|24h` returns the reader's compact history.
- `heat-governor mcp` is a read-only MCP server over stdio with tools `mesh_status`, `machine_state`
  and `sense`: `claude mcp add heat-governor -- heat-governor mcp`.

Each machine entry carries `state` (`ok`, `stale`, `unreachable`) judged on the reader's clock; `admit` is
null unless the machine is `ok`, so a missing reading never reads as permission. Each entry's
`thermal.memory` is derived from the daemon's `sensors` (`mem:*`, `psi:memory_*`, memory zones): `used_pct`,
`avail_mib`, `total_mib` (derived as avail / (1 - used/100); null at 100% or when either is missing),
`swap_used_mib`, `pressure_some_avg10` and `temp_c`. `temp_c` is the hottest zone whose type starts with
`ddr`, `ddrss`, `dram`, `emc` or `mem` (a PMIC such as `pmm8650au_1_sdram` is not counted), and is null
on boards that expose no such zone (Jetson, DGX Spark); `memory` itself is null when the document has no
`mem:*` sensor. History samples carry `mem_used_pct`. The list of machines is
[`fleet.toml`](fleet.example.toml). A bearer-token tailnet listener and a daemon `GET /history` route
are **(planned)**.

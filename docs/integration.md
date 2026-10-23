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

# GPU clock ceiling contract

The GPU clock ceiling is the one control that needs root. `heat_governor/gpuclock.py` calls a small
root helper (`/usr/local/sbin/heat-governor-gpuclock`: `lock <MHz>`, `reset`, `status`) through a
narrow sudoers rule. `heat_governor/gpulaw.py` decides what to write, and
`heat_governor/operator_ceiling.py` holds the operator's ceiling.

## The operator's ceiling is law

Operator rule (2026-10-24): "governor hand ceiling is law. request to pass it is logged, so we can tell
that it would have happened".

The incident behind it happened on the Jetson AGX Orin on 2026-10-24. The operator set GPU ceilings by
hand (`sudo heat-governor-gpuclock lock <MHz>`) for a clock sweep. The running daemon had
`gpu_clock = true`. At the warm level it wrote its own band ceiling and later removed it, so the
operator's ceilings were overridden for the whole sweep without any log line.

### Declaring it

| Source | How | Lifetime |
|---|---|---|
| `config` | `[daemon] gpu_ceiling_mhz = <MHz>` (a whole number, 100-5000) | Live-reloaded like the other daemon keys; removing the key clears it. |
| `hold` | `heat-governor gpu-clock hold <MHz>` | Persisted in `<data_dir>/operator.json`, so it survives restarts. A running daemon reads it within ~2 s (it polls the file's mtime). |
| `observed` | A hand lock the hardware shows (Jetson only, see below) | Persisted in the same file until `gpu-clock release`. |

When more than one is set, the lowest wins. `gpu-clock release` removes the file, which clears a hold
or an observed ceiling. A `config` ceiling stays until its key is removed. `gpu-clock status` shows
the ceiling in force and its source. `hold` and `release` only write the file. The daemon applies it
when it runs with `apply`; in dry-run it logs `dry-run would set gpu ceiling <MHz>`.

`<data_dir>/operator.json` is written atomically (temp file, fsync, rename):

```json
{"gpu_ceiling_mhz": 900, "source": "hold", "at": 1761300000.0, "at_utc": "2026-10-24T10:00:00Z"}
```

`source` is `hold` or `observed`. A file that does not parse is treated as no hold.

### What the daemon does

- **The ceiling written is `min(operator ceiling, band)`.** The daemon may go below the operator
  ceiling for heat, and it comes back up to the operator ceiling as it cools. It never writes a
  ceiling above the operator ceiling.
- **The operator ceiling applies with `gpu_clock` off too.** It is the operator's own ceiling, written
  through the same helper. With `gpu_clock` off there is no band, so the daemon holds exactly the
  operator ceiling.
- **The daemon never removes it.** On exit, on turning `gpu_clock` off and in crash recovery
  (`Ledger.recover`), the daemon restores the operator ceiling instead of calling `reset`.
- **Clearing it brings back the daemon's own behaviour.** With `gpu_clock` on that is the band ceiling,
  or `reset` when the band wants none. With `gpu_clock` off the daemon resets the ceiling it was
  holding for the operator.
- **A band it may not write is logged**, once per change of the wanted value (or of the operator
  ceiling):

  ```text
  gpu: would raise the ceiling to none (policy: level cool) -- operator ceiling 2200 MHz holds
  gpu: would raise the ceiling to 1100 (policy: level warm) -- operator ceiling 900 MHz holds
  ```

  The state document carries `gpu_operator_mhz`, `gpu_operator_source`, `gpu_wanted_mhz` and
  `gpu_held_by_operator` ([`state.md`](state.md)). Every per-tick record carries
  `gpu_operator_mhz`, `gpu_wanted_mhz` and `gpu_held_by_operator`. `heat-governor sensors` reports
  `gpu_held_by_operator_ticks`, the number of ticks in the window on which the operator ceiling held a
  higher policy ceiling.

### An undeclared hand lock

The daemon reads the ceiling back with the helper's existing `status` (the helper is unchanged). It
does this after each of its own writes and every 5 s while it drives the GPU.

- **Jetson (Thor, Orin):** `status` prints devfreq `max_freq` in MHz. A read-back below the value
  the daemon saw after its own last write was made by someone else. The daemon treats it as the
  operator's ceiling (source `observed`), persists it and logs it once:

  ```text
  gpu: observed an outside ceiling of 714 MHz -- treating it as the operator's (law)
  ```

  From then on the daemon never writes above it, until `gpu-clock release`. The helper snaps a lock
  down to the nearest step in `available_frequencies`, so the comparison uses the read-back after the
  daemon's own write, not the MHz it asked for.
- **DGX Spark (GB10):** `nvidia-smi` exposes no `-lgc` lock. `status` prints only the current and
  maximum SM clocks (`2405, 3003`; checked live 2026-10-24). A raw `nvidia-smi -lgc` or
  `sudo heat-governor-gpuclock lock` is therefore **invisible** to the daemon and is *not* law there.
  To make a hand ceiling law on the Spark, declare it with `gpu-clock hold <MHz>` or
  `[daemon] gpu_ceiling_mhz`.

Limits:

- A hand lock made *before* the daemon starts is the starting point, not an observation. The daemon
  first reads the ceiling when it probes the helper, so declare a ceiling that predates the daemon.
- Nothing else that lowers devfreq `max_freq` (for example an nvpmodel change) can be told apart from
  a hand lock, so it is also treated as the operator's.
- A hand lock *above* the daemon's ceiling is not detected, and an observed ceiling stays law until
  `gpu-clock release`. To sweep a ceiling upward by hand, use `gpu-clock hold <MHz>` for each step.

Status: implemented and unit-tested with a fake helper. The observed-ceiling path has not been run
on a real Jetson.

## Containers follow the same rule

A CPU band never raises a container's own `NanoCpus` limit when that limit is tighter than the band:
a container the operator limited to 4 CPUs is never set to 8. If the governor had capped it lower,
it restores the container's own limit. The would-be raise is logged once per change of the band:

```text
would raise <container> to 8 cpus -- its own limit 4 holds
```

User units already follow this rule ([`user-units.md`](user-units.md)): a band never raises a quota
tighter than the unit's own.

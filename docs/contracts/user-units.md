# User-unit CPU quota contract

`heat-governor run` can cap the CPU quota of the operator's own **systemd user units** (for example
`app.slice`, where interactive terminal sessions run). It is **off** unless the config file lists a unit.
Each unit runs in one of two modes:

- `levels` (the default): a fixed CPU count per temperature level. The band rule is
  `heat_governor.policy.unit_cpus`.
- `adaptive`: a feedback loop that measures how the temperature rate responds to the CPU it takes
  away, learns that response per unit, sizes each change from it, and keeps what it learned across
  restarts (`heat_governor/adaptive.py`; see [Adaptive mode](#adaptive-mode)).

`heat_governor/units.py` makes the changes in both modes.

## Why

On the DGX Spark, 2026-10-23 night, parallel interactive sessions (Claude Code sessions running test
suites, `cypher-shell`) pushed the load average to 65-81 and the board zone to 95.2 C; the board
hard-resets at about 94.5-95 C. A CPU cap on the serving container cannot reach that load: those
processes run in `app.slice/app-<terminal>-<n>.scope` under the user manager, not in docker.
The user manager has the `cpu` controller delegated
(`user@1000.service/cgroup.subtree_control` = `cpu memory pids`), so no root is needed.
Docker containers live in `system.slice` and are never affected.

## Configuration

```toml
[[units]]
name = "app.slice"     # a unit name ending .slice, .service or .scope
warm = 8               # CPUs at each level; CPUQuota = cpus x 100%
hot = 4
critical = 2
dry_run = true         # optional: log "dry-run would ..." and change nothing
```

| Key | Meaning |
|---|---|
| `name` | Required. A `systemctl --user` unit ending `.slice`, `.service` or `.scope`; it may not start with `-`. Listed once. |
| `dry_run` | Optional, default `false`. When `true` the unit is read and every decision is logged as `dry-run would ...`, but nothing is changed and nothing is written to the ledger, even when the daemon runs with `apply = true`. Use it to watch what a unit cap would do before enabling it; turning it off on a live reload applies the current level at once. The state reports it per unit. |
| `mode` | Optional, `levels` (default) or `adaptive`. |
| `warm`, `hot`, `critical` | `levels` only. Optional CPU counts (> 0, fractions allowed) per level. They may not rise from warm to hot to critical. Not allowed with `mode = "adaptive"`. |
| `min_cpus`, `engage`, `interval_s`, `target_rate_c_min` | `adaptive` only (an error with `levels`); see [Adaptive mode](#adaptive-mode). |

In `levels` mode:

- The cap at a level is the band of the highest listed level at or below it (only `hot` given: warm is
  uncapped, hot and critical get the hot band). `cool` never caps.
- If a unit lists **none** of the three, the platform's default applies. Only `spark` declares one:
  warm 8 / hot 4 / critical 2 CPUs (of 20), **declared, unmeasured**. On other platforms such a unit is
  logged once and never capped.
- A unit's own bands replace the platform default entirely (they are not merged).
- A band never raises a quota tighter than the unit's own.
- The level carries the controller's hysteresis and dwell, so a cap falls back one level at a time.
- A running daemon picks up added and removed units within about 2 s; a removed unit gets its quota back.

## Adaptive mode

```toml
[[units]]
name = "app.slice"
mode = "adaptive"
min_cpus = 1               # never cap below this (> 0; default 1)
engage = "warm"            # the level at which the loop engages: warm (default) | hot | critical
interval_s = 20            # one evaluation per interval (>= 5; default 20)
target_rate_c_min = 0.0    # temperature rate to hold while engaged, C per minute (default 0 = flat)
dry_run = true             # optional, as in levels mode
```

The loop's conditions come from what it measures, not from fixed CPU counts. Its inputs, at most once
per `interval_s`: the level, the governing temperature's rate (`rate_c_s` in the state, over the
profile's `rate_window_s`), the unit's measured CPU use over the last interval, and the host CPU count.

| Situation | What the loop does |
|---|---|
| Engaged (level >= `engage`) and `rate - target` > 0.005 C/s | **Tighten.** The cut in CPUs is `error / gain` once a gain is learned, else the cold-start step `max(1, 25% of use)`. The cut is clamped to 1 CPU minimum and 50% of measured use maximum. The new cap is `max(min_cpus, use - cut)`, never above the current cap. It is based on measured use, because a cap far above what the unit uses does nothing. |
| Engaged, `rate - target` < -0.005 C/s, and the level is exactly `engage` | **Release** one step: `+max(1, 10% of host CPUs)`, at most once per `2 x interval_s`. |
| Engaged, otherwise | **Hold** (inside the deadband, at `min_cpus`, or above the engage level while cooling). |
| Not engaged (level below `engage`) | **Release** one step per `2 x interval_s`. Once the cap would reach the host CPU count it is removed, which restores the unit's own quota through the normal restore path. |
| Sensor lost (no readable zone; the level is then `hot`) | Engaged: tighten by the cold-start step, because a learned gain cannot be applied to an unknown error. Not engaged: hold, so the loop never releases while blind. Nothing is learned. |
| Use not measured yet (the first evaluation, or an unreadable cgroup) | Hold. |

**Learning.** Each tighten records `(time, use, rate)`. The next evaluation compares: when use fell by
at least 0.5 CPU and the rate fell, the sample is `(rate_before - rate_now) / (use_before - use_now)` in
C/s per CPU. Otherwise the sample is counted in `ignored`. The gain is an EWMA (alpha 0.3) of the samples,
clamped to [0.0001, 0.05] C/s per CPU. Each tighten yields one sample at most. **In dry-run nothing is
learned**: the change is not made, so there is no response to measure, and the would-be decision is only
logged.

**Persistence.** The learned model per unit, per platform name, is kept in `<data_dir>/adaptive.json`:
`{"version": 1, "platforms": {"spark": {"app.slice": {"gain": 0.004, "samples": 5, "ignored": 1,
"updated": 1761300000.0}}}}`. `updated` is epoch seconds. The file is written atomically (temporary file,
fsync, rename) after each accepted sample and read on start. An unreadable or corrupt file is logged
(`adaptive: <path> is unreadable (...): starting fresh`) and replaced by the next save. A config reload
keeps each loop's current cap and model. Switching a unit back to `levels` drops its loop, but its saved
model stays in the file.

**Measuring use.** `systemctl --user show -p ControlGroup -- <unit>` gives the unit's cgroup path (cached).
The daemon reads `usage_usec` from `/sys/fs/cgroup<path>/cpu.stat`. Use in CPUs is the change in
`usage_usec` divided by the change in wall-clock microseconds. When the file is gone the path is looked
up once more, because the unit may have restarted. A counter that went backwards gives no reading and
starts a new baseline. Levels-mode units are measured too, every tick, for the state document.

**Log lines** (each also goes into the records):

```text
adaptive app.slice: tighten 16 -> 12 cpus (use 16, rate +0.050 C/s, target +0.000 C/s, cold start: 25% of use)
adaptive app.slice: tighten 12.3 -> 6.15 cpus (use 12.3, rate +0.030 C/s, target +0.000 C/s, gain 0.0040 C/s/cpu from 5 samples)
adaptive app.slice: tighten 12 -> 9 cpus (use 12, sensor lost: cold-start step)
adaptive app.slice: learned gain 0.0125 C/s/cpu from 1 samples (rate fell 0.050 C/s for 4 cpus)
adaptive app.slice: sample ignored: use fell 0.2 cpus, rate fell +0.020 C/s (needs >= 0.5 cpus and a falling rate; 1 ignored)
adaptive app.slice: release 9 -> 11 cpus (rate -0.020 C/s, target +0.000 C/s, cooling faster than needed)
adaptive app.slice: release 12 -> 14 cpus (level cool, below engage warm)
adaptive app.slice: release 18 -> none (level cool, below engage warm: unit's own quota restored)
adaptive app.slice: dry-run would tighten 16 -> 12 cpus (use 16, ...; not learning in dry-run)
```

The `cap unit ...` / `restore unit ...` lines of the actuator follow, as in levels mode.

## Behaviour

Both modes hand their cap to the same actuator, so every rule below applies to both.

| Rule | How |
|---|---|
| Ledger first | Before the first change the unit's current quota is read fresh (`systemctl --user show -p CPUQuotaPerSecUSec`) and written to the ledger (`unit_quota`: microseconds per second, `null` = no quota). |
| Runtime only | Every change is `systemctl --user set-property --runtime -- <unit> CPUQuota=<N>%`. A reboot drops it: a cap cannot outlive the machine's uptime. |
| Restore | On exit, on removal from the config, and by the next start's ledger recovery after a crash: `CPUQuota=<original>%`, or `CPUQuota=` (no quota). A failed restore stays in the ledger for the next try; a unit that no longer exists is dropped. |
| Dry run | Without `--apply` it reads the unit and logs `dry-run would cap unit ...`; it changes nothing. |
| Missing unit | A unit that is not found or is inactive is logged once, shown `missing: true` in the state, skipped, and looked for again every 30 s. |
| State | `units` in the state document: `name`, `mode`, `dry_run`, `cpus`, `use_cpus`, `missing`, and for adaptive units `gain` and `samples` ([state.md](state.md)). |

The daemon itself is a user service and on the Spark runs in `app.slice`, so a cap on `app.slice`
bounds it too; it logs a note when that is so. It needs little CPU, but under heavy contention its
share inside the slice follows `CPUWeight` (the default 100 is shared equally with every busy sibling
scope).

## Status

Implemented and unit-tested with a fake `systemctl` and a temporary cgroup tree (no test sets a property
on a real unit or reads a real cgroup). The bands are declared, not measured: whether 8 / 4 / 2 CPUs keep
the Spark under its reset temperature during a heavy interactive load has not been run. Adaptive mode has
not run on a real box either: its constants (deadband 0.005 C/s, 25% cold-start step, 50% largest cut,
10% release step, EWMA alpha 0.3, gain clamp) are design values, not tuned. Nor is it measured whether
the board's rate responds to a unit's CPU within one `interval_s`, which the learning rule assumes.

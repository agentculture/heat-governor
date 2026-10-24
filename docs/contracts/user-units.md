# User-unit CPU quota contract

`heat-governor run` can cap the CPU quota of the operator's own **systemd user units** (for example
`app.slice`, where interactive terminal sessions run) by temperature level. It is **off** unless the
config file lists a unit. `heat_governor/units.py` implements it; the band rule is
`heat_governor.policy.unit_cpus`.

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
```

| Key | Meaning |
|---|---|
| `name` | Required. A `systemctl --user` unit ending `.slice`, `.service` or `.scope`; it may not start with `-`. Listed once. |
| `warm`, `hot`, `critical` | Optional CPU counts (> 0, fractions allowed) per level. They may not rise from warm to hot to critical. |

- The cap at a level is the band of the highest listed level at or below it (only `hot` given: warm is
  uncapped, hot and critical get the hot band). `cool` never caps.
- If a unit lists **none** of the three, the platform's default applies. Only `spark` declares one:
  warm 8 / hot 4 / critical 2 CPUs (of 20), **declared, unmeasured**. On other platforms such a unit is
  logged once and never capped.
- A unit's own bands replace the platform default entirely (they are not merged).
- A band never raises a quota tighter than the unit's own.
- The level carries the controller's hysteresis and dwell, so a cap falls back one level at a time.
- A running daemon picks up added and removed units within about 2 s; a removed unit gets its quota back.

## Behaviour

| Rule | How |
|---|---|
| Ledger first | Before the first change the unit's current quota is read fresh (`systemctl --user show -p CPUQuotaPerSecUSec`) and written to the ledger (`unit_quota`: microseconds per second, `null` = no quota). |
| Runtime only | Every change is `systemctl --user set-property --runtime -- <unit> CPUQuota=<N>%`. A reboot drops it: a cap cannot outlive the machine's uptime. |
| Restore | On exit, on removal from the config, and by the next start's ledger recovery after a crash: `CPUQuota=<original>%`, or `CPUQuota=` (no quota). A failed restore stays in the ledger for the next try; a unit that no longer exists is dropped. |
| Dry run | Without `--apply` it reads the unit and logs `dry-run would cap unit ...`; it changes nothing. |
| Missing unit | A unit that is not found or is inactive is logged once, shown `missing: true` in the state, skipped, and looked for again every 30 s. |
| State | `units` in the state document: `name`, `cpus`, `missing` ([state.md](state.md)). |

The daemon itself is a user service and on the Spark runs in `app.slice`, so a cap on `app.slice`
bounds it too; it logs a note when that is so. It needs little CPU, but under heavy contention its
share inside the slice follows `CPUWeight` (the default 100 is shared equally with every busy sibling
scope).

## Status

Implemented and unit-tested with a fake `systemctl` (no test sets a property on a real unit). The bands
are declared, not measured: whether 8 / 4 / 2 CPUs keep the Spark under its reset temperature during a
heavy interactive load has not been run.

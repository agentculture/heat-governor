# State contract (schema 1)

`heat-governor run` publishes one JSON document about once a second. Clients read it to decide whether to
start new work and how much to run at once. This file is the contract; `heat_governor/state.py` writes it
and `heat_governor/client.py` reads it.

## Where it is

| Source | Default |
|---|---|
| file | `$HEAT_GOVERNOR_STATE`, else `$XDG_RUNTIME_DIR/heat-governor/state.json`, else `$XDG_STATE_HOME/heat-governor/state.json` |
| HTTP | `GET http://HOST:PORT/state` when the daemon runs with `--listen HOST:PORT` (off by default) |

`$XDG_CONFIG_HOME` and `$XDG_STATE_HOME` default to `$HOME/.config` and `$HOME/.local/state` (the XDG defaults).

The file is replaced atomically (write a temporary file, then rename), so a reader never sees a partial
document. It is mode 0644, so a client in a container can read it through a read-only bind mount of the
directory (mount the directory, not the file: an atomic rename does not show through a single-file mount).

## Fields

| Field | Type | Meaning |
|---|---|---|
| `schema` | int | `1`. Bumped on any incompatible change. |
| `host`, `platform`, `version` | str | Who published it: hostname, `spark`/`thor`/`orin`/`generic`, package version. |
| `at`, `at_utc` | float, str | When it was published (epoch seconds, ISO UTC). |
| `stale_after_s` | float | Treat the document as unknown when older than this. |
| `level` | str | `cool`, `warm`, `hot` or `critical`. |
| `admit` | bool | `true` in `cool` and `warm`: new work may start. `false` in `hot` and `critical`. |
| `concurrency_factor` | float | `1.0` cool, `0.5` warm, `0.0` hot/critical. Allowed in-flight = `max(1, floor(base × factor))` while admitted. |
| `duty` | float | Fraction of each period batch containers are paused (0-1). |
| `cpus` | int or null | CPU cap applied to governed containers; `null` = their own limit. |
| `gpu_ceiling_mhz` | int or null | GPU clock ceiling the daemon set (its band, or the operator's); `null` = none. |
| `gpu_clock` | str | `off`, or the control's status (`ok ...`, `helper not installed ...`). |
| `gpu_operator_mhz`, `gpu_operator_source` | int or null, str or null | The operator's GPU ceiling in force (law) and where it comes from: `config` (`[daemon] gpu_ceiling_mhz`), `hold` (`gpu-clock hold`) or `observed` (a hand lock seen on a Jetson); `null` = none. See [`gpu-clock.md`](gpu-clock.md). |
| `gpu_wanted_mhz` | int or null | The policy's band ceiling this tick while `gpu_clock` is on; `null` = it wants no ceiling, or `gpu_clock` is off. |
| `gpu_held_by_operator` | bool | `true` when the policy wanted a ceiling above the operator's (including none): the operator's held, and this is what would have happened. Also in every per-tick record; `heat-governor sensors` counts the ticks (`gpu_held_by_operator_ticks`). |
| `config`, `config_error` | str or null | The config file in force, and why the last edit was rejected (if it was). |
| `temp_c` | float or null | Governing temperature: the hottest readable watched zone. `null` = sensor lost (level is then `hot`). |
| `effective_c`, `rate_c_s` | float | Temperature plus the lookahead on its rise, and the rise rate. |
| `hottest_zone` | str or null | `index:type` of the hottest zone. |
| `sensors` | object | Every sensor separately, this tick: `zone:<index>:<type>` (each thermal zone, null when unreadable), `gpu:<field>` (temp_c, power_w, sm_mhz, gr_mhz, mem_mhz, util_pct, mem_util_pct, events, tlimit_c, limit_c), `fan:<name>`, `cpu:*` (online, busy_pct, iowait_pct, mhz_avg, mhz_max, load1), `mem:*` (used_pct, avail_mib, swap_used_mib), `psi:<memory\|io\|cpu>_<some\|full>_avg10`, `disk:<dev>:*` (read_mbs, write_mbs, busy_pct), `nvme:<dev>:<label>` (every NVMe temperature sensor: composite, sensor_1, ...; the drive's own limits as `..._max_c` / `..._crit_c`). Rates are deltas since the previous tick. |
| `source` | str | Which input governs: `board` (thermal zones) or `gpu` (the GPU's own temperature, mapped onto the board's bands). |
| `gpu_temp_c`, `gpu_limit_c` | float or null | The GPU's reading, and its own thermal limit where the driver reports headroom (`temperature.gpu.tlimit`). |
| `reason` | str | `rising`, `steady`, `falling` or `sensor_lost`. |
| `limits` | object | `warm_c`, `hot_c`, `crit_c` of the active profile. |
| `applying` | bool | Whether the daemon changes containers (`--apply`) or only signals. |
| `containers` | list | Governed containers: `name`, `role`, `paused_by_governor`, `cpus`, `pair` (`true` for a container of the tensor-parallel pair; since 0.14.0). |
| `units` | list | Configured systemd user units (`[[units]]`, empty when none): `name`, `mode` (`levels` or `adaptive`), `dry_run` (decisions logged only), `cpus` (the cap the daemon holds — for a dry-run unit, the cap it would hold; `null` = the unit's own quota), `use_cpus` (CPUs the unit used over the last measurement: each tick for `levels`, each evaluation for `adaptive`; `null` until measured or when its cgroup is unreadable), `missing` (`true` while the unit is not found or inactive and therefore skipped). Adaptive units also carry `gain` (learned C/s of temperature rate per CPU removed, `null` until learned) and `samples`. See [`user-units.md`](user-units.md). |

| `pair` | object or null | The tensor-parallel pair (`[pair]` in the config, since 0.14.0); `null` without one. See [The pair section](#the-pair-section). |

The level, admission and temperatures are this tick's. Actuator results (`containers`, `units`, `gpu_ceiling_mhz`)
are as of the previous tick: the daemon publishes before it touches docker or the GPU helper, so a slow or
failing actuator can never delay the admission signal.

## The pair section

When one model runs across two boxes with tensor parallelism (one container, a "rank", per box), each
box's daemon reads the other's document and governs its rank by the hotter of the two. The top-level
`level`, `admit`, `concurrency_factor` and `cpus` stay **this box's own** decision: they are what the peer
reads, so the two daemons never feed each other's pair result back. The pair's decision is published
separately:

| Field | Type | Meaning |
|---|---|---|
| `peer` | str | `HOST:PORT` of the peer daemon's state server (`[pair] peer`). |
| `peer_status` | str | `ok`; `stale` (the peer answers but its `at` has not changed for `stale_after_s`, measured on this box's clock from when it last changed); `unreachable` (never read, or the last read failed and the last good one is older than `stale_after_s`). Any read that fails, or a document that is no decision (no level, a non-finite `at` or `cpus`), counts as a failed read. |
| `peer_error` | str or null | The last read failure while not `ok`. |
| `peer_age_s` | float or null | Seconds since the peer's document last advanced (this box's clock). |
| `stale_after_s` | float or null | The staleness window in force: the larger of `[pair] stale_after_s` and the peer's own published `stale_after_s` (its cadence, honoured up to 600 s). |
| `peer_host`, `peer_level`, `peer_cpus`, `peer_effective_c` | | The peer's own `host`, `level`, `cpus` and `effective_c`, as last read (kept while stale). |
| `peer_gpu_operator_mhz`, `gpu_operator_match` | int or null, bool or null | The peer's operator GPU ceiling, and whether it equals this box's `gpu_operator_mhz` (`null` when the peer is not `ok` or does not publish it). A mismatch is logged, never acted on. |
| `level`, `admit`, `concurrency_factor` | str, bool, float | The pair's level: the higher of the two boxes' levels; admission and factor from it, by this box's `concurrency`. |
| `cpus` | int or null | The CPU cap on the pair's containers: the tighter of the two boxes' `cpus` (`null` = their own limit). |
| `basis` | str | `local` (this box is the hotter), `peer`, `equal`, or `peer_lost` (not `ok`: one level and one CPU band hotter than this box alone; the loosest band when this box has no cap). |
| `containers` | list | Names of this box's pair containers (governed as serving: never paused). |

What is shared is the **decided** level and CPU cap (after lookahead, hysteresis, dwell and the ramp), not a
raw temperature: both daemons take `max(level)` and `min(cpus)` of the same two documents, so they choose
the same band within one `[pair] poll_s` of each other. Re-deriving a band from a shared temperature would
let each box's own ramp state make them disagree for a ramp interval. Bands are monotone in temperature, so
the tighter cap is the hotter box's.

Clients of the pair's work (a gateway sending requests to the pair, a job launcher) read `pair.admit` and
`pair.concurrency_factor` -- `client.read(..., pair=True)` or `heat-governor state|wait --pair` -- and fall
back to the host signal (reason `no-pair`) when the daemon has no `[pair]`. Either box's document carries
the same pair decision, so a client may read whichever box it reaches.

## Client rules

1. **Missing or stale** (`now - at > stale_after_s`) means no governor is running there. The reference
   client admits by default (`if_unknown="admit"`); a caller that would rather wait than run ungoverned
   passes `if_unknown="hold"`.
2. **Never interrupt running work.** The signal gates *new* work. A request that already started finishes.
3. **Re-read before each new unit of work** (a request, a batch, a job), not once at start.
4. Unknown fields are ignored; new optional fields may appear without a schema bump (`pair`, for
   example, arrived in 0.14.0 under schema 1).

## Example

```json
{"schema":1,"host":"box-1","platform":"spark","version":"0.11.0","at":1761229495.1,
 "at_utc":"2026-10-23T14:24:55Z","stale_after_s":5.0,"level":"warm","admit":true,
 "concurrency_factor":0.5,"duty":0.15,"cpus":8,"temp_c":89.5,"effective_c":89.9,"rate_c_s":0.8,
 "hottest_zone":"4:acpitz","reason":"rising","limits":{"warm_c":88.0,"hot_c":91.5,"crit_c":93.0},
 "applying":true,"containers":[{"name":"model-gear-vllm-primary","role":"serving",
 "paused_by_governor":false,"cpus":8}],"units":[{"name":"app.slice","mode":"adaptive","dry_run":false,"cpus":8.0,"use_cpus":7.9,"missing":false,
 "gain":0.004,"samples":5}]}
```

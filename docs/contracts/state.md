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
| `gpu_ceiling_mhz` | int or null | GPU clock ceiling the daemon set; `null` = none. |
| `gpu_clock` | str | `off`, or the control's status (`ok ...`, `helper not installed ...`). |
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
| `containers` | list | Governed containers: `name`, `role`, `paused_by_governor`, `cpus`. |

The level, admission and temperatures are this tick's. Actuator results (`containers`, `gpu_ceiling_mhz`)
are as of the previous tick: the daemon publishes before it touches docker or the GPU helper, so a slow or
failing actuator can never delay the admission signal.

## Client rules

1. **Missing or stale** (`now - at > stale_after_s`) means no governor is running there. The reference
   client admits by default (`if_unknown="admit"`); a caller that would rather wait than run ungoverned
   passes `if_unknown="hold"`.
2. **Never interrupt running work.** The signal gates *new* work. A request that already started finishes.
3. **Re-read before each new unit of work** (a request, a batch, a job), not once at start.
4. Unknown fields are ignored; new optional fields may appear without a schema bump.

## Example

```json
{"schema":1,"host":"box-1","platform":"spark","version":"0.11.0","at":1761229495.1,
 "at_utc":"2026-10-23T14:24:55Z","stale_after_s":5.0,"level":"warm","admit":true,
 "concurrency_factor":0.5,"duty":0.15,"cpus":8,"temp_c":89.5,"effective_c":89.9,"rate_c_s":0.8,
 "hottest_zone":"4:acpitz","reason":"rising","limits":{"warm_c":88.0,"hot_c":91.5,"crit_c":93.0},
 "applying":true,"containers":[{"name":"model-gear-vllm-primary","role":"serving",
 "paused_by_governor":false,"cpus":8}]}
```

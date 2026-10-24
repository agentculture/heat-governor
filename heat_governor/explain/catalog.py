"""Markdown catalog for ``heat-governor explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("heat-governor",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# heat-governor

Keeps machines running heavy AI work without thermal resets, while losing as
little throughput as possible. One daemon per host senses about once a second,
decides a level (cool / warm / hot / critical), publishes an admission signal,
records every tick, and with `--apply` throttles the containers that opted in.

Supported: DGX Spark (GB10, measured bands), Jetson AGX Thor and Orin (bands
declared from their trip points, unmeasured), and a generic fallback.

## Governing verbs

- `heat-governor sense` / `capabilities` -- read-only probes.
- `heat-governor run [--apply]` -- the daemon.
- `heat-governor state` / `wait` -- the client side of the signal.
- `heat-governor report` / `replay <trace>` / `sensors` -- learn from recorded runs
  (`sensors`: every sensor separately over a window).
- `heat-governor unit` -- a systemd user unit to install it.
- `heat-governor config ...` -- the file a running daemon re-reads.
- `heat-governor gpu-clock ...` -- the GPU clock ceiling (root helper).

## Agent verbs

- `whoami`, `learn`, `explain <path>`, `overview`, `doctor`, `cli overview`.

## Exit-code policy

- `0` success, `1` user-input error, `2` environment error, `3` `wait` timed
  out while held, `4+` reserved.

## See also

- `heat-governor explain run`, `explain state`, `explain wait`
- `docs/contracts/state.md`, `docs/integration.md`
"""

_WHOAMI = """\
# heat-governor whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    heat-governor whoami
    heat-governor whoami --json
"""

_LEARN = """\
# heat-governor learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    heat-governor learn
    heat-governor learn --json
"""

_EXPLAIN = """\
# heat-governor explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    heat-governor explain heat-governor
    heat-governor explain whoami
    heat-governor explain --json <path>
"""

_OVERVIEW = """\
# heat-governor overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the template carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    heat-governor overview
    heat-governor overview --json
"""

_DOCTOR = """\
# heat-governor doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`claude` → `CLAUDE.md`), plus a
skills-present check. Exits 1 when unhealthy.

prompt-file-present requires the *resident* prompt the declared backend
actually reads. Other harness prompt files recognized under the same backend
name (`AGENTS.override.md`, `.pi/SYSTEM.md`, `QWEN.md`) belong to
interactively available harnesses the mesh daemon never loads; they are
reported by the informational harness-prompts check and never substituted.

## Usage

    heat-governor doctor
    heat-governor doctor --json
"""

_CLI = """\
# heat-governor cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    heat-governor cli overview
    heat-governor cli overview --json
"""


_SENSE = """\
# heat-governor sense

Reads every thermal zone, the board's fans and the GPU once, and shows which
zones are watched on this platform. Read-only. A zone that cannot be read
(an idle Thor GPU returns EAGAIN, idle Orin DLA zones ENODATA) shows as
unreadable and does not count; only when every watched zone is unreadable is
the reading lost -- the controller then treats the host as hot.

    heat-governor sense [--platform spark|thor|orin|generic] [--json]
"""

_CAPABILITIES = """\
# heat-governor capabilities

What this host lets heat-governor read (zones, trip points, fans, nvidia-smi,
tegrastats) and change (docker pause and CPU caps, the admission signal), and
the active profile with the provenance of its numbers. Controls that exist but
are not driven (nvpmodel, fan PWM, GPU clock locks) are reported as such,
never simulated.

    heat-governor capabilities [--platform ...] [--config file.toml] [--json]
"""

_RUN = """\
# heat-governor run

The daemon. Each tick (default 1 s): read the watched zones; compute the
effective temperature (temperature + lookahead x rising rate); choose the
level with hysteresis and dwell; publish the state file (and `GET /state`
with `--listen`); record the tick; and with `--apply`:

- duty-cycle pause containers labelled `heat-governor.role=batch` (or named
  with `--batch`), fully paused at `critical`;
- cap CPUs of batch containers and of serving containers that opted in
  (`heat-governor.cpu-cap=true`, or `--serving NAME --serving-cpu-cap`).
  Drops are immediate; rises move one band per ramp interval.

- with `[[units]]` in the config, set a CPUQuota on those systemd user
  units (e.g. `app.slice`) by level (`systemctl --user set-property
  --runtime`; a reboot drops it). Off unless a unit is listed.

Serving containers are never paused. Only pauses this daemon made are ever
undone; a restart releases what its ledger recorded. On exit it unpauses its
own pauses and restores every CPU limit it changed.

    heat-governor run --once                 # one dry-run tick
    heat-governor run --apply --listen 127.0.0.1:9476
    heat-governor run --apply --serving model-gear-vllm-primary --serving-cpu-cap
"""

_STATE = """\
# heat-governor state

Reads the admission state a daemon published (local file by default, or
`--source http://host:port/state`). Missing or stale state is `unknown` and
admits unless `--if-unknown hold`. Contract: `docs/contracts/state.md`.

    heat-governor state [--source PATH|URL] [--json]
"""

_WAIT = """\
# heat-governor wait

Blocks until the host admits new work. Exit 0 when admitted, 3 when
`--timeout` passes while still held. For scripts: `heat-governor wait && job`.

    heat-governor wait [--source PATH|URL] [--timeout S] [--poll S] [--json]
"""

_REPORT = """\
# heat-governor report

Scans the records for restarts with a NEW kernel boot id (a reboot or hard
reset) and summarises the last `--window` seconds before each: the hottest
temperature, the last level, duty and per-zone readings.

    heat-governor report [--data-dir DIR] [--window 60] [--json]
"""

_REPLAY = """\
# heat-governor replay

Runs the controller over a recorded trace -- this daemon's records (.jsonl)
or a 1 s `t1s-*.log` telemetry file -- and reports time per level, the share
of time new work would be held and the mean batch duty. Use it to check a
profile change against real traces before it governs anything. Open-loop: it
cannot show how the temperature would have responded to the governing.

    heat-governor replay t1s-20261023.log --platform spark
"""

_UNIT = """\
# heat-governor unit

Prints a systemd user unit for the daemon; writes nothing. Flags after `--`
go to `run` (default `--apply`).

    heat-governor unit -- --apply --listen 127.0.0.1:9476
"""

_CONFIG = """\
# heat-governor config

One TOML file tunes everything: the temperature bands, the duty curve, CPU and
GPU clock bands, client concurrency per level, the poll interval and the
containers to govern. A running daemon re-reads it within ~2 s: a valid change
applies at once (logged with the keys that changed), an invalid one is
rejected and the last good configuration stays in force. Unknown keys are
errors, so a typo never silently leaves a default in place.

Path: `$HEAT_GOVERNOR_CONFIG`, else `~/.config/heat-governor/config.toml`.
Precedence: command-line flags > the file > the built-in platform profile.

    heat-governor config init --write   # a commented copy of the built-in profile
    heat-governor config check          # validate (exit 1 with every problem)
    heat-governor config show           # the effective configuration
    heat-governor config overview       # where it is and whether it is valid
    heat-governor config path

Sections: `platform`, `[profile]` (warm_c, hot_c, crit_c, hysteresis_c,
dwell_s, lookahead_s, rate_window_s, duty_curve, duty_period_s,
duty_decay_per_s, cpu_bands, ramp_s, gpu_clock_bands, concurrency, zones,
gpu), `[daemon]` (apply, poll_s, listen, state, data_dir, use_labels,
discover_every_s, record, fsync, reload, gpu_clock), `[[containers]]`
(name, role = batch|serving, cpu_cap) and `[[units]]` (name of a systemd
user unit, plus optional warm / hot / critical CPU counts; without them the
platform default: spark 8 / 4 / 2, declared, unmeasured). Units are off
unless listed; contract: `docs/contracts/user-units.md`.
"""

_GPU_CLOCK = """\
# heat-governor gpu-clock

The GPU clock ceiling: the one control that needs root. A small helper,
installed once per host by the operator, accepts only `lock <MHz>`, `reset`
and `status`; a sudoers rule allows exactly that helper without a password.
The daemon lowers the ceiling per `gpu_clock_bands` when `daemon.gpu_clock =
true` (or `run --gpu-clock`): drops at once, rises one band per `ramp_s`.
Every lock is written to the ledger first and removed on exit, on the next
start, and when gpu_clock is turned off.

DGX Spark: `nvidia-smi -lgc 0,<MHz>` / `-rgc`. Jetson: the GPU devfreq
nodes' `max_freq`, snapped to `available_frequencies`.

    heat-governor gpu-clock status          # available? bands in force
    heat-governor gpu-clock helper          # the script and how to install it
    heat-governor gpu-clock lock 2200       # dry run; --apply to lock
    heat-governor gpu-clock reset --apply

The bands are declared, not measured: what a lower ceiling does to the board
temperature on each platform is still to be measured.
"""

_SENSORS = """\
# heat-governor sensors

Every sensor separately, from the per-tick records, over a time window: each
thermal zone (`zone:<index>:<type>`), each GPU reading (`gpu:temp_c`,
`gpu:power_w`, `gpu:sm_mhz`, `gpu:util_pct`, `gpu:limit_c`, ...) and each fan
(`fan:<name>`), with samples, min, mean, p95, max and -- for temperatures --
the seconds at or above `--above`. Use it to tell results apart per sensor,
e.g. one GPU-ceiling step against another. The live state carries the same
names under `sensors` every tick.

    heat-governor sensors --since -30m --above 92
    heat-governor sensors --since 16:00 --until 16:10 --match zone --json
"""

ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("heat-governor",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("sense",): _SENSE,
    ("capabilities",): _CAPABILITIES,
    ("run",): _RUN,
    ("state",): _STATE,
    ("wait",): _WAIT,
    ("report",): _REPORT,
    ("replay",): _REPLAY,
    ("unit",): _UNIT,
    ("sensors",): _SENSORS,
    ("config",): _CONFIG,
    ("config", "overview"): _CONFIG,
    ("config", "path"): _CONFIG,
    ("config", "show"): _CONFIG,
    ("config", "init"): _CONFIG,
    ("config", "check"): _CONFIG,
    ("gpu-clock",): _GPU_CLOCK,
    ("gpu-clock", "overview"): _GPU_CLOCK,
    ("gpu-clock", "status"): _GPU_CLOCK,
    ("gpu-clock", "helper"): _GPU_CLOCK,
    ("gpu-clock", "lock"): _GPU_CLOCK,
    ("gpu-clock", "reset"): _GPU_CLOCK,
}

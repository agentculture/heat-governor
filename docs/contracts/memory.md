# Memory guard contract

The memory guard keeps a machine from thrashing into a reset when a process eats its RAM. It
watches available memory, kills the runaway (only your own user's processes), records who used
the memory, and keeps that record off the machine in an S3-compatible bucket. It was built after
the 2026-11-14 EliteMini reset: a test grew to about 41 GB five times in 17 minutes, the kernel's
OOM killer fired late twice, and the box thrashed for 16 minutes until it was reset
(`docs/specs/2026-11-14-memory-guard.md`).

It is off unless the config has a `[memory]` table. Shipping is off unless `[ship]` has
`enabled = true`. Neither touches the thermal path: `level`, `admit` and `concurrency_factor`
stay thermal-only.

## When it fires

A guard thread, separate from the 1 s tick, reads `/proc/meminfo` every `poll_s` (0.25 s). It
fires when available memory is:

- below the **floor**, or
- below the **soft limit** and projected to reach zero within `horizon_s` (10 s) at the current
  fill rate (a least-squares slope over `window_s`, 10 s).

A condition must hold on two consecutive samples. After a fire the trigger disarms until
available memory is back above the soft limit, so one wave gives one fire. The exception is a
fire that killed nothing because no victim was eligible or the kill was refused: the guard
re-arms and tries again, 1 s later, then 2, 4, 8 ... up to 60 s between tries. A kill or a
recovery resets that backoff, so memory eaten by something it may not kill (a root container)
gives about ten `memory_would_kill` rows in five minutes, not one per poll.

Memory pressure (PSI) is not used: on 2026-11-14 it stayed near 0 until less than 2 GB was
left, while the fill rate gave warning tens of seconds earlier.

### Limits

The first match wins:

1. `floor_mib` / `soft_mib` in `[memory]` (set together).
2. The platform profile's declared limits, for boxes that run full of AI work.
3. `floor_pct` / `soft_pct` of RAM (5% / 20%).

| Platform | Floor | Soft | Why |
|---|---|---|---|
| `spark` | 3072 MiB | 6144 MiB | 125 GB unified memory, 9.8-12.2 GB free at idle serving |
| `thor` | 3072 MiB | 6144 MiB | 126 GB unified memory, 10.5 GB free under AI load |
| `orin` | 2048 MiB | 4096 MiB | 63 GB, no swap, 8.3 GB free |
| `ventunoq` | 1024 MiB | 2048 MiB | 15 GB, no swap |
| `generic` (e.g. EliteMini) | 5% | 20% | percentages |

The platform limits are declared, not measured (2026-11-15 readings). `heat-governor memory
status` shows the effective limits and their source (`config`, `platform` or `percent`).

Measured on the 2026-11-14 waves with the generic defaults: the trigger fires 4-18 s before
available memory falls below 2 GB, with 5.4-7.1 GB still free, and 0 times across the
2026-11-10..13 records (deviation `d1`). Check other settings with
`heat-governor memory replay <records.jsonl> --total-mib N`.

## Who it kills

Only with `action = "kill"`. With the default `action = "record"` it writes `memory_would_kill`
and never signals anything.

- Only processes owned by the daemon's own user. Root and container processes (vLLM runs as
  another user) are never eligible. The guard refuses to choose anything when it runs as root.
- Never pid 1-2, the daemon itself or its parent chain, or a protected name: `systemd`, `init`,
  `sshd`, `sshd-session`, `login`, `gnome-shell`, `Xwayland`, `tailscaled`, `dockerd`,
  `containerd`, plus `protect` from the config.
- Sizes and growth use anonymous memory (`RssAnon`), so services with large file-backed
  mappings never look like runaways.
- The victim is chosen by growth, in this order:
  1. an eligible process **over its boundary** (see [Boundaries](#boundaries)), most MiB over
     first;
  2. otherwise the faster of the fastest-growing eligible process with at least
     `min_victim_mib` (2048 MiB) of anonymous memory and the fastest-growing process group
     below the session or systemd scope (many small workers), killing that group's biggest
     growing member;
  3. only when nothing is growing (a flat curve under the floor), the biggest eligible process
     of at least `min_victim_mib`.

  A big process that is not growing is never chosen while something else grows, and a process
  within its boundary is never chosen at all.
- The kill is pinned: the guard opens a pidfd, re-checks the start time and owner, and sends
  SIGKILL through the pidfd, so a recycled pid is never hit.
- At most `max_kills_per_hour` (3) kills per rolling hour. Past that it records
  `memory_would_kill` plus `memory_budget_exhausted` and does not kill.
- A second kill of the same program (owner, executable, command-line hash) within 30 minutes
  carries `repeat: N`, so a relaunch loop shows up as one repeating offender.

## Boundaries

`[[memory.allow]]` rules give known processes an acceptable size, so the guard blames the
process that is *out of bounds* rather than the one that is merely big:

```toml
[memory]
act_over_pct = 50                  # "far over" = more than max_mib x 1.5

[[memory.allow]]
name = "vllm"                      # unique label
cgroup = "*docker-*.scope"         # any of comm / cmdline / cgroup; all given must match
cmdline = "*vllm*serve*"
max_mib = 102400                   # >= 64
scope = "process"                  # "process" (default) or "total" across all matches
```

- `comm` is a list of exact process names (also matched against the argv[0] and executable
  basenames); `cmdline` and `cgroup` are shell-style globs. A process matching several rules
  falls under the tightest one (smallest `max_mib`).
- **Within** its boundary a process is never blamed and never chosen as a victim.
- **Over** it: it is blamed first and, if it is an eligible own-user process, it is the first
  victim when the trigger fires. The first crossing records `memory_boundary_exceeded` (once
  per process, reset after it falls 5% back under).
- **Far over** (`act_over_pct`): the guard acts even before system memory is low, on its
  routine process scan (every `top_every_s`, every poll while low). An eligible own-user
  process is killed (same protections, budget and pidfd path; `reason: "boundary"`); one the
  guard may not kill gets a single `memory_would_kill` with `why_not: "boundary: not killable
  (uid N)"`. Either happens once per process.
- `min_victim_mib` still applies, per process: a match using less anonymous memory than
  `min_victim_mib` is blamed and recorded but never killed, whatever the rule's `max_mib`.
  So a rule whose `max_mib` is below `min_victim_mib` only kills a match once that process
  has grown past `min_victim_mib`.

## Blame

Every fire event (`memory_kill`, `memory_would_kill`) and every `memory_boundary_exceeded`
carries `blamed`: up to three processes of **any** user (root, containers, vLLM included),
over their boundary first, then those that are growing (by anonymous growth). Processes that
are neither are not listed; only when nothing is over or growing (a flat curve under the
floor) does it name the biggest processes instead. Each entry has the pid, uid, process name,
redacted command line, anonymous size, growth, cgroup, the container id when the cgroup names
one (`docker-<id>`, 12 hex characters) and its boundary, if any. Blame is evidence only: the
kill still targets eligible own-user processes.

## What it records

Rows in the daemon's records (`<data_dir>/records/YYYYMMDD.jsonl`), each with an `event` key:

| Event | When |
|---|---|
| `memory_kill` | A process was killed: pid, parent chain, command line (redacted), anonymous size, growth, cgroup, `repeat`, `kills_last_hour` |
| `memory_would_kill` | The guard fired but did not kill (record-only, budget, or refused), with `why_not` |
| `memory_budget_exhausted` | The hourly kill budget is used up |
| `memory_boundary_exceeded` | A process (or a rule's total) first went over its `[[memory.allow]]` boundary: rule, usage, max, `blamed` |
| `oom_kill` | The kernel or a cgroup OOM-killed something (from `/proc/vmstat` and `memory.events`), even when the guard did not act |

Tick rows gain an optional `top` list (the biggest processes by anonymous memory), every
`top_every_s` and on every poll while under the soft limit. `heat-governor report` adds the
minimum free memory, swap and peak memory PSI before each reboot.

Command lines are truncated to 512 characters and redacted: values after token, key, secret,
password and auth-style flags, `KEY=VALUE` arguments, `Authorization` headers and known token
shapes are masked. Environment variables are never read.

## The state document

`GET /state` gains an optional `mem_guard` object while a guard runs; see
[`state.md`](state.md).

## Shipping to S3

With `[ship] enabled = true` the daemon uploads to any S3-compatible store (AWS S3, Cloudflare R2,
MinIO), signing requests itself (SigV4, no third-party library):

| Object | When |
|---|---|
| `<prefix>/<machine>/<boot_id>/events/<utc>-<kind>.json` | Each `memory_kill`, `memory_budget_exhausted`, `memory_boundary_exceeded` and `oom_kill`, immediately |
| `<prefix>/<machine>/<boot_id>/blackbox.jsonl.gz` | The last `blackbox_minutes` (10) of records, every `blackbox_every_s` (60 s), every 10 s while memory is low |
| `<prefix>/<machine>/<boot_id>/postmortem.json` | Once, on start after an unclean shutdown, under the **crashed** boot's id (next to that boot's events) |

- `machine` is `[ship] machine`, else the `transport = "local"` entry of `fleet.toml`. It is
  never the kernel hostname. Without either, shipping is disabled.
- Credentials come from `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, else the `profile` in
  `~/.aws/credentials` (keep it mode 600). They are never written to records, logs or uploads.
- `http://` endpoints are accepted only for loopback and tailnet (100.64.0.0/10) hosts; use
  `https://` for anything else.
- Nothing on the guard's thread waits on disk or the network: its events are handed to a
  writer thread (records, then the S3 spool) through a bounded queue.
- Uploads never block the guard: objects go to a spool directory first and are retried with
  backoff (timeouts, 5xx, clock skew after boot). Configuration errors (missing bucket, bad key)
  are logged once an hour.

Read a machine's evidence from anywhere with
`heat-governor ship fetch --machine <name> [--boot <id>]`.

## Obligations

These hold at the seams other code depends on:

1. **State doc:** `mem_guard` is optional and additive; `level`, `admit` and
   `concurrency_factor` are identical with or without it.
2. **Records:** every new row type carries an `event` key; tick rows gain only an optional `top`
   list; existing readers (`history.py`, `report()`) parse them unchanged.
3. **S3 layout:** the three keys above; `ship fetch` reads exactly this layout.
4. **S3 API:** path-style PUT/GET/ListObjectsV2 signed with SigV4, `x-amz-content-sha256` the
   real payload hash, region configurable (including R2's `auto`).
5. **Kill:** own user only, protected names honoured, pinned by pidfd, SIGKILL only, within the
   budget, recorded before the next poll.
6. **Config:** `[memory]` and `[ship]` are validated strictly, including on live reload.
7. **CLI:** `memory` and `ship` verbs take `--json`, write results to stdout and errors to
   stderr, and exit 0/1/2.
8. **Uploaded evidence:** no environment variables, no credentials, redacted and truncated
   command lines.

## Enabling it

```toml
[memory]
action = "kill"          # default "record": never kills
# floor_mib = 3072       # optional; otherwise the platform's limits or the percentages
# soft_mib = 6144

[ship]
enabled = true
endpoint = "http://<spark-tailnet-ip>:9000"   # MinIO on the tailnet; https for R2/AWS
bucket = "heat-governor"
profile = "heat-governor"                     # a [heat-governor] profile in ~/.aws/credentials
machine = "elitemini"
```

`heat-governor ship test` checks the bucket and credentials with a probe object.

## Keeping the guard itself alive

The daemon locks the memory it has resident at start (`mlockall(MCL_CURRENT)`: the interpreter,
its modules, the guard's code) and the systemd unit sets `MemoryMin=128M`, so the guard keeps
running under memory pressure. It does not lock later allocations (thread stacks, upload
buffers), which would pin RAM the guard is trying to free.

## Host containment

The guard is a backstop. For heavy jobs, a per-command memory limit contains a runaway before
the guard has to act:

```bash
systemd-run --user --scope -p MemoryMax=30G -- <command>
```

On the EliteMini, re-runs of the 2026-11-14 test under such a scope were killed inside the scope
with the rest of the box untouched. Machine-wide limits (`MemoryMax` on `user-1000.slice`,
systemd-oomd for SSH session scopes, a hardware watchdog) are host configuration, outside this
repo.

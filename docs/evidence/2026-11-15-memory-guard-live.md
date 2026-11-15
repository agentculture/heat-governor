# Memory guard live validation (EliteMini, 2026-11-15)

First run of the memory guard and the S3 shipper on a real machine: heat-governor 0.17.0 (PR #14
head) on the EliteMini (AMD Ryzen 5 7640HS, 45.9 GB RAM, 4 GB swap, `generic` platform), shipping to
a MinIO container on spark. Plan tasks t12 (deploy, EliteMini only) and t13 (validation).

## Setup

- **MinIO on spark:** `minio/minio` RELEASE.2025-09-07 in docker, S3 port published only on
  spark's tailnet address (`100.127.105.72:9000`). The console is not published. Root
  credentials are in a mode-600 file on spark only.
- **Bucket and key:** bucket `heat-governor`. Policy `heat-governor-rw` allows ListBucket and
  GetBucketLocation on the bucket, and GetObject and PutObject on its objects. User `hg57ca…` is
  attached to that policy.
- **EliteMini credentials:** a `[heat-governor]` profile in `~/.aws/credentials` (mode 600,
  backed up first).
- **EliteMini config:** `[memory] action = "kill"` (generic platform, so percentage limits:
  floor 2296 MiB, soft 9187 MiB) and `[ship]` to the MinIO endpoint, `machine = "elitemini"`.
- **Unit:** a `MemoryMin=128M` drop-in. Daemon log at start: `mlockall: ok`,
  `memory guard: on action=kill ...`, `ship: on, to http://100.127.105.72:9000 ...`.

## Results

| Check | Target | Result |
|---|---|---|
| `ship test` (PUT and GET a probe object) | works | ok, 31.9 ms |
| Scoped key outside its bucket | refused | PUT and LIST on another bucket: `403 AccessDenied` |
| Black box shipped after start | present | `fleet/elitemini/<boot>/blackbox.jsonl.gz` |
| Controlled runaway killed by the guard (c26) | killed with >= 2 GB available | killed at 8748 MiB available (min 1 Hz sample 9794 MiB) |
| Governor tick gap during the run (c26) | <= 2 s | max 1.0 s (39 ticks) |
| Kernel OOM killer involved | no | no OOM lines in the kernel log |
| `memory_kill` event in S3 (c26) | within 10 s of the event | same second (event 04:35:48.315Z, object 04:35:48Z) |
| `ship fetch` from another machine (spark) | prints victim and black box | 1 `memory_kill` (pid 918094 `python3`), black box 610 rows covering the 10 min before |
| Quiet days: `memory replay` over 2026-11-10..13 records (c27, d1) | 0 fires | 0 fires over 85948 / 86390 / 86391 / 86391 samples |
| Incident: `memory replay` over the 2026-11-14 fixture | 1 fire per wave | 5 fires, 4-18 s before 2048 MiB, 5.4-7.1 GB available |

### The runaway

`python3` allocated anonymous memory at about 1.2 GB/s, touching every page, inside
`systemd-run --user --scope -p MemoryMax=36G -p MemorySwapMax=0` as a safety net. With 40.5 GB
available, a cap below about 31 GB would have stopped the allocator before the guard could see it.
The guard fired on the horizon rule: 1232 MiB/s with 8748 MiB left. It killed the allocator at
31748 MiB of anonymous memory, about 5 GB below the cgroup cap:

```json
{"event": "memory_kill", "reason": "horizon", "pid": 918094, "comm": "python3",
 "rss_anon_mib": 31748.25, "growth_mib_s": 1222.6, "avail_mib": 8748.2, "rate_mib_s": 1232.3,
 "repeat": 1, "signal": "SIGKILL", "kills_last_hour": 1,
 "parents": ["bash", "claude", "bash", "sshd"]}
```

The recorded parent chain names what started the process: a Claude Code session over SSH. On
2026-11-14 this attribution was only recoverable from the kernel's OOM tables.

## Fleet rollout (same day, 05:10-05:12 UTC)

The other five boxes were enabled the same way: the 0.17.0 wheel from the PR head, `[memory]
action = "kill"` with each platform's declared limits, `[ship]`, a `MemoryMin=128M` drop-in and a
unit restart. Every box passed `config check`, logged `mlockall: ok` and passed `ship test`. No box
ships to itself: spark ships to a second MinIO on arduino (`100.97.170.7:9000`, same image, same
bucket and scoped policy), and everyone else ships to spark.

| Box (machine name) | Limits (floor / soft) | Free at start | Ships to |
|---|---|---|---|
| thor | 3072 / 6144 MiB (platform) | 10567 MiB | spark |
| spark2 | 3072 / 6144 MiB (platform) | 9663 MiB | spark |
| orin | 2048 / 4096 MiB (platform) | 8150 MiB | spark |
| arduino (`cultureos`) | 1024 / 2048 MiB (platform) | 6054 MiB | spark |
| spark | 3072 / 6144 MiB (platform) | 11861 MiB | arduino |
| elitemini | 2296 / 9187 MiB (percent) | 40574 MiB | spark |

Afterwards the fleet view reported all six `ok` and admitting work. Spark's store held black
boxes from cultureos, elitemini, orin, spark2 and thor; arduino's held spark's.

## After the code review (06:14-06:17 UTC)

The review fixes and the blame/boundaries change (deviation d4) were redeployed to all six boxes:
every box restarted on the PR head, logged `mlockall: ok (current pages)` and passed `ship test`.
Locked address space fell from 420-590 MB to 264-432 MB per daemon (resident memory 74-112 MB).
The real pinned RAM was always close to the resident size; the earlier figure counted reserved
address space.

The controlled runaway on the EliteMini, re-run on this build: killed on the horizon rule at
8769 MiB available (min 1 Hz sample 9599 MiB), max tick gap 1.0 s, no kernel OOM, victim chosen
by growth (`python3`, 1229 MiB/s). Its `blamed` list then also named two idle processes
(`claude`, `heat-governor`) to fill three slots; blame now lists only processes that are over
a boundary or growing.

## Not covered here

- The runaway test ran on the EliteMini only. On the five AI boxes the platform limits (deviation
  d3) are declared from 2026-11-15 readings, not measured under a runaway.
- The postmortem path (upload after an unclean boot) has not fired for real; there was no unclean
  boot after the deploy.
- No real kill of a multi-process (group fallback) runaway yet.

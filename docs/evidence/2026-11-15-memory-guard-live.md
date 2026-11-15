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

## Not covered here

- Only the EliteMini runs the guard. spark, spark2, thor, orin and the VENTUNO Q still run 0.16
  without `[memory]`. Their platform limits (deviation d3) are declared from 2026-11-15 readings,
  not measured under a runaway.
- The postmortem path (upload after an unclean boot) has not fired for real; there was no unclean
  boot after the deploy.
- No real kill of a multi-process (group fallback) runaway yet.
- spark's own daemon needs a ship target other than spark (risk r3).

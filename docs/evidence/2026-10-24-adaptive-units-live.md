# Adaptive user-unit CPU cap: live runs on four boxes

2026-10-24, 16:22-17:01Z, on the DGX Spark, spark2, Jetson AGX Thor and Orin.

## Method

- **Load:** a CPU burner on half the cores (`python` busy loops), run in a
  throwaway `hg-test.slice` as a `systemd-run --user` scope, with
  `RuntimeMaxSec=900`.
- **Second daemon:** a second heat-governor ran from the PR build beside the
  production one, with `--no-docker` and `--no-gpu-clock`. It used its own data
  dir and state, and governed only that slice:
  `mode = "adaptive"`, `interval_s = 20`, `min_cpus = 1`. Warm was set 1.5 C
  above the box's temperature at start, so the burner alone engaged it.
- **Phases:**
  1. 6 min engaged.
  2. `kill -9` the daemon, then restart it (recover and model reload).
  3. 2 min engaged.
  4. Burner off, then 5 min cooling.
  5. SIGTERM, checking the quota is restored and the ledger is empty.

## Results

| Box | Burner | Learned gain (C/s per CPU) | Samples | Noise band (C/s) | kill -9 -> restart | Exit |
|---|---|---|---|---|---|---|
| DGX Spark | 10 procs | 0.10 -> 0.14 -> 0.21 | 3 (1 ignored) | 0.03-0.24 | recovered, model kept | quota none, ledger empty |
| spark2 | 10 procs | 0.071 | 1 | about 0.07 | recovered | quota none, ledger empty |
| Thor | 7 procs | 0.068 -> 0.140 | 6 | 0.016-0.142 | recovered, model kept | quota none, ledger empty |
| Orin | 6 procs | 0.018 -> 0.037 | 7 | 0.009-0.018 | recovered, model kept | quota none, ledger empty |

## What the first build got wrong, and the fixes

The first build used a fixed deadband and one rate reading per interval. Each
problem below was found on a live box.

| Seen live | Cause | Fix |
|---|---|---|
| Tighten and release every 20 s (Thor) | A fixed 0.005 C/s band, against about +-0.1 C/s of 20 s trend noise | Band = 2 x the slope's standard error |
| Gain stuck at the 0.05 clamp (Thor) | Samples learned from noise | A sample must exceed the combined noise; clamp widened to 0.5 |
| An idle slice kept being cut (Thor, Orin) | Heat from elsewhere read as the unit's | Use under 0.5 CPU: never cut, release |
| A slow creep never acted on (Orin, 61.2 -> 63.2 C in 6 min) | Inside each 20 s band | A long window (4 intervals) is checked when an interval is inside its band |
| A rate taken from 2 s windows | The controller's `rate_c_s` | A least-squares trend over the whole interval |

## Limits

- **Only after the fixes.** The long window and the board/GPU source switch
  were exercised on synthetic data only, so the final build's live run is the
  check.
- **Not the real slice.** The live runs used a test slice, not `app.slice`.
- **Untuned constants.** The cold-start step (25% of use) and the release step
  (10% of host CPUs) are design values.

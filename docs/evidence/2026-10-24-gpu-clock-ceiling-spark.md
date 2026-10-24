# GPU clock ceiling on the DGX Spark: an operator ceiling of 2200 MHz

Measured 2026-10-24, 15:31-16:43Z, on the DGX Spark (GB10), kernel 7.0.0-1019,
driver 580.178.04, BIOS 0ACUM028. Decision (operator, 2026-10-24): both Sparks run with an **operator GPU ceiling of
2200 MHz** (`[daemon] gpu_ceiling_mhz = 2200`, law: the governor may go lower for
heat, never higher). The lower `gpu_clock_bands` steps (1800 / 1400 MHz) apply
only above the warm threshold.

## Method

- **Workload:** the live cortex lane (Qwen3.8-27B NVFP4, DSpark speculation
  n=7, vLLM 0.31.0, `max_num_seqs=2`) serving LiveCodeBench prompts. Settings:
  temperature 1.0, thinking on, 2 concurrent streams, `max_tokens` 6144, and
  fixed seeds, so every setting saw the same prompts in the same order.
- **Settings:** 12 minutes each, in order: default, 2200, 1800, 1400, default
  again (to measure drift), and a 3003 ceiling. Each ceiling was set with
  `heat-governor-gpuclock lock`, which runs `nvidia-smi -lgc 0,<MHz>`.
- **Measurements:**
  - throughput comes from vLLM's own `generation_tokens_total` counter;
  - temperatures, GPU power and clock come from the 1 s telemetry;
  - the first 3 minutes of each setting are excluded as warm-up;
  - tokens per joule counts GPU power only.

## Result

| Ceiling (MHz) | SM clock | tok/s | GPU W | board z0 avg / p95 / max (C) | tok/J (GPU) |
|---|---|---|---|---|---|
| none (default) | 2449 | 38.2 | 37.5 | 82.1 / 85.8 / 91.8 | 1.02 |
| **2200** | 2189 | **38.1** | **24.2** | 79.8 / 84.6 / 90.6 | **1.57** |
| 1800 | 1781 | 36.5 | 17.0 | 76.5 / 85.3 / 90.6 | 2.15 |
| 1400 | 1392 | 34.9 | 14.1 | 78.0 / 87.1 / 91.1 | 2.48 |
| none (again) | 2433 | 37.3 | 38.2 | 86.2 / 88.0 / 92.4 | 0.98 |
| 3003 | 2450 | 37.0 | 36.8 | 82.0 / 84.7 / 86.2 | 1.01 |

Speculative acceptance was the same in every setting: 21.8-22.3%, mean
acceptance length 2.53-2.56. The ceiling changes the clock, not the work.

## Why 2200

- **No throughput cost.** At 2200 MHz throughput is 38.1 against 38.2 tok/s by
  default; the two default runs differ by more than that (38.2 and 37.3).
  Decode on this lane is limited by memory bandwidth, not by GPU clock.
- **A third less GPU power.** 24.2 W against 37.5 W, which is 55% more tokens
  per joule. Less power is less heat on a box that hard-resets at about
  94.5-95 C on the board zones.
- **The board temperature responds less than the power.** The board zones also
  follow CPU load: parallel interactive sessions moved the load average between
  4 and 9 across the settings, and the two default runs differ by 4 C. So the
  temperature columns carry about +-4 C of noise; the power column is the clean
  measure.
- **Lower steps cost throughput.** 1800 MHz costs about 4% and 1400 MHz about
  9%. They remain as heat-driven steps above the warm threshold, not as the
  default.
- **No headroom above the default without root.** A 3003 MHz ceiling left the
  clock at about 2450 MHz, the application clock. A higher minimum clock needs
  `nvidia-smi -lgc <min>,<max>`, which the helper does not offer, and is not
  expected to help a memory-bound decode.

## Prompt-heavy work (spark2, same hardware and BIOS)

`lobes benchmark` (llama-benchy 0.4.0) on spark2's cortex lane, driven from the
Spark through an ssh tunnel: pp 2048 / tg 128 on top of 0, 32k and 128k tokens
of context, 1 stream, 3 runs after 1 warmup per depth. GPU power is the mean
over busy seconds (above 20 W) from spark2's 1 s telemetry.

| Ceiling | pp tok/s at 32k | pp tok/s at 128k | tg tok/s at 128k | GPU W (busy) | board z0 max (C) |
|---|---|---|---|---|---|
| none (2436 MHz) | 2005 | 1188 | 14.5 | 82.8 | 95.0 |
| 2200 | 1941 (-3%) | 1121 (-6%) | 17.3 | 56.9 | 85.2 |
| 1800 | 1749 (-13%) | 970 (-18%) | 15.6 | 37.2 | 73.4 |

Prefill is compute-bound, so it is the workload a lower clock costs most. Even
so, 2200 MHz reads long prompts only 3-6% slower, for 31% less GPU power. At
the default clock, long prefill took the board zone to 95.0 C, the level at
which the Spark hard-resets. The depth-0 cells are excluded: the first setting
started cold.

## Fine-tuning (the Spark)

The `unsloth-cli` LoRA ran on LiquidAI/LFM2.5-1.2B-Instruct, the `hand` base,
with the `preset:lfm2` targets and r=16. Each run was 300 steps at batch 16 with
2048-token sequences, over the 532-row demo corpus. All runs used one
long-lived NGC `pytorch:25.11` container, and the cortex lane served no outside
traffic during any of them. The runtime is the trainer's own `train_runtime`.

| Ceiling | train_runtime | samples/s | GPU W (busy) | GPU / board z0 max (C) |
|---|---|---|---|---|
| none (2421 MHz) | 60.1 s | 79.8 | 63.5 | 75 / 81.9 |
| 2200 | 57.8 s | 83.0 | 47.3 | 70 / 76.5 |
| 1800 | 63.9 s | 75.2 | 30.8 | 64 / 74.7 |
| none, repeated last | 54.8 s | 87.7 | 67.4 | 78 / 86.9 |

The final loss was identical in all four runs (0.6785), so the runs are
deterministic and only the speed differs.

The two default runs differ by 5.3 s (9%). That noise includes an order effect:
later runs reuse compiled kernels and caches. So the cost of 2200 MHz for this
fine-tune is **between 0 and about 5%**, for about 28% less GPU power. 1800 MHz
costs about 11%, which is beyond the noise.

## Limits of this measurement

- **Workloads covered:** decode at 2 streams, prefill up to 128k context, and a
  small-model LoRA fine-tune. Not measured: vision input, the speech-to-text
  lane, and larger fine-tunes (4B/9B), which keep the GPU busier and may lose
  more at 2200 MHz.
- **Small samples.** Each setting covered 6-8 requests (about 27,000 generated
  tokens), so throughput differences under about 3% are not resolved.
- **Two boxes.** The decode and fine-tune sweeps ran on the Spark, and the
  prefill sweep on spark2. The two have the same hardware, BIOS 0ACUM028 and
  driver 580.178.04.
- **Fine-tune noise.** The clock settings were run in order rather than
  interleaved, so the 0-5% range for fine-tuning is the honest resolution.

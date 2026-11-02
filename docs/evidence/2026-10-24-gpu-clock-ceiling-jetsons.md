# GPU clock ceilings on the Jetson AGX Thor and Orin

Measured 2026-10-24. Decisions (operator):

- **Thor:** no default ceiling. The declared heat steps stay.
- **Orin:** no fixed ceiling. The governor's GPU clock control stays on, with
  **measured** heat steps: 1122 MHz at warm (85 C) and 918 MHz at 88 C.

## Method

- **Settings:** each ceiling was set with `heat-governor-gpuclock lock <MHz>`,
  which writes devfreq `max_freq` (the nearest available step at or below the
  value) on every GPU devfreq node. Each setting ran for 10 minutes.
- **Samples:** every 2 s, the live governor's state supplied the governing
  temperature, zones and GPU clock, and the INA3221 rails supplied power.
- **Warm-up:** the first 2 minutes of each setting are excluded.

**Workloads:**

- **Thor:** the live worker lane, `nvidia/Qwen3.6-35B-A3B-NVFP4` with DFlash
  n=12 and `max_num_seqs=1`, serving the 60 LiveCodeBench prompts at temperature
  1.0, thinking on, 1 stream, `max_tokens` 6144.
- **Orin:** the live decider sidecar, `culture-dev/clef-27b-GPTQ-W4A16-g64`,
  answering one fixed 3-question `/v1/systemone` request repeatedly, 2
  concurrent. The work is mostly prefill, with a short answer.

## Thor (worker decode)

| Ceiling (MHz) | tok/s | per request | VDD_GPU | VDD_CPU_SOC_MSS | temp avg / max (C) |
|---|---|---|---|---|---|
| none (1575) | 98.4 | 100.1 | 29.0 W | 18.0 W | 69.9 / 74.8 |
| 1300 | 95.0 | 94.3 | 28.1 W | 17.7 W | 75.6 / 76.5 |
| 1100 | 93.6 | 91.2 | 26.9 W | 17.4 W | 76.0 / 78.5 |
| 900 | 83.2 | 86.2 | 24.3 W | 16.3 W | 74.3 / 75.7 |
| none (again) | 95.6 | 99.0 | 29.9 W | 18.2 W | 77.7 / 78.3 |

A lower clock saves only 1-5 W on the GPU rail and costs 3-15% throughput. The
temperature rose steadily over the run, with the room and not with the clock.
Thor gains nothing from a default ceiling.

## Orin (decider)

The first sweep is excluded. Orin's live governor had `gpu_clock = true`, and at
the warm level it wrote its own 1100 MHz step, which devfreq rounded down to
1020 MHz. That overrode the hand ceilings for the whole sweep.

That incident is why the operator's ceiling is now law (see
`docs/contracts/gpu-clock.md`). It also showed that 1100 is not an Orin devfreq
step: the available steps are 306, 408, 510, 612, 714, 816, 918, 1020, 1122,
1224 and 1300.5 MHz.

The sweep was rerun with the governor's GPU control off, restored afterwards.
The rerun stopped after the full-clock setting because the board passed 90 C.
The remaining settings ran next.

| Ceiling (MHz) | req/s | latency median | VDD_GPU_SOC | VIN_SYS_5V0 | temp avg / max (C) |
|---|---|---|---|---|---|
| none (1300) | 0.886 | 2.26 s | 54.0 W | 12.4 W | 88.8 / **94.1** |
| 1122 | 0.780 (-12%) | 2.56 s | 40.3 W | 11.5 W | 80.1 / 83.6 |
| 918 | 0.647 (-27%) | 3.09 s | 29.3 W | 10.7 W | 78.7 / 80.2 |
| 714 | 0.511 (-42%) | 3.91 s | 21.5 W | 9.8 W | 74.0 / 75.3 |

- **The decider is compute-bound.** Throughput falls almost in step with the
  clock.
- **Full clock is unsafe under sustained load.** It passed the 94 C critical
  level.
- **1122 MHz is the efficient step.** It costs 12% of the request rate for 25%
  less GPU rail power, and the board stays under warm.
- **The steps follow from that.** 1122 MHz applies at warm and 918 MHz at 88 C.
  Real decider traffic is bursty, so the full clock stays available when the
  board is cool.

## Limits

- **Samples.** There was one 10-minute run per setting, and Thor's runs were
  single-stream.
- **Untested Thor work.** Thor's prefill-heavy work and its senses (vision)
  lane were not measured.
- **Thor drift.** The room warmed during Thor's sweep, and the two default runs
  differ by 3%.

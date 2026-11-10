# VENTUNO Q limits under CPU, GPU and NPU load (2026-11-10)

Handoff open item 4 (`docs/HANDOFF-2026-11-09.md`): measure the VENTUNO Q's limits
under CPU, GPU and NPU load before cultureos leaves dry-run (decision q3).

Measured on cultureos (Arduino VENTUNO Q, Qualcomm QCS8300, kernel
6.8.0-1084-qcom) on 2026-11-10, in two runs:

1. **Synthetic loads** (16:34Z–17:24Z): CPU, GPU and NPU burns, each alone and
   then all three together.
2. **Serving soak** (19:43Z–20:50Z): Gemma 4 E4B, the largest model Arduino
   fits to this board, served by Arduino's own runner and loaded with steady
   requests. Full CPU and GPU load ran alongside it for 45 minutes.

The heat-governor 0.16.0 daemon ran in dry-run (`apply = false`) throughout,
recording what it would have done. The only other workload was the idle
`model-gear-gateway` container. Room temperature was not measured.

## Result

**Under every load we could produce, the board stays far below its limits.** No
kernel throttling happened, no throughput was lost, and the fan never left its idle
state.

| | Value |
|---|---|
| Worst case (CPU + GPU + NPU, 15 min) | 71.0 °C peak (NSP), 68.4 °C in the last minute, 28.3 W board peak |
| Long soak (E4B serving + CPU + GPU, 45 min) | 69.9 °C peak, levels off at about 65 °C after 10 min, 29.2 W peak |
| Serving under full load | E4B decode 10.2 tok/s (9.6 served alone), prefill about 348 tok/s; no thermal loss |
| Lowest SoC trip (GPU/NPU passive) | 115 °C, so **44 °C of margin** at the peak |
| Declared `ventunoq` levels | warm 94 / hot 98 / crit 102 °C |
| heat-governor's decision | `cool` on every tick; max effective temperature 74.1 °C (with lookahead) |
| Kernel throttling | none: every non-fan cooling device stayed at 0, cpufreq and GPU devfreq maximums never dropped |
| Fan (`emc2305`, kernel-driven) | stayed at cooling state 5/10 (≈3300 rpm) from idle to worst case |

The declared levels sit roughly 20 °C above the worst case measured here and
about 20 °C below the first passive trip. With `apply = true` the governor would
not have acted at all during either run.

## Run 1: synthetic loads

### Method

- **Sampler.** It ran at 1 Hz and recorded:
  - every thermal zone;
  - cpufreq current and maximum per policy;
  - each cooling device's state;
  - `emc2305` fan rpm, GPU devfreq current and maximum, and `ina232` board power;
  - CPU utilisation.

  A watchdog would have killed all load if a SoC zone reached 110 °C or a PMIC
  zone 105 °C. It never fired.
- **Phases.**
  - A 3 min idle baseline.
  - 10 min each of CPU, GPU and NPU load.
  - 15 min of all three together.
  - Between phases, the board cooled until the hottest SoC zone was within 4 °C of
    baseline (46.6 °C).
- **Loads**, from `scripts/measure/`:
  - **CPU:** `burn_cpu.py`, numpy float32 2048² matmul (OpenBLAS) on all 8 cores.
  - **GPU:** `burn_gpu.py`, a wgpu compute shader running a vec4 FMA loop on the
    Adreno 623 through Mesa turnip (Vulkan).
  - **NPU:** `burn_npu.sh` runs `llama-bench` prefill (pp2048, 20 repetitions per
    model load) on Hexagon HTP0 (v75). It uses Arduino's own
    `app-bricks/llamacpp-npu-runner:0.13.2` image with the flags of its
    `service_compose.yaml` (cpuset 0–3, 4 threads, `/dev/fastrpc-cdsp`). The model
    is Gemma 4 E2B QAT q4_0, the GGUF Arduino's model catalog lists for the
    VENTUNO Q. Model reloads keep the NPU at roughly 85% duty.

  That catalog entry (`models-list.yaml`, `llamacpp:gemma-4-E2B_q4_0-it`) is marked
  `supported_boards: ["ventunoq"]` with `GGML_HEXAGON_DEVICES: 1`. The file is the
  pinned revision it names. The weights are Google's QAT q4_0 GGUF. Arduino's
  adaptation is the quant choice, the Hexagon v75 build of llama.cpp and the
  session count, not a re-conversion.
- **Analysis:** `analyze.py <run-dir> <records.jsonl>`. "Steady" is the mean of the
  last 3 minutes of a phase.

### Measurements

| Phase | min | soc peak | soc steady | cpu | cpuss | gpu | nsp | ddr | pmic | W avg | W peak |
|---|---|---|---|---|---|---|---|---|---|---|---|
| idle | 3.0 | 49.6 | 46.8 | 49.6 | 48.1 | 48.5 | 47.2 | 47.6 | 51.4 | 12.1 | 13.6 |
| CPU | 10.1 | 53.7 | 52.3 | 53.7 | 52.9 | 51.1 | 49.4 | 50.2 | 52.4 | 15.9 | 17.4 |
| GPU | 10.0 | 51.4 | 48.8 | 51.1 | 49.2 | 51.4 | 48.3 | 47.9 | 51.8 | 13.9 | 18.0 |
| NPU | 10.1 | 62.1 | 58.1 | 56.7 | 55.4 | 54.0 | 62.1 | 55.4 | 55.8 | 19.4 | 22.4 |
| all | 15.1 | 71.0 | 67.9 | 67.2 | 65.8 | 66.3 | 71.0 | 64.7 | 62.7 | 24.7 | 28.3 |

Temperatures are in °C, and each block column is that block's peak.

Throughput, first minute against last minute of each phase:

| Phase | Throughput | Steady clocks |
|---|---|---|
| CPU | 50.1 → 50.6 GFLOPS (+0.9%) | 2100 / 2352 / 1946 MHz (hw max 2112 / 2362 / 1958) |
| GPU | 144.7 → 145.0 GFLOPS (+0.2%) | 875 MHz (max 877) |
| NPU | 696.2 → 695.0 tok/s pp2048 (−0.2%) | — |
| all | CPU 45.7 → 44.6, GPU 144.9 → 145.2, NPU 670.6 → 672.7 | 2100 / 2340 / 1949 MHz, GPU 870 MHz |

In the combined phase the CPU gives up about 10% and the NPU about 4%. That is
contention, not heat: the NPU runner's 4 host threads share cores 0–3 with the
CPU burn, and no clock was capped.

The hottest zone in the combined phase, minute by minute:

```text
min   0    2    4    6    8   10   12   14
soc 61.2 64.3 66.4 66.2 67.1 67.1 67.5 68.4
pmic 55.9 58.9 60.3 61.1 61.7 61.8 62.1 62.4
```

## Run 2: serving an Arduino-fitted model

### Method

- **Server.** It is Arduino's `llamacpp-npu-runner:0.13.2` started through its own
  entrypoint (`run-model-router.sh`), with every environment variable, limit,
  device and mount from `service_compose.yaml`. Only the models directory and the
  host port changed. The runner sized the model itself:

  ```text
  gemma-4-E4B_q4_0-it: 2696 MiB on the NPU + 356 MiB of f16 KV cache + 614 MiB of
  compute buffers -> 2213 MiB per session, requires 2 sessions
  ```

  That matches the catalog's `GGML_HEXAGON_DEVICES: 2` for this model. The context
  is 16384, reasoning is off, and the host side is 4 threads on cores 0–3.
- **Client.** `serve_client.py` sends one request at a time to
  `/v1/chat/completions`, back to back, and records llama.cpp's own timings. Each
  request has about 960 prompt tokens and asks for 256 out. Each prompt is unique,
  so the prompt cache never helps.
- **Phases** (`run_serve.sh`):
  - 2 min idle;
  - 15 min of serving alone;
  - 45 min of serving plus `burn_cpu.py` (all 8 cores) and `burn_gpu.py`;
  - then a cool-down.
- **Smoke test.** A 1 + 1 min smoke test with Gemma 4 E2B (1 session) ran first.

### Measurements

| Phase | min | soc peak | soc steady | cpu | gpu | nsp | ddr | pmic | W avg | W peak |
|---|---|---|---|---|---|---|---|---|---|---|
| idle | 2.1 | 44.8 | 42.5 | 44.8 | 43.3 | 43.1 | 43.1 | 46.8 | 11.7 | 13.9 |
| E4B serving | 15.0 | 60.9 | 56.3 | 55.6 | 52.9 | 60.9 | 53.5 | 54.1 | 18.8 | 23.2 |
| E4B serving + CPU + GPU | 45.0 | 69.9 | 65.2 | 65.3 | 64.4 | 69.9 | 62.4 | 60.7 | 23.9 | 29.2 |

| Model, phase | Requests | Prompt tok/s | Decode tok/s, first 3 min → last 3 min | Per request |
|---|---|---|---|---|
| E2B, serving (smoke) | 4 | 636 | 21.4 | about 13.5 s |
| E2B, + CPU + GPU (smoke) | 3 | 620 | 20.7 | about 13.9 s |
| E4B, serving | 29, 0 errors | 352 → 354 | 9.6 → 9.6 | 29.7 s |
| E4B, + CPU + GPU | 96, 1 error | 347 → 349 | 10.1 → 10.2 | 28.1 s |

The hottest zone and the PMIC in the 45-minute loaded phase, in 5-minute steps:

```text
min   0    5   10   15   20   25   30   35   40
soc 60.1 63.9 65.3 65.2 65.0 65.6 65.4 65.2 65.5
pmic 55.2 59.3 60.3 60.4 60.2 60.6 60.5 60.4 60.4
```

- **The board levels off.** After about 10 minutes the hottest zone holds at about
  65 °C and the PMIC at about 60 °C for the remaining 35 minutes. Run 1's slow rise
  at the end of its 15-minute phase was this settling.
- **Serving keeps its speed under full load.** E4B's decode did not drop when every
  CPU core and the GPU were busy. It rose by about 6%, from 9.6 to 10.2 tok/s. The
  likely cause is clock speed: with the CPU idle, schedutil kept cores 0–3 near
  1.2–1.5 GHz, and under load they ran at their 2.1–2.4 GHz maximum. That speeds up
  the host-side work of each decode step. E2B lost about 4% in the smoke test.
- **The CPU burn lost 11.6% over the loaded phase** (39.6 → 35.1 GFLOPS). The
  clocks sat at their hardware maximum and no cooling device engaged, so this was
  not thermal. It is contention with the server's 4 host threads on cores 0–3. Why
  it grew over the phase was not established.
- **One request failed** with HTTP 500, 18 minutes into the loaded phase, in
  2.9 s. The next request succeeded, and 125 of the 126 requests in the run completed.
  The router logged `http client error: Failed to read connection` at that moment:
  it lost its connection to the model's child server for one request. That is a
  proxy failure, not a model or NPU error.
- **E4B loads without pipeline parallelism.** At startup the runner could not
  allocate a 343 MB compute buffer on HTP0 (`rpcmem_alloc failed`) and fell back to
  `retrying without pipeline parallelism`. Every E4B figure here is for that
  fallback. That is how Arduino's own configuration runs it on this board, but a
  smaller context or batch might let pipeline parallelism through and run faster.

## What it means for heat-governor

- **The NPU is the hot block.** On its own it raises the NSP zones 11 °C, against
  5 °C for all 8 CPU cores and 2 °C for the GPU. It also draws the most extra power,
  about 7 W against about 4 W for the CPU. Serving E4B on 2 sessions behaves the
  same way: NSP leads every phase.
- **The board can serve an LLM and still perform.** E4B served at full speed for
  45 minutes with every other block saturated, and the board levelled off about
  50 °C under its first passive trip.
- **CPU caps have little to work on here.** A full CPU load adds only about 5 °C. If
  this board ever runs hot, the lever that matters is pausing NPU work (a `batch`
  container). Today both containers on cultureos are `serving`, so they are never
  paused.
- **The declared profile is safe but unexercised.** The 94/98/102 °C levels were
  never approached, so this run shows no thermal reason against `apply = true`. It
  also shows no evidence that `apply = true` would do anything in this room.

## Limits

- **One run, one room.** Room temperature was not recorded. A warm room, an
  enclosure, or a fan failure would shift every number.
- **Settling (answered by run 2).** Run 1's combined phase was still rising
  about 0.3 °C/min when it ended. Run 2's 45-minute soak levelled off after about
  10 minutes.
- **Workloads not tried.** The GPU burn is ALU-only, with no memory traffic or real
  graphics. Requests were sequential (one at a time), with 256-token answers and
  prompts of about 960 tokens. Long contexts, concurrent requests, and Arduino's
  QNN vision models (`ei-qnn-models-runner`) were not run.
- **The fan's headroom was never used.** The kernel kept the `emc2305` at state 5
  of 10 throughout. Its only active trip is 38 °C on `cpuss-0-0`, so the upper fan
  states were never seen, and neither was what controls them.
- **No reset point.** No thermal reset was observed, so the profile's "no thermal
  reset has been observed" note still holds. The trips themselves (115/126 °C
  passive, 125 °C PMIC critical) were not approached.

## Reproduce

On cultureos (needs `uv`, docker, and the `llamacpp-npu-runner:0.13.2` image):

```bash
mkdir -p ~/hg-measure/models ~/hg-measure/runs && cp scripts/measure/* ~/hg-measure/
curl -L -o ~/hg-measure/models/gemma-4-E2B_q4_0-it.gguf \
  https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf/resolve/347eef722ec7f151f37d1ef0b5c7c77d8de4efcb/gemma-4-E2B_q4_0-it.gguf
cd ~/hg-measure && uv venv venv && uv pip install --python venv/bin/python numpy wgpu
setsid nohup ./run.sh runs/limits-$(date +%Y%m%dT%H%M) 600 900 >/dev/null 2>&1 &
venv/bin/python analyze.py runs/limits-<stamp> ~/.local/state/heat-governor/records/<day>.jsonl

# run 2: Arduino's model layout (<org>/<repo>/<file>.gguf), then the serving soak
D=~/hg-measure/models-e4b/google/gemma-4-E4B-it-qat-q4_0-gguf && mkdir -p $D
curl -L -o $D/gemma-4-E4B_q4_0-it.gguf \
  https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf/resolve/e998672c8018dd704e347d3b6843c112547244d1/gemma-4-E4B_q4_0-it.gguf
setsid nohup ./run_serve.sh runs/serve-e4b-$(date +%Y%m%dT%H%M) ~/hg-measure/models-e4b 900 2700 \
  >/dev/null 2>&1 &
```

The raw runs are kept on cultureos: `~/hg-measure/runs/limits-20261110T1834/` (run 1),
`~/hg-measure/runs/serve-e4b-20261110T2143/` (run 2) and
`~/hg-measure/runs/serve-smoke-e2b/` (the E2B smoke test). Each holds `samples.jsonl`
and the perf, request, server and phase logs.

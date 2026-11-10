#!/bin/bash
# Serving soak: Arduino's llamacpp-npu-runner (its own router entrypoint and compose settings)
# serving the models in $MODELS, a steady request loop, then the same plus full CPU+GPU load.
# Usage: run_serve.sh <run-dir> <models-dir> [serve-seconds] [loaded-seconds]
set -u
H="$HOME/hg-measure"
R="$1"; MODELS="$2"; S1="${3:-900}"; S2="${4:-2700}"
PORT=9998
mkdir -p "$R"; rm -f "$H/ABORT"
PY="$H/venv/bin/python"
I=ghcr.io/arduino/app-bricks/llamacpp-npu-runner:0.13.2
log() { echo "$(date +%s) $*" | tee -a "$R/phases.log"; }
socmax() { tail -1 "$R/samples.jsonl" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["soc_max"])'; }
aborted() { [ -e "$H/ABORT" ]; }
cleanup() {
  pkill -f hgm-burn; docker kill hgm-llm >/dev/null 2>&1
  docker logs hgm-llm >"$R/server.log" 2>&1; docker rm hgm-llm >/dev/null 2>&1
  kill "$SAMP"; echo done >"$H/PHASE"
}

echo idle >"$H/PHASE"
"$PY" "$H/sampler.py" "$R/samples.jsonl" 2>>"$R/sampler.err" &
SAMP=$!
trap cleanup EXIT

# Same environment, limits, devices and mounts as service_compose.yaml (models dir swapped).
docker run -d --name hgm-llm \
  -e LLAMA_LOG_LEVEL=error -e LLAMA_ARG_HOST=0.0.0.0 -e LLAMA_ARG_PORT=9999 -e LLAMA_ARG_POLL=0 \
  -e LLAMA_ARG_BATCH=1024 -e LLAMA_ARG_UBATCH=512 -e LLAMA_ARG_MODELS_DIR=/models \
  -e LLAMA_ARG_FLASH_ATTN=on -e LLAMA_ARG_MODELS_MAX=2 -e LLAMA_ARG_CTX_SIZE=16384 \
  -e LLAMA_ARG_REASONING=off -e LLAMA_ARG_THINK_BUDGET=128 -e LLAMA_ARG_THREADS=4 \
  -e LLAMA_ARG_CPU_MASK=0x0f -e LLAMA_ARG_IMAGE_MAX_TOKENS=280 -e LLAMA_ARG_FIT=off \
  -e GGML_HEXAGON_OPBATCH=2048 -e GGML_HEXAGON_MBUF=256,256 -e XDG_CACHE_HOME=/models \
  -w /models -p 127.0.0.1:$PORT:9999 \
  --cap-add SYS_RESOURCE --ulimit memlock=-1:-1 --ulimit nofile=65536:65536 \
  --oom-score-adj -500 --memory 12g --memory-swap 12g --shm-size 2g \
  --device /dev/dma_heap/system --device /dev/fastrpc-cdsp \
  -v /sys/firmware/devicetree/base/model:/run/device-model \
  -v /usr/share/qcom:/run/host-qcom:ro -v "$MODELS:/models/" \
  --cpuset-cpus 0-3 "$I" >/dev/null || { log "server failed to start"; exit 2; }

for _ in $(seq 120); do
  curl -sf "http://127.0.0.1:$PORT/v1/models" -o "$R/models.json" && break; sleep 2
done
MODEL=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"][0]["id"])' "$R/models.json") \
  || { log "no model listed"; exit 2; }
log "server up, model $MODEL"
docker logs hgm-llm 2>&1 | grep -E "session|HEXAGON|ctx|Configuring|NPU|Big model" >"$R/server-start.log"

log "idle baseline 120s"; sleep 120
BASE=$(socmax); log "baseline soc_max $BASE"

URL="http://127.0.0.1:$PORT/v1/chat/completions"
echo serve >"$H/PHASE"; log "start serve for ${S1}s"
nice -n 0 "$PY" "$H/serve_client.py" "$URL" "$MODEL" "$R/requests.jsonl" hgm-burn &
end=$(( $(date +%s) + S1 )); while [ "$(date +%s)" -lt "$end" ]; do aborted && break; sleep 2; done
aborted && { log "ABORT: $(cat "$H/ABORT")"; exit 3; }
log "stop serve at soc_max $(socmax)"

echo serve+load >"$H/PHASE"; log "start serve+load (cpu gpu) for ${S2}s"
nice -n 10 "$PY" "$H/burn_cpu.py" "$R/perf.log" hgm-burn 2>>"$R/burn.err" &
nice -n 10 "$PY" "$H/burn_gpu.py" "$R/perf.log" hgm-burn 2>>"$R/burn.err" &
end=$(( $(date +%s) + S2 )); while [ "$(date +%s)" -lt "$end" ]; do aborted && break; sleep 2; done
aborted && { log "ABORT: $(cat "$H/ABORT")"; exit 3; }
log "stop serve+load at soc_max $(socmax)"

pkill -f hgm-burn; echo cool >"$H/PHASE"; log "cool"
end=$(( $(date +%s) + 600 ))
while [ "$(date +%s)" -lt "$end" ]; do
  "$PY" -c "import sys; sys.exit(0 if $(socmax) <= $BASE + 4 else 1)" && break; sleep 5
done
log "cooled to $(socmax)"; log "finished"

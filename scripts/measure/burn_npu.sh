#!/bin/bash
# NPU burn: llama-bench prefill (pp2048) on Hexagon HTP0 in Arduino's llamacpp-npu-runner,
# mirroring their service_compose.yaml (cpuset 0-3, 4 threads, fastrpc-cdsp). argv: log hgm-burn
LOG="$1"
NAME="hgm-npu-$$"
I=ghcr.io/arduino/app-bricks/llamacpp-npu-runner:0.13.2
M=gemma-4-E2B_q4_0-it.gguf
# docker run goes to the background and we `wait` on it: bash defers traps while a
# foreground child runs, so this is what lets TERM kill the container at once.
trap 'docker kill "$NAME" >/dev/null 2>&1' EXIT
trap 'exit 143' TERM INT
docker run --rm --name "$NAME" \
  --cap-add SYS_RESOURCE --ulimit memlock=-1:-1 --shm-size 2g --cpuset-cpus 0-3 \
  --device /dev/dma_heap/system --device /dev/fastrpc-cdsp \
  -v /sys/firmware/devicetree/base/model:/run/device-model:ro \
  -v /usr/share/qcom:/run/host-qcom:ro \
  -v "$HOME/hg-measure/models:/models:ro" \
  -e GGML_HEXAGON_OPBATCH=2048 -e GGML_HEXAGON_MBUF=256,256 \
  -e LD_LIBRARY_PATH=/opt/pkg-snapdragon/lib -e ADSP_LIBRARY_PATH=/opt/pkg-snapdragon/lib \
  --entrypoint /qairt-entrypoint.sh "$I" \
  bash -c "while true; do /opt/pkg-snapdragon/bin/llama-bench -m /models/$M --device HTP0 -ngl 100 \
    -t 4 -fa 1 -b 1024 -ub 512 -p 2048 -n 0 -r 20 -o csv 2>&1 | grep -v '^build' | \
    while read -r l; do echo \"\$(date +%s) \$l\"; done; done" >>"$LOG" 2>&1 &
wait $!

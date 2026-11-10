#!/bin/bash
# Orchestrate the VENTUNO Q limits run. Usage: run.sh <run-dir> [phase-seconds] [all-seconds]
set -u
H="$HOME/hg-measure"
R="$1"; P="${2:-600}"; A="${3:-900}"
mkdir -p "$R"; rm -f "$H/ABORT"
PY="$H/venv/bin/python"
echo idle >"$H/PHASE"
"$PY" "$H/sampler.py" "$R/samples.jsonl" 2>>"$R/sampler.err" &
SAMP=$!
trap 'pkill -f hgm-burn; docker ps -q --filter name=hgm- | xargs -r docker kill >/dev/null; kill $SAMP; echo done >"$H/PHASE"' EXIT

log() { echo "$(date +%s) $*" | tee -a "$R/phases.log"; }
socmax() { tail -1 "$R/samples.jsonl" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["soc_max"])'; }
aborted() { [ -e "$H/ABORT" ]; }

start() {  # start <kind>
  case $1 in
    cpu) nice -n 10 "$PY" "$H/burn_cpu.py" "$R/perf.log" hgm-burn 2>>"$R/burn.err" & ;;
    gpu) nice -n 10 "$PY" "$H/burn_gpu.py" "$R/perf.log" hgm-burn 2>>"$R/burn.err" & ;;
    npu) nice -n 10 "$H/burn_npu.sh" "$R/npu.log" hgm-burn 2>>"$R/burn.err" & ;;
  esac
}
stop_all() { pkill -f hgm-burn; docker ps -q --filter name=hgm- | xargs -r docker kill >/dev/null 2>&1; sleep 2; }

hold() {  # hold <seconds>: wait, but return early on abort
  local end=$(( $(date +%s) + $1 ))
  while [ "$(date +%s)" -lt "$end" ]; do aborted && return 1; sleep 2; done
}
cool() {
  echo "cool:$1" >"$H/PHASE"; log "cool after $1"
  local end=$(( $(date +%s) + 900 ))
  while [ "$(date +%s)" -lt "$end" ]; do
    "$PY" -c "import sys; sys.exit(0 if $(socmax) <= $BASE + 4 else 1)" && break
    sleep 5
  done
  log "cooled to $(socmax)"
}
phase() {  # phase <name> <seconds> <kinds...>
  local name=$1 secs=$2; shift 2
  echo "$name" >"$H/PHASE"; log "start $name ($*) for ${secs}s at soc_max $(socmax)"
  for k in "$@"; do start "$k"; done
  hold "$secs"; local rc=$?
  stop_all; log "stop $name at soc_max $(socmax) rc=$rc"
  aborted && { log "ABORT: $(cat "$H/ABORT")"; exit 3; }
  cool "$name"
}

log "idle baseline 180s"; sleep 180
BASE=$(socmax); log "baseline soc_max $BASE"
phase cpu "$P" cpu
phase gpu "$P" gpu
phase npu "$P" npu
phase all "$A" cpu gpu npu
log "finished"

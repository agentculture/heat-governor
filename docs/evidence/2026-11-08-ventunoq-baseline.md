# VENTUNO Q baseline before the fleet view (2026-11-08)

The state of cultureos (Arduino VENTUNO Q, Qualcomm QCS8300) and its view of the
mesh before heat-governor was installed on it. Plan task t13; spec claim c20 and
honesty condition h16 (`docs/specs/2026-11-08-fleet-view-ventuno-q-fit.md`).

Measured 2026-11-08T22:57Z on cultureos, read-only, using the `main` branch code
(heat-governor 0.15.0, before the `ventunoq` profile existed).

## Board identity

```text
/sys/firmware/devicetree/base/compatible: arduino,monza qcom,monaco-monza qcom,qcs8300
/proc/device-tree/model:                  Qualcomm Technologies, Inc. Monaco Monza addons
cores: 8    memory: 14 GiB    kernel: 6.8.0-1084-qcom
```

## What heat-governor 0.15.0 did with it

| Field | Value |
|---|---|
| Platform resolved | `generic` (detection read only `device-tree/model`) |
| Zones watched | all 48 |
| Hottest zone | `25:pmm8650au_1_sdram` at 46.9 °C, a memory PMIC, not the SoC |
| Limits (warm / hot / crit) | 102 / 106 / 110 °C, from the PMIC's 125 °C critical trip minus 15 °C |
| GPU | not read (`gpu: {}`) |
| Board power | not read |

The SoC zones carry only passive trips (CPU 126/130/135 °C, GPU and NPU
115/130/135 °C), so the generic limits sat far above anything the board would
reach under normal load. The governor would essentially never have acted.

## Installation and reach

| Check | Result |
|---|---|
| `which heat-governor` | not found |
| `systemctl --user status heat-governor` | unit not found |
| `curl http://<box>:9476/state` from cultureos, for spark, spark2, thor, orin | `000` (no connection) on all four |

Every remote daemon binds `127.0.0.1:9476` (the Sparks also bind their private
`10.77.77.x` pair link), so checking the fleet from cultureos meant SSH-ing into
each box. A read-only survey earlier the same day confirmed this from each box's
`ss -ltnp` and config.

## Reproduce

```bash
git worktree add /tmp/hg-main main && cd /tmp/hg-main
uv run heat-governor sense --json        # platform generic, 48 zones
uv run heat-governor capabilities --json # limits 102/106/110
for h in spark spark2 thor orin; do curl -s -m3 -o /dev/null -w "%{http_code}\n" "http://$h:9476/state"; done
```

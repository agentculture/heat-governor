#!/usr/bin/env python3
"""1 Hz thermal/power sampler with an abort watchdog (heat-governor VENTUNO Q limits run).

Writes one JSON line per second to argv[1]. If any SoC zone >= ABORT_SOC_C or any PMIC
zone >= ABORT_PMIC_C, it kills every load (processes with 'hgm-burn' in argv, containers
named hgm-*) and writes ~/hg-measure/ABORT.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ABORT_SOC_C = float(os.environ.get("ABORT_SOC_C", "110"))
ABORT_PMIC_C = float(os.environ.get("ABORT_PMIC_C", "105"))
SOC = ("cpu-", "cpuss-", "gpuss-", "nsp-", "ddrss-")
TH = Path("/sys/class/thermal")
HOME = Path.home() / "hg-measure"


def rd(p):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return ""


def num(p, div=1.0):
    try:
        return int(rd(p)) / div
    except ValueError:
        return None


zones = []
for z in sorted(TH.glob("thermal_zone*"), key=lambda p: int(p.name[12:])):
    zones.append((rd(z / "type"), z / "temp"))
cdevs = [(rd(c / "type"), c / "cur_state") for c in sorted(TH.glob("cooling_device*"))]
policies = sorted(Path("/sys/devices/system/cpu/cpufreq").glob("policy*"))
hw = {rd(h / "name"): h for h in Path("/sys/class/hwmon").glob("hwmon*")}
fan, ina = hw.get("emc2305"), hw.get("ina232")
gpu = Path("/sys/class/devfreq/3d00000.gpu")


def cpu_times():
    f = rd("/proc/stat").splitlines()[0].split()[1:]
    v = list(map(int, f))
    return sum(v), v[3] + v[4]


def abort(reason):
    (HOME / "ABORT").write_text(f"{time.time()} {reason}\n")
    subprocess.run(["pkill", "-f", "hgm-burn"], check=False)
    names = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=hgm-"], capture_output=True, text=True
    ).stdout.split()
    if names:
        subprocess.run(["docker", "kill", *names], check=False, capture_output=True)


out = open(sys.argv[1], "a", buffering=1)
prev = cpu_times()
while True:
    t0 = time.time()
    temps = {}
    for ty, p in zones:
        v = num(p, 1000.0)
        if v is not None:
            temps[ty] = v
    tot, idle = cpu_times()
    util = 1 - (idle - prev[1]) / max(1, tot - prev[0])
    prev = (tot, idle)
    rec = {
        "t": round(t0, 2),
        "phase": rd(HOME / "PHASE"),
        "temps": temps,
        "soc_max": max((v for k, v in temps.items() if k.startswith(SOC)), default=None),
        "cdev": {f"{i}:{ty}": int(rd(p) or 0) for i, (ty, p) in enumerate(cdevs)},
        "cpufreq": {
            p.name: [num(p / "scaling_cur_freq", 1000), num(p / "scaling_max_freq", 1000)]
            for p in policies
        },
        "gpu_mhz": [num(gpu / "cur_freq", 1e6), num(gpu / "max_freq", 1e6)],
        "fan_rpm": [num(fan / f"fan{i}_input") for i in range(1, 6)] if fan else None,
        "power_w": num(ina / "power1_input", 1e6) if ina else None,
        "cpu_util": round(util, 3),
        "load1": float(rd("/proc/loadavg").split()[0]),
    }
    out.write(json.dumps(rec) + "\n")
    pm = max((v for k, v in temps.items() if k.startswith("pmm")), default=0)
    if (rec["soc_max"] or 0) >= ABORT_SOC_C:
        abort(f"soc_max {rec['soc_max']}")
    elif pm >= ABORT_PMIC_C:
        abort(f"pmic {pm}")
    time.sleep(max(0.0, 1.0 - (time.time() - t0)))

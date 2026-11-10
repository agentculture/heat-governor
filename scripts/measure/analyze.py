#!/usr/bin/env python3
"""Summarise a VENTUNO Q limits run. argv: run-dir [records.jsonl]  (run on cultureos)."""

import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

R = Path(sys.argv[1])
S = [json.loads(line) for line in open(R / "samples.jsonl")]
GROUPS = {
    "cpu": "cpu-",
    "cpuss": "cpuss-",
    "gpu": "gpuss-",
    "nsp": "nsp-",
    "ddr": "ddrss-",
    "pmic": "pmm",
}


def gmax(r, p):
    return max(v for k, v in r["temps"].items() if k.startswith(p))


phases = defaultdict(list)
for r in S:
    phases[r["phase"]].append(r)

perf = defaultdict(list)  # kind -> [(t, value)]
for line in open(R / "perf.log") if (R / "perf.log").exists() else []:
    t, kind, v = line.split()
    perf[kind].append((float(t), float(v)))
npu = []
for line in open(R / "npu.log") if (R / "npu.log").exists() else []:
    parts = line.strip().split(" ", 1)
    if len(parts) == 2 and parts[1].startswith('"') and "HTP0" in parts[1]:
        cols = [c.strip('"') for c in parts[1].split(",")]
        try:
            npu.append((float(parts[0]), float(cols[-2])))  # avg_ts
        except ValueError:
            pass

rec = []
if len(sys.argv) > 2:
    t0, t1 = S[0]["t"], S[-1]["t"]
    for line in open(sys.argv[2]):
        if not line.startswith('{"t":179'):
            continue
        t = float(line[5 : line.index(",")])
        if t0 <= t <= t1:
            r = json.loads(line)
            rec.append((t, r["level"], r["duty"], r["cpus"], r["effective_c"]))


def window(series, a, b):
    return [v for t, v in series if a <= t <= b]


print(f"run {R.name}: {len(S)} samples, {(S[-1]['t'] - S[0]['t']) / 60:.1f} min\n")
hdr = (
    "phase",
    "min",
    "soc pk",
    "soc ss",
    "cpu",
    "cpuss",
    "gpu",
    "nsp",
    "ddr",
    "pmic",
    "W avg",
    "W pk",
    "fan",
    "t→ss",
)
print("{:<11}{:>5}{:>8}{:>8}{:>7}{:>7}{:>7}{:>7}{:>7}{:>7}{:>7}{:>7}{:>6}{:>6}".format(*hdr))
for name, rs in phases.items():
    n = len(rs)
    tail = rs[-180:] if n > 300 else rs  # steady state = last 3 min of a load phase
    soc = [r["soc_max"] for r in rs]
    ss = st.mean(r["soc_max"] for r in tail)
    # time to reach within 1 C of steady state
    tss = next((i for i, v in enumerate(soc) if v >= ss - 1.0), None)
    row = [name, f"{n / 60:.1f}", f"{max(soc):.1f}", f"{ss:.1f}"]
    row += [f"{max(gmax(r, p) for r in rs):.1f}" for p in GROUPS.values()]
    pw = [r["power_w"] for r in rs if r["power_w"]]
    row += [
        f"{st.mean(pw):.1f}",
        f"{max(pw):.1f}",
        f"{max(r['fan_rpm'][0] for r in rs):.0f}",
        f"{tss / 60:.1f}" if tss is not None and not name.startswith(("idle", "cool")) else "-",
    ]
    print("{:<11}{:>5}{:>8}{:>8}{:>7}{:>7}{:>7}{:>7}{:>7}{:>7}{:>7}{:>7}{:>6}{:>6}".format(*row))

print(
    "\nthrottling evidence per phase (non-fan cooling devices > 0, cpufreq max below hw max,"
    " GPU devfreq max below 877):"
)
hwmax = {k: v[1] for k, v in S[0]["cpufreq"].items()}
for name, rs in phases.items():
    cd = Counter()
    for r in rs:
        for k, v in r["cdev"].items():
            if v and "fan" not in k:
                cd[k] += 1
    fcap = Counter(k for r in rs for k, v in r["cpufreq"].items() if v[1] < hwmax[k])
    gcap = sum(1 for r in rs if r["gpu_mhz"][1] and r["gpu_mhz"][1] < 877)
    fan = Counter(v for r in rs for k, v in r["cdev"].items() if "fan" in k)
    print(
        f"  {name:<10} cdev {dict(cd) or '-'}  cpufreq-capped {dict(fcap) or '-'}  "
        f"gpu-capped {gcap or '-'}  fan states {dict(sorted(fan.items()))}"
    )

print("\nthroughput (first minute → last minute of each load phase):")
for name, rs in phases.items():
    if name.startswith(("idle", "cool", "smoke", "done")) or not name:
        continue
    a, b = rs[0]["t"], rs[-1]["t"]
    out = []
    for kind, series in list(perf.items()) + [("npu_pp2048_tps", npu)]:
        w = [(t, v) for t, v in series if a <= t <= b]
        if len(w) >= 2:
            first = st.mean(window(w, a, a + 90) or [w[0][1]])
            last = st.mean(window(w, b - 90, b) or [w[-1][1]])
            out.append(f"{kind} {first:.1f}→{last:.1f} ({(last / first - 1) * 100:+.1f}%)")
    avg_mhz = {k: st.mean(r["cpufreq"][k][0] for r in rs[-180:]) for k in hwmax}
    util = st.mean(r["cpu_util"] for r in rs[-180:])
    gmhz = st.mean(r["gpu_mhz"][0] for r in rs[-180:])
    print(f"  {name:<6} {'; '.join(out)}")
    print(
        f"         ss cpu MHz {[round(v) for v in avg_mhz.values()]} util {util:.2f}"
        f" gpu MHz {gmhz:.0f}"
    )

reqs = []
if (R / "requests.jsonl").exists():
    reqs = [json.loads(line) for line in open(R / "requests.jsonl")]
if reqs:
    print("\nserving (llama-server requests; first 3 min → last 3 min of each phase):")
    for name, rs in phases.items():
        a, b = rs[0]["t"], rs[-1]["t"]
        w = [q for q in reqs if a <= q["t"] <= b]
        ok = [q for q in w if "error" not in q and q.get("gen_tps")]
        if not w:
            continue
        err = len(w) - len(ok)

        def m(key, lo, hi, ok=ok):
            v = [q[key] for q in ok if lo <= q["t"] <= hi]
            return st.mean(v) if v else float("nan")

        print(
            f"  {name:<11} {len(w)} req ({err} errors)  "
            f"prompt {m('prompt_tps', a, a + 180):.0f}→{m('prompt_tps', b - 180, b):.0f} tok/s  "
            f"decode {m('gen_tps', a, a + 180):.1f}→{m('gen_tps', b - 180, b):.1f} tok/s  "
            f"mean decode {m('gen_tps', a, b):.1f}  mean wall "
            f"{st.mean(q['wall_s'] for q in ok) if ok else float('nan'):.1f} s"
        )

if rec:
    print("\nheat-governor dry-run decisions during the run (records):")
    for name, rs in phases.items():
        a, b = rs[0]["t"], rs[-1]["t"]
        w = [x for x in rec if a <= x[0] <= b]
        if not w:
            continue
        lv = Counter(x[1] for x in w)
        print(
            f"  {name:<10} levels {dict(lv)}  max effective {max(x[4] for x in w):.1f}  "
            f"max duty {max(x[2] for x in w):.2f}  min cpus "
            f"{min((x[3] for x in w if x[3]), default='-')}"
        )

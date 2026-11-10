#!/usr/bin/env python3
"""Steady request loop against llama-server; one JSON line per request with llama.cpp timings.

argv: url model log hgm-burn [max_tokens] [prompt_words]
Each prompt is unique (request number + shuffled filler) so the prompt cache can't help.
"""

import json
import random
import sys
import time
import urllib.request

url, model, logpath = sys.argv[1], sys.argv[2], sys.argv[3]
max_tokens = int(sys.argv[5]) if len(sys.argv) > 5 else 256
words = int(sys.argv[6]) if len(sys.argv) > 6 else 900
VOCAB = (
    "thermal board sensor fan clock power governor container queue latency batch prefill decode "
    "token model memory bandwidth cache heat sink zone trip passive critical throttle duty cycle "
    "edge device inference request stream window record replay limit margin ambient soak"
).split()
log = open(logpath, "a", buffering=1)
n = 0
while True:
    n += 1
    rnd = random.Random(n)
    filler = " ".join(rnd.choice(VOCAB) for _ in range(words))
    prompt = (
        f"Request {n}. Below are field notes from a hardware lab. Summarise them as a numbered "
        f"list of concrete observations, then explain what to measure next.\n\n{filler}"
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "cache_prompt": False,
        }
    ).encode()
    t0 = time.time()
    try:
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:  # nosec B310 - localhost only
            d = json.load(r)
        tm = d.get("timings", {})
        rec = {
            "t": round(t0, 2),
            "wall_s": round(time.time() - t0, 2),
            "prompt_n": tm.get("prompt_n"),
            "prompt_tps": tm.get("prompt_per_second"),
            "gen_n": tm.get("predicted_n"),
            "gen_tps": tm.get("predicted_per_second"),
        }
    except Exception as e:  # keep looping; record the failure
        rec = {"t": round(t0, 2), "wall_s": round(time.time() - t0, 2), "error": repr(e)[:200]}
        time.sleep(2)
    log.write(json.dumps(rec) + "\n")

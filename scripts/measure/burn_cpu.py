#!/usr/bin/env python3
"""CPU burn: float32 matmul on all threads; logs GFLOPS every ~5 s. argv: log hgm-burn"""

import sys
import time

import numpy as np

n = 2048
a = np.random.rand(n, n).astype(np.float32)
b = np.random.rand(n, n).astype(np.float32)
log = open(sys.argv[1], "a", buffering=1)
while True:
    t0, k = time.time(), 0
    while time.time() - t0 < 5:
        a @ b
        k += 1
    dt = time.time() - t0
    log.write(f"{time.time():.1f} cpu_gflops {2 * n**3 * k / dt / 1e9:.1f}\n")

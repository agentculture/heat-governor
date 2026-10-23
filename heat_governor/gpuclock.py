"""GPU clock ceiling: the one control that needs root.

The daemon runs as an ordinary user. The privileged step is a small helper,
installed once per host by the operator (``heat-governor gpu-clock helper``
prints it and the sudoers line), that accepts exactly three commands:

* ``lock <MHz>`` -- cap the GPU's maximum clock (DGX Spark: ``nvidia-smi
  -lgc 0,<MHz>``; Jetson: the highest step of each GPU devfreq node's
  ``available_frequencies`` at or below <MHz> written to ``max_freq``);
* ``reset`` -- remove the cap (``nvidia-smi -rgc``; Jetson: the top step);
* ``status`` -- print the current ceiling in MHz.

The daemon calls it as ``sudo -n <helper> ...``. Without the helper, or
without the sudoers rule, the control reports itself unavailable and nothing
else changes. Every lock is written to the ledger first and undone on exit
and on the next start.

Status (2026-10-23): the mechanism is implemented and unit-tested; whether
``nvidia-smi -lgc`` takes effect on GB10 and what a lower ceiling does to the
board temperature are NOT yet measured.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - fixed argv only
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

HELPER_PATH = "/usr/local/sbin/heat-governor-gpuclock"
MIN_MHZ, MAX_MHZ = 100, 5000

Run = Callable[[list[str]], "tuple[int, str]"]


def _run(argv: list[str]) -> tuple[int, str]:  # pragma: no cover - thin shell
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout or p.stderr).strip()


@dataclass
class GpuClock:
    helper: str = HELPER_PATH
    run: Run = _run
    available: bool = False
    reason: str = "not probed"
    ceiling_mhz: int | None = None  # what we last set; None = no cap from us

    def probe(self) -> bool:
        if not Path(self.helper).exists():
            self.available, self.reason = False, f"helper not installed at {self.helper}"
            return False
        if not shutil.which("sudo"):
            self.available, self.reason = False, "sudo not found"
            return False
        rc, out = self.run(["sudo", "-n", self.helper, "status"])
        if rc != 0:
            self.available = False
            self.reason = (
                f"sudo -n {self.helper} status failed ({out or rc}); is the sudoers rule installed?"
            )
            return False
        self.available, self.reason = True, f"ok (helper status: {out})"
        return True

    def lock(self, mhz: int) -> tuple[bool, str]:
        mhz = int(mhz)
        if not MIN_MHZ <= mhz <= MAX_MHZ:
            return False, f"refusing {mhz} MHz (allowed {MIN_MHZ}-{MAX_MHZ})"
        rc, out = self.run(["sudo", "-n", self.helper, "lock", str(mhz)])
        if rc == 0:
            self.ceiling_mhz = mhz
        return rc == 0, out

    def reset(self) -> tuple[bool, str]:
        rc, out = self.run(["sudo", "-n", self.helper, "reset"])
        if rc == 0:
            self.ceiling_mhz = None
        return rc == 0, out


HELPER_SCRIPT = r"""#!/bin/bash
# heat-governor-gpuclock -- the only privileged step of heat-governor.
# Install root-owned, mode 0755, at /usr/local/sbin/heat-governor-gpuclock.
# Usage: lock <MHz> | reset | status
set -euo pipefail
cmd="${1:-status}"
gpu_nodes() { ls -d /sys/class/devfreq/*gpu* 2>/dev/null || true; }
is_jetson() { [ -e /etc/nv_tegra_release ] && [ -n "$(gpu_nodes)" ]; }
case "$cmd" in
  lock)
    mhz="${2:-}"
    [[ "$mhz" =~ ^[0-9]+$ ]] && [ "$mhz" -ge 100 ] && [ "$mhz" -le 5000 ] || { echo "bad MHz: $mhz" >&2; exit 2; }
    if is_jetson; then
      for d in $(gpu_nodes); do
        want=$((mhz * 1000000)); best=""
        for f in $(cat "$d/available_frequencies"); do [ "$f" -le "$want" ] && best="$f"; done
        [ -n "$best" ] || best=$(tr ' ' '\n' < "$d/available_frequencies" | sort -n | head -1)
        echo "$best" > "$d/max_freq"
      done
    else
      nvidia-smi -lgc "0,$mhz" >/dev/null
    fi
    echo "$mhz" ;;
  reset)
    if is_jetson; then
      for d in $(gpu_nodes); do
        tr ' ' '\n' < "$d/available_frequencies" | sort -n | tail -1 > "$d/max_freq"
      done
    else
      nvidia-smi -rgc >/dev/null
    fi
    echo reset ;;
  status)
    if is_jetson; then
      d=$(gpu_nodes | head -1); echo $(( $(cat "$d/max_freq") / 1000000 ))
    else
      nvidia-smi --query-gpu=clocks.sm,clocks.max.sm --format=csv,noheader,nounits | head -1
    fi ;;
  *) echo "usage: $0 lock <MHz> | reset | status" >&2; exit 2 ;;
esac
"""


def sudoers_line(user: str) -> str:
    return f"{user} ALL=(root) NOPASSWD: {HELPER_PATH}"


def install_instructions(user: str) -> str:
    return (
        "# 1. save the helper (printed above) and install it root-owned:\n"
        f"sudo install -o root -g root -m 0755 heat-governor-gpuclock {HELPER_PATH}\n"
        "# 2. allow only that helper without a password:\n"
        f"echo '{sudoers_line(user)}' | sudo tee /etc/sudoers.d/heat-governor-gpuclock\n"
        "sudo chmod 0440 /etc/sudoers.d/heat-governor-gpuclock && sudo visudo -cf /etc/sudoers.d/heat-governor-gpuclock\n"
        "# 3. check: heat-governor gpu-clock status\n"
    )

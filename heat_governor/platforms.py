"""Platform detection and the built-in thermal profiles.

A :class:`Profile` is pure data: which thermal zones to watch, the temperature
bands the controller acts on, the duty curve for batch containers and the CPU
bands. The controller (:mod:`heat_governor.policy`) never knows which machine it
runs on; only this module does.

Provenance of the numbers (keep this honest when you change them):

* **DGX Spark (GB10)** -- MEASURED on 2026-10-23. Two silent hard resets with
  the hottest ACPI zone at ~94.5-95 C while the GPU sat at 77-82 C; every trip
  point reads 104.8 C, so the firmware's own trips are useless here. What held
  for 3.5 h: pause at 92 C / resume at 86 C, a 1 s poll, CPU bands
  85:12,88:8,90:4. Zone 0/4 move 2-5 C per second, hence the lookahead.
* **Jetson AGX Thor / Orin** -- DECLARED, not measured. The bands are derived
  from each board's own passive trip point (Thor 109 C, Orin 99 C) minus a
  margin. No reset has been observed on either board, so treat them as a
  conservative starting point to tune from recorded runs.
* **generic** -- derived at runtime from the lowest critical trip point.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence

# Breakpoints of a piecewise-linear curve: (temperature C, value).
Curve = tuple[tuple[float, float], ...]
# CPU bands: (from_c, cpus) ascending -- at or above from_c the cap is cpus.
CpuBands = tuple[tuple[float, int], ...]


@dataclass(frozen=True)
class Limits:
    """warm: start throttling side work; hot: stop admitting new work;
    critical: batch containers fully paused. ``hysteresis_c`` is how far below a
    threshold the temperature must fall (for ``dwell_s``) before a level drops."""

    warm_c: float
    hot_c: float
    crit_c: float
    hysteresis_c: float = 2.0
    dwell_s: float = 5.0

    def __post_init__(self) -> None:
        if not self.warm_c < self.hot_c < self.crit_c:
            raise ValueError(
                f"limits must ascend: warm {self.warm_c} < hot {self.hot_c} < crit {self.crit_c}"
            )


@dataclass(frozen=True)
class Profile:
    name: str
    zones: tuple[str, ...]  # thermal-zone types to watch; empty = every zone
    limits: Limits
    duty_curve: Curve  # effective temp -> fraction of each period a batch container is paused
    cpu_bands: CpuBands
    # GPU clock ceiling at or above each temperature: (from_c, max MHz). Used only
    # when the daemon runs with gpu_clock enabled and the root helper is installed.
    gpu_clock_bands: CpuBands = ()
    # The GPU's own temperature as a second input, with its own warm/hot/critical
    # bands (None = the GPU is not a separate input, e.g. a Jetson whose
    # gpu-thermal zone is already watched). Whichever input is closer to its own
    # limit governs.
    gpu_temp_limits: "Limits | None" = None
    # CPU cap for configured systemd user units ([[units]]) per level: (level, cpus)
    # for any of warm / hot / critical. Empty = a unit is capped only by its own bands.
    unit_bands: tuple[tuple[str, float], ...] = ()
    lookahead_s: float = 1.5  # effective temp = temp + lookahead * rising rate
    rate_window_s: float = 3.0
    ramp_s: float = 60.0  # a CPU cap rises one band per ramp_s; drops are immediate
    duty_period_s: float = 2.0
    duty_decay_per_s: float = 0.1  # how fast the duty may fall (rises are immediate)
    # client concurrency factor per level: cool, warm, hot, critical (0 = hold new work)
    concurrency: tuple[float, float, float, float] = (1.0, 0.5, 0.0, 0.0)
    gpu: str = "none"  # "nvidia-smi" | "jetson" | "none"
    provenance: str = "declared"
    notes: str = ""
    extra: dict = field(default_factory=dict, compare=False)


SPARK = Profile(
    name="spark",
    zones=("acpitz",),
    limits=Limits(warm_c=88.0, hot_c=91.5, crit_c=93.0, hysteresis_c=1.0, dwell_s=2.0),
    duty_curve=((88.0, 0.0), (89.5, 0.15), (91.0, 0.35), (92.5, 0.7), (93.0, 1.0)),
    cpu_bands=((85.0, 12), (88.0, 8), (90.0, 4)),
    gpu_clock_bands=((90.0, 2200), (91.5, 1800), (93.0, 1400)),  # declared, unmeasured
    # GB10 reports ~21 C of headroom (T.Limit) at 73 C: its own limit is near 94 C.
    gpu_temp_limits=Limits(warm_c=82.0, hot_c=87.0, crit_c=91.0),  # declared, unmeasured
    # of 20 CPUs, for the operator's own sessions (app.slice): declared, unmeasured
    unit_bands=(("warm", 8.0), ("hot", 4.0), ("critical", 2.0)),
    lookahead_s=0.5,
    rate_window_s=2.0,
    gpu="nvidia-smi",
    provenance=(
        "measured 2026-10-23 (resets at ~94.5-95 C on acpitz zones 0/4); bands tuned by "
        "open-loop replay of that day's 1 s trace (new work held ~17%, batch fully paused ~4%)"
    ),
    notes="GB10: no OS clock/power-limit control; fans firmware-controlled.",
)

THOR = Profile(
    name="thor",
    zones=("cpu-thermal", "gpu-thermal", "soc012-thermal", "soc345-thermal", "tj-thermal"),
    limits=Limits(warm_c=95.0, hot_c=100.0, crit_c=104.0),
    duty_curve=((95.0, 0.0), (98.0, 0.2), (101.0, 0.5), (104.0, 1.0)),
    cpu_bands=((95.0, 10), (100.0, 6)),
    # measured 2026-10-24 (docs/evidence/2026-10-24-gpu-clock-ceiling-jetsons.md): worker
    # decode loses 3-15% at 1300-900 MHz for only 1-5 W, so no default ceiling; these
    # heat steps stay declared
    gpu_clock_bands=((98.0, 1300), (101.0, 1000)),
    gpu="jetson",
    provenance="declared: passive trip 109 C minus margin; unmeasured",
    notes="gpu-thermal returns EAGAIN while the GPU is idle; that zone is skipped, not hot.",
)

ORIN = Profile(
    name="orin",
    zones=(
        "cpu-thermal",
        "gpu-thermal",
        "soc0-thermal",
        "soc1-thermal",
        "soc2-thermal",
        "tj-thermal",
    ),
    limits=Limits(warm_c=85.0, hot_c=90.0, crit_c=94.0),
    duty_curve=((85.0, 0.0), (88.0, 0.2), (91.0, 0.5), (94.0, 1.0)),
    cpu_bands=((85.0, 8), (90.0, 4)),
    # measured 2026-10-24 (docs/evidence/2026-10-24-gpu-clock-ceiling-jetsons.md): the
    # decider at 2 concurrent, full 1300 MHz -> board 94.1 C max; 1122 MHz -> 83.6 C max for
    # -12% req/s and -25% GPU rail; 918 MHz -> 80.2 C for -27%. Steps are devfreq steps.
    gpu_clock_bands=((85.0, 1122), (88.0, 918)),
    gpu="jetson",
    provenance=(
        "declared limits: passive trip 99 C minus margin; GPU clock steps measured "
        "2026-10-24 (decider sweep)"
    ),
    notes="cv0-2 zones return ENODATA (no DLA load); skipped.",
)

BUILTIN = {p.name: p for p in (SPARK, THOR, ORIN)}


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip("\x00\n ")
    except OSError:
        return ""


def detect(root: Path = Path("/"), *, nvidia_smi: bool = True) -> str:
    """Return ``spark`` / ``thor`` / ``orin`` / ``generic``.

    Jetsons name themselves in the device tree. The Spark has no device-tree
    model, so it is recognised by its GPU name (``NVIDIA GB10``)."""
    model = _read(root / "proc/device-tree/model").lower()
    if "jetson agx thor" in model:
        return "thor"
    if "jetson agx orin" in model:
        return "orin"
    if nvidia_smi and shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        if "GB10" in out:
            return "spark"
    return "generic"


def generic_profile(sys_root: Path = Path("/sys")) -> Profile:
    """Bands from the lowest critical trip point: crit 15 C under it."""
    crits: list[float] = []
    for zone in sorted((sys_root / "class/thermal").glob("thermal_zone*")):
        for t in zone.glob("trip_point_*_type"):
            if _read(t) == "critical":
                try:
                    crits.append(int(_read(t.with_name(t.name[:-4] + "temp"))) / 1000.0)
                except ValueError:
                    continue
    crit = (min(crits) - 15.0) if crits else 95.0
    return Profile(
        name="generic",
        zones=(),
        limits=Limits(warm_c=crit - 8.0, hot_c=crit - 4.0, crit_c=crit),
        duty_curve=((crit - 8.0, 0.0), (crit - 4.0, 0.3), (crit, 1.0)),
        cpu_bands=((crit - 8.0, max(2, (os.cpu_count() or 8) // 2)), (crit - 4.0, 2)),
        provenance="derived from the lowest critical trip point minus 15 C",
    )


def _curve(value: Iterable[Sequence[float]]) -> Curve:
    return tuple((float(t), float(v)) for t, v in value)


def _int_bands(value: Iterable[Sequence[float]]) -> tuple[tuple[float, int], ...]:
    return tuple((float(t), int(c)) for t, c in value)


def _gpu_temp_override(p: Profile, g: dict | bool) -> Profile:
    if g is False:
        return replace(p, gpu_temp_limits=None)
    base = p.gpu_temp_limits or Limits(warm_c=82.0, hot_c=87.0, crit_c=91.0)
    return replace(p, gpu_temp_limits=replace(base, **{k: float(v) for k, v in g.items()}))


def _concurrency_override(p: Profile, conc: dict) -> Profile:
    levels = ("cool", "warm", "hot", "critical")
    cur = dict(zip(levels, p.concurrency))
    cur.update({k: float(v) for k, v in conc.items()})
    return replace(p, concurrency=tuple(cur[k] for k in levels))  # type: ignore[arg-type]


def apply_overrides(profile: Profile, cfg: dict) -> Profile:
    """Overlay a ``[profile]`` table from a TOML config onto a profile."""
    p = profile
    if "zones" in cfg:
        p = replace(p, zones=tuple(str(z) for z in cfg["zones"]))
    lim = {
        k: float(cfg[k])
        for k in ("warm_c", "hot_c", "crit_c", "hysteresis_c", "dwell_s")
        if k in cfg
    }
    if lim:
        p = replace(p, limits=replace(p.limits, **lim))
    converters = {"duty_curve": _curve, "cpu_bands": _int_bands, "gpu_clock_bands": _int_bands}
    converters |= dict.fromkeys(
        ("lookahead_s", "rate_window_s", "ramp_s", "duty_period_s", "duty_decay_per_s"), float
    )
    converters["gpu"] = str
    for key, convert in converters.items():
        if key in cfg:
            p = replace(p, **{key: convert(cfg[key])})
    if "gpu_temp" in cfg:
        p = _gpu_temp_override(p, cfg["gpu_temp"])
    if "concurrency" in cfg:
        p = _concurrency_override(p, cfg["concurrency"])
    if p is not profile:
        p = replace(p, provenance=f"{profile.provenance}; overridden by config")
    return p


def load_config(path: Path | None) -> dict:
    """Read and validate a config file (see :mod:`heat_governor.config`)."""
    from heat_governor.config import load

    return load(path, required=path is not None).data


def resolve(
    name: str | None = None,
    *,
    config: dict | None = None,
    root: Path = Path("/"),
) -> Profile:
    """Pick the profile: explicit name, else ``config['platform']``, else detect."""
    cfg = config or {}
    name = name or cfg.get("platform") or detect(root)
    if name in BUILTIN:
        base = BUILTIN[name]
    elif name == "generic":
        base = generic_profile(root / "sys")
    else:
        raise ValueError(f"unknown platform {name!r}; known: {', '.join([*BUILTIN, 'generic'])}")
    return apply_overrides(base, cfg.get("profile", {}))

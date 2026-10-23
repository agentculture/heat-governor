"""Read-only sensing: thermal zones, fans, GPU.

Everything takes a ``sys_root`` so tests run against a fake tree. A zone whose
``temp`` cannot be read (EAGAIN on an idle Thor GPU, ENODATA on the Orin's
idle DLA zones) or reads outside -40..150 C is reported as ``None`` and does
not count toward the governing temperature. Only when *every* watched zone is
unreadable is the reading lost (the controller then treats the host as hot).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

MIN_C, MAX_C = -40.0, 150.0
GPU_FIELDS = (
    "temperature.gpu",
    "power.draw",
    "clocks.sm",
    "utilization.gpu",
    "clocks_event_reasons.active",
    "clocks.gr",
    "clocks.mem",
    "utilization.memory",
)


@dataclass(frozen=True)
class Zone:
    index: int
    type: str
    temp_c: float | None
    trips: tuple[tuple[str, float], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.index}:{self.type}"


@dataclass
class Reading:
    t: float
    zones: list[Zone]
    watched: tuple[str, ...]
    fans: dict[str, float] = field(default_factory=dict)
    gpu: dict[str, object] = field(default_factory=dict)
    system: dict[str, float] = field(default_factory=dict)

    def watched_zones(self) -> list[Zone]:
        return [z for z in self.zones if not self.watched or z.type in self.watched]

    @property
    def temp_c(self) -> float | None:
        """Hottest readable watched zone; None when none can be read."""
        temps = [z.temp_c for z in self.watched_zones() if z.temp_c is not None]
        return max(temps) if temps else None

    @property
    def gpu_temp_c(self) -> float | None:
        t = self.gpu.get("temp_c")
        return float(t) if isinstance(t, (int, float)) and MIN_C <= t <= MAX_C else None

    @property
    def hottest(self) -> Zone | None:
        live = [z for z in self.watched_zones() if z.temp_c is not None]
        return max(live, key=lambda z: z.temp_c or 0.0) if live else None

    def sensors(self) -> dict[str, float | str | None]:
        """Every sensor under its own flat name -- ``zone:<index>:<type>``,
        ``gpu:<field>``, ``fan:<name>`` -- so results can be told apart per sensor."""
        return flat_sensors(
            {z.key: z.temp_c for z in self.zones},
            self.gpu,
            self.fans,  # type: ignore[arg-type]
            self.system,
        )

    def as_dict(self) -> dict[str, object]:
        hot = self.hottest
        return {
            "t": round(self.t, 3),
            "temp_c": self.temp_c,
            "hottest_zone": hot.key if hot else None,
            "zones": {z.key: z.temp_c for z in self.zones},
            "watched": list(self.watched),
            "fans": self.fans,
            "gpu": self.gpu,
        }


def flat_sensors(
    zones: dict, gpu: dict, fans: dict, system: dict | None = None
) -> dict[str, float | str | None]:
    out: dict[str, float | str | None] = {f"zone:{k}": v for k, v in (zones or {}).items()}
    out |= {f"gpu:{k}": v for k, v in (gpu or {}).items()}
    out |= {f"fan:{k}": v for k, v in (fans or {}).items()}
    out |= dict(system or {})  # already named cpu:* / mem:* / psi:* / disk:<dev>:*
    return out


_DISK_RE = ("nvme", "sd", "mmcblk", "vd")


class SystemProbe:
    """CPU, memory, pressure and per-disk I/O from /proc and /sys, once a tick.

    Rates (CPU busy %, disk MB/s and % busy) are deltas against the previous
    read, so the first read reports none of them. Every field is optional: a
    kernel without /proc/pressure (the Orin) simply has no ``psi:*`` keys."""

    def __init__(
        self, proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys"), clock=time.monotonic
    ) -> None:
        self.proc = proc_root
        self.sys = sys_root
        self._clock = clock
        self._cpu_prev: tuple[float, float] | None = None
        self._disk_prev: dict[str, tuple[float, float, float]] = {}
        self._at: float | None = None

    def _cpu(self, out: dict) -> None:
        raw = _read(self.proc / "stat") or ""
        line = raw.splitlines()[0].split() if raw else []
        if len(line) > 5 and line[0] == "cpu":
            vals = [float(x) for x in line[1:9]]
            idle = vals[3] + vals[4]  # idle + iowait
            total = sum(vals)
            if self._cpu_prev is not None:
                dt, di = total - self._cpu_prev[0], idle - self._cpu_prev[1]
                if dt > 0:
                    out["cpu:busy_pct"] = round(100.0 * (1 - di / dt), 1)
                    out["cpu:iowait_pct"] = (
                        round(100.0 * (vals[4] - self._cpu_prev[2]) / dt, 1)
                        if len(self._cpu_prev) > 2
                        else None
                    )
            self._cpu_prev = (total, idle, vals[4])
        online = _read(self.sys / "devices/system/cpu/online")
        if online:
            n = 0
            for part in online.split(","):
                a, _, b = part.partition("-")
                n += (int(b) - int(a) + 1) if b else 1
            out["cpu:online"] = n
        freqs = []
        for f in self.sys.glob("devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
            raw = _read(f)
            if raw and raw.isdigit():
                freqs.append(int(raw) / 1000.0)
        if freqs:
            out["cpu:mhz_avg"] = round(sum(freqs) / len(freqs))
            out["cpu:mhz_max"] = round(max(freqs))
        load = (_read(self.proc / "loadavg") or "").split()
        if load:
            out["cpu:load1"] = float(load[0])

    def _mem(self, out: dict) -> None:
        info: dict[str, float] = {}
        for line in (_read(self.proc / "meminfo") or "").splitlines():
            k, _, v = line.partition(":")
            try:
                info[k] = float(v.split()[0]) / 1024.0  # MiB
            except (ValueError, IndexError):
                continue
        if "MemTotal" in info and "MemAvailable" in info:
            out["mem:avail_mib"] = round(info["MemAvailable"])
            out["mem:used_pct"] = round(100.0 * (1 - info["MemAvailable"] / info["MemTotal"]), 1)
        if "SwapTotal" in info and "SwapFree" in info:
            out["mem:swap_used_mib"] = round(info["SwapTotal"] - info["SwapFree"])
        for kind in ("memory", "io", "cpu"):
            raw = _read(self.proc / "pressure" / kind)
            for line in (raw or "").splitlines():
                parts = line.split()
                if parts and parts[0] in ("some", "full") and parts[1].startswith("avg10="):
                    out[f"psi:{kind}_{parts[0]}_avg10"] = float(parts[1].split("=")[1])

    def _disks(self, out: dict, now: float) -> None:
        dt = (now - self._at) if self._at is not None else None
        seen: dict[str, tuple[float, float, float]] = {}
        for line in (_read(self.proc / "diskstats") or "").splitlines():
            f = line.split()
            if len(f) < 14:
                continue
            dev = f[2]
            if not dev.startswith(_DISK_RE) or (dev[-1].isdigit() and "p" in dev[-3:]):
                continue  # whole disks only (nvme0n1, sda, mmcblk0), not partitions
            if dev.startswith("sd") and dev[-1].isdigit():
                continue
            rd, wr, busy_ms = float(f[5]), float(f[9]), float(f[12])  # sectors, sectors, ms
            seen[dev] = (rd, wr, busy_ms)
            prev = self._disk_prev.get(dev)
            if prev and dt and dt > 0:
                out[f"disk:{dev}:read_mbs"] = round((rd - prev[0]) * 512 / 1e6 / dt, 2)
                out[f"disk:{dev}:write_mbs"] = round((wr - prev[1]) * 512 / 1e6 / dt, 2)
                out[f"disk:{dev}:busy_pct"] = round(min(100.0, (busy_ms - prev[2]) / (dt * 10)), 1)
        self._disk_prev = seen

    def read(self) -> dict:
        out: dict = {}
        now = self._clock()
        self._cpu(out)
        self._mem(out)
        self._disks(out, now)
        self._at = now
        return {k: v for k, v in out.items() if v is not None}


def _read(path: Path) -> str | None:
    """One raw read. sysfs zones may fail with EAGAIN/ENODATA (an idle Thor GPU,
    the Orin's idle DLA zones); through Python's buffered text layer an EAGAIN
    surfaces as a ``None`` read and a TypeError, so read the fd directly."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        data = os.read(fd, 4096)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data.decode(errors="replace").strip()


def _millideg(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        c = int(raw) / 1000.0
    except ValueError:
        return None
    return c if MIN_C <= c <= MAX_C else None


def read_zones(sys_root: Path = Path("/sys"), *, trips: bool = False) -> list[Zone]:
    base = sys_root / "class" / "thermal"
    zones: list[Zone] = []
    paths = sorted(
        base.glob("thermal_zone*"), key=lambda p: int(p.name.removeprefix("thermal_zone") or 0)
    )
    for zp in paths:
        idx = int(zp.name.removeprefix("thermal_zone") or 0)
        ztype = _read(zp / "type") or f"zone{idx}"
        trip_list: list[tuple[str, float]] = []
        if trips:
            for tt in sorted(zp.glob("trip_point_*_type")):
                temp = _millideg(_read(tt.with_name(tt.name[: -len("type")] + "temp")))
                kind = _read(tt)
                if temp is not None and kind:
                    trip_list.append((kind, temp))
        zones.append(Zone(idx, ztype, _millideg(_read(zp / "temp")), tuple(trip_list)))
    return zones


def read_fans(sys_root: Path = Path("/sys")) -> dict[str, float]:
    """PWM duty (0-255) and tach RPM where the board exposes them (Jetsons do;
    the Spark's fans are firmware-controlled and not visible)."""
    out: dict[str, float] = {}
    for hw in sorted((sys_root / "class" / "hwmon").glob("hwmon*")):
        name = _read(hw / "name") or hw.name
        for f in ("pwm1", "fan1_input"):
            raw = _read(hw / f)
            if raw is not None:
                try:
                    out[f"{name}.{f}"] = float(raw)
                except ValueError:
                    continue
    return out


_TLIMIT_OK: bool | None = None  # does this driver know temperature.gpu.tlimit?
_GPU_KEYS = (
    "temp_c",
    "power_w",
    "sm_mhz",
    "util_pct",
    "events",
    "gr_mhz",
    "mem_mhz",
    "mem_util_pct",
    "tlimit_c",
)


def _smi(fields: tuple[str, ...], timeout: float) -> str:
    try:
        p = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def read_gpu_nvidia_smi(timeout: float = 3.0) -> dict[str, object]:
    """GPU temperature, power, clock, utilisation, throttle bits and -- where the
    driver reports it -- ``tlimit_c``: degrees of headroom left before the GPU's
    own thermal limit (GB10: 21 C at 73 C, so a limit near 94 C). ``limit_c`` =
    temp + headroom. Older drivers without the field fall back to the rest."""
    global _TLIMIT_OK
    if not shutil.which("nvidia-smi"):
        return {}
    out = ""
    if _TLIMIT_OK is not False:
        out = _smi((*GPU_FIELDS, "temperature.gpu.tlimit"), timeout)
        if _TLIMIT_OK is None:
            _TLIMIT_OK = bool(out)  # remember: one probe, not two processes every tick
    if not out:
        out = _smi(GPU_FIELDS, timeout)
    vals = [v.strip() for v in out.splitlines()[0].split(",")] if out else []
    res: dict[str, object] = {}
    for key, raw in zip(_GPU_KEYS, vals):
        if key == "events":
            res[key] = raw
            continue
        try:
            res[key] = float(raw)
        except ValueError:
            res[key] = None  # e.g. "[N/A]" (GB10 reports no memory clock)
    t, head = res.get("temp_c"), res.get("tlimit_c")
    if isinstance(t, float) and isinstance(head, float):
        res["limit_c"] = t + head
    return res


def read_gpu_jetson(sys_root: Path = Path("/sys")) -> dict[str, object]:
    """GPU load (per mille -> percent) and devfreq clock on a Jetson."""
    res: dict[str, object] = {}
    for load in sys_root.glob("devices/platform/*gpu*/load"):
        raw = _read(load)
        if raw and raw.isdigit():
            res["util_pct"] = int(raw) / 10.0
            break
    for dev in sorted((sys_root / "class" / "devfreq").glob("*")):
        if "gpu" in dev.name:
            raw = _read(dev / "cur_freq")
            if raw and raw.isdigit():
                res[f"{dev.name}.mhz"] = int(raw) / 1e6
    return res


class Sensor:
    def __init__(
        self,
        watched: tuple[str, ...],
        *,
        sys_root: Path = Path("/sys"),
        gpu: str = "none",
        gpu_every_s: float = 1.0,
        clock=time.time,
    ) -> None:
        self.watched = watched
        self.sys_root = sys_root
        self.gpu = gpu
        self.gpu_every_s = gpu_every_s
        self._clock = clock
        self._gpu_cache: dict[str, object] = {}
        self.system = SystemProbe(Path(sys_root).parent / "proc", Path(sys_root))
        self._gpu_at = 0.0

    def _gpu(self, now: float) -> dict[str, object]:
        if self.gpu == "none" or now - self._gpu_at < self.gpu_every_s:
            return self._gpu_cache
        self._gpu_at = now
        if self.gpu == "nvidia-smi":
            self._gpu_cache = read_gpu_nvidia_smi()
        elif self.gpu == "jetson":
            self._gpu_cache = read_gpu_jetson(self.sys_root)
        return self._gpu_cache

    def read(self) -> Reading:
        now = self._clock()
        return Reading(
            t=now,
            zones=read_zones(self.sys_root),
            watched=self.watched,
            fans=read_fans(self.sys_root),
            gpu=self._gpu(now),
            system=self.system.read(),
        )


def capabilities(sys_root: Path = Path("/sys"), root: Path = Path("/")) -> dict[str, object]:
    """What this host lets us read and change, without changing anything."""
    zones = read_zones(sys_root, trips=True)
    fans = read_fans(sys_root)
    docker_sock = root / "var/run/docker.sock"
    jetson = (root / "etc/nv_tegra_release").exists()
    return {
        "zones": [
            {"key": z.key, "type": z.type, "temp_c": z.temp_c, "readable": z.temp_c is not None}
            | {"trips": [{"type": k, "temp_c": t} for k, t in z.trips]}
            for z in zones
        ],
        "fans": fans,
        "read": {
            "thermal_zones": bool(zones),
            "fans": bool(fans),
            "nvidia_smi": shutil.which("nvidia-smi") is not None,
            "tegrastats": shutil.which("tegrastats") is not None,
        },
        "control": {
            "docker_pause": docker_sock.exists(),
            "docker_cpus": docker_sock.exists(),
            "admission_signal": True,
            # Present but NOT driven by heat-governor (root-only or parked):
            "nvpmodel": "present, not driven" if shutil.which("nvpmodel") else "absent",
            "fan_pwm": "present, firmware/nvfancontrol owned" if fans else "absent",
            "gpu_clock_lock": "not driven (parked by operator)",
        },
        "jetson": jetson,
    }

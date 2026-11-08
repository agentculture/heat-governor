"""Fakes shared by the governing tests: sysfs trees captured from the real
boards (2026-10-23) and an in-memory Docker."""

from __future__ import annotations

from pathlib import Path

from heat_governor.docker import Container, DockerError

# (type, millidegrees or None for an unreadable zone, [(trip type, millideg)])
SPARK_ZONES = [
    ("acpitz", t, [("critical", 104800)]) for t in (83800, 71700, 82600, 71400, 83800, 73200, 73300)
]
THOR_ZONES = [
    ("tj-thermal", 47781, [("critical", 114500), ("active", 80000)]),
    ("gpu-thermal", None, [("passive", 109000), ("critical", 114500)]),  # EAGAIN while idle
    ("cpu-thermal", 47875, [("passive", 109000), ("critical", 114500)]),
    ("soc012-thermal", 47687, [("passive", 109000), ("critical", 114500)]),
    ("soc345-thermal", 46968, [("passive", 109000), ("critical", 114500)]),
]
ORIN_ZONES = [
    ("cpu-thermal", 59968, [("passive", 99000), ("critical", 104500)]),
    ("gpu-thermal", 53687, [("passive", 99000), ("critical", 104500)]),
    ("cv0-thermal", None, [("passive", 99000), ("critical", 104500)]),  # ENODATA
    ("cv1-thermal", None, [("passive", 99000), ("critical", 104500)]),
    ("cv2-thermal", None, [("passive", 99000), ("critical", 104500)]),
    ("soc0-thermal", 55500, [("passive", 99000), ("critical", 104500)]),
    ("soc1-thermal", 56937, [("passive", 99000), ("critical", 104500)]),
    ("soc2-thermal", 54812, [("passive", 99000), ("critical", 104500)]),
    ("tj-thermal", 60000, [("active", 75000), ("critical", 104500)]),
]
# Arduino VENTUNO Q (arduino,monza): a representative subset of its 48 zones. Trip types and
# values read from the live VENTUNO Q sysfs, 2026-11-08. Only the two PMIC zones carry a critical
# trip (125 C); cpuss-0-0 also has an active 38 C trip. Idle temps are typical; the PMIC sdram
# zone is the hottest, as on the real box.
_CPU = [("passive", 135000), ("passive", 126000), ("passive", 130000)]
_ACC = [("passive", 135000), ("passive", 115000), ("passive", 130000)]
_TWO = [("passive", 135000), ("passive", 130000)]
_CPUSS = [("passive", 135000), ("passive", 130000), ("active", 38000)]
_PMIC = [("passive", 105000), ("critical", 125000)]
VENTUNOQ_ZONES = [
    ("aoss-0-thermal", 43000, _TWO),
    ("cpu-0-0-0-thermal", 44100, _CPU),
    ("cpu-1-0-0-thermal", 43800, _CPU),
    ("cpuss-0-0-thermal", 43500, _CPUSS),
    ("cpuss-1-0-thermal", 43400, _TWO),
    ("ddrss-0-thermal", 45000, _TWO),
    ("gpuss-0-thermal", 44200, _ACC),
    ("gpuss-1-thermal", 44600, _ACC),
    ("nsp-0-0-0-thermal", 43200, _ACC),
    ("nsp-0-1-0-thermal", 43900, _ACC),
    ("pcie-0-thermal", 42000, _TWO),
    ("pmm8620au-0-therm", 46000, _PMIC),
    ("pmm8650au-1-therm", 48000, _PMIC),
]

GPU_DEVFREQ = "3d00000.gpu"
GPU_FREQS = (479000000, 599000000, 780000000, 877000000)


def make_sys(
    root: Path,
    zones,
    *,
    model: str | None = None,
    fan: tuple | None = None,
    compatible: list[str] | None = None,
    devfreq: bool | None = None,
    ina232: int | None = None,
) -> Path:
    """Build ``root/sys`` (and ``root/proc/device-tree/{model,compatible}``); return the sys root.

    ``compatible`` is the device-tree compatible list (NUL-separated on disk). ``devfreq=True``
    adds the VENTUNO Q GPU node ``class/devfreq/3d00000.gpu``. ``ina232`` (microwatts) adds an
    hwmon named ``ina232`` with ``power1_input``. All default to None: nothing extra is written.
    """
    sysr = root / "sys"
    for i, (ztype, temp, trips) in enumerate(zones):
        z = sysr / "class" / "thermal" / f"thermal_zone{i}"
        z.mkdir(parents=True)
        (z / "type").write_text(ztype + "\n")
        if temp is not None:
            (z / "temp").write_text(f"{temp}\n")
        for j, (kind, t) in enumerate(trips):
            (z / f"trip_point_{j}_type").write_text(kind + "\n")
            (z / f"trip_point_{j}_temp").write_text(f"{t}\n")
    if fan:
        hw = sysr / "class" / "hwmon" / "hwmon0"
        hw.mkdir(parents=True)
        (hw / "name").write_text("pwmfan\n")
        (hw / "pwm1").write_text(f"{fan[0]}\n")
    if ina232 is not None:
        hw = sysr / "class" / "hwmon" / "hwmon1"
        hw.mkdir(parents=True)
        (hw / "name").write_text("ina232\n")
        (hw / "power1_input").write_text(f"{ina232}\n")
    if devfreq:
        gpu = sysr / "class" / "devfreq" / GPU_DEVFREQ
        gpu.mkdir(parents=True)
        (gpu / "cur_freq").write_text(f"{GPU_FREQS[0]}\n")
        (gpu / "max_freq").write_text(f"{GPU_FREQS[-1]}\n")
        (gpu / "available_frequencies").write_text(" ".join(map(str, GPU_FREQS)) + " \n")
    if model or compatible:
        dt = root / "proc" / "device-tree"
        dt.mkdir(parents=True, exist_ok=True)
        if model:
            (dt / "model").write_text(model + "\x00")
        if compatible:
            (dt / "compatible").write_bytes(b"".join(c.encode() + b"\x00" for c in compatible))
    return sysr


def set_temp(sysr: Path, index: int, millideg: int) -> None:
    (sysr / "class" / "thermal" / f"thermal_zone{index}" / "temp").write_text(f"{millideg}\n")


class FakeDocker:
    def __init__(self, *containers: Container) -> None:
        self.c = {c.id: c for c in containers}
        self.calls: list[tuple] = []

    def _get(self, ref: str) -> Container:
        for c in self.c.values():
            if ref in (c.id, c.name):
                return c
        raise DockerError(f"no such container {ref}")

    def inspect(self, ref: str) -> Container:
        return self._get(ref)

    def labelled(self) -> list[Container]:
        return [c for c in self.c.values() if c.role]

    def names(self) -> list[str]:
        return [c.name for c in self.c.values()]

    def _set(self, ref: str, **kw) -> None:
        c = self._get(ref)
        self.c[c.id] = Container(**{**c.__dict__, **kw})

    def pause(self, ref: str) -> None:
        self.calls.append(("pause", ref))
        if self._get(ref).state == "paused":
            raise DockerError("already paused")
        self._set(ref, state="paused")

    def unpause(self, ref: str) -> None:
        self.calls.append(("unpause", ref))
        self._set(ref, state="running")

    def set_nano_cpus(self, ref: str, nano: int) -> None:
        self.calls.append(("cpus", ref, nano))
        self._set(ref, nano_cpus=nano)


def container(cid: str, name: str, role: str | None, *, state="running", cap=False, nano=0):
    return Container(cid, name, state, role, cap, nano)


class FakeSystemctl:
    """In-memory ``systemctl --user``: ``units`` maps a unit name to its quota
    (usec per second, None = no quota); a name not in it does not exist."""

    def __init__(self, units: dict | None = None, *, inactive=(), ledger=None) -> None:
        from heat_governor.units import UnitInfo

        self._info = UnitInfo
        self.units: dict[str, int | None] = dict(units or {})
        self.inactive = set(inactive)
        self.calls: list[tuple] = []
        self.ledger_path = ledger  # when set: what was on disk at each set_quota
        self.on_disk: list[dict] = []
        self.usage: dict[str, int] = {}  # unit -> cumulative CPU usec (settable)
        self.usage_reads = 0  # how often cpu_usage_usec was called

    def show(self, unit: str):
        self.calls.append(("show", unit))
        if unit not in self.units:
            return self._info(loaded=False, active=False, quota_usec=None)
        return self._info(True, unit not in self.inactive, self.units[unit])

    def set_quota(self, unit: str, percent: str | None) -> None:
        from heat_governor.units import UnitMissing

        self.calls.append(("set", unit, percent))
        if self.ledger_path is not None:
            import json

            self.on_disk.append(json.loads(self.ledger_path.read_text()))
        if unit not in self.units:
            raise UnitMissing(f"Unit {unit} not found.")
        self.units[unit] = None if percent is None else int(float(percent) * 10_000)

    def cpu_usage_usec(self, unit: str) -> int | None:
        self.usage_reads += 1
        return self.usage.get(unit) if unit in self.units else None


class FakeGpuClock:
    """The GPU clock helper, in memory. ``hw`` is the ceiling the hardware shows
    (what the helper's ``status`` reads back on a Jetson: devfreq max_freq);
    ``visible=False`` is the DGX Spark (GB10), whose status shows no lock at all.
    :meth:`hand_lock` is the operator's own ``sudo heat-governor-gpuclock lock``,
    made behind the daemon's back."""

    def __init__(self, *, hw_max: int = 1300, visible: bool = True, available: bool = True):
        self.helper = "/fake/heat-governor-gpuclock"
        self.available = available
        self.reason = "ok" if available else "helper not installed"
        self.ceiling_mhz: int | None = None
        self.hw_max = hw_max
        self.hw = hw_max
        self.visible = visible
        self.calls: list[tuple] = []
        self.reads = 0
        self.probes = 0

    def probe(self) -> bool:
        self.probes += 1
        return self.available

    def lock(self, mhz: int):
        self.calls.append(("lock", mhz))
        self.ceiling_mhz = mhz
        self.hw = min(mhz, self.hw_max)
        return True, str(mhz)

    def reset(self):
        self.calls.append(("reset",))
        self.ceiling_mhz = None
        self.hw = self.hw_max
        return True, "reset"

    def read_ceiling(self) -> int | None:
        self.reads += 1
        return self.hw if self.visible else None

    def hand_lock(self, mhz: int) -> None:
        self.hw = mhz

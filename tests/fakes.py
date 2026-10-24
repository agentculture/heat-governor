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


def make_sys(root: Path, zones, *, model: str | None = None, fan: tuple | None = None) -> Path:
    """Build ``root/sys`` (and ``root/proc/device-tree/model``); return the sys root."""
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
    if model:
        dt = root / "proc" / "device-tree"
        dt.mkdir(parents=True)
        (dt / "model").write_text(model + "\x00")
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

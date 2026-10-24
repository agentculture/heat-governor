"""CPU quota on the operator's own systemd *user* units (opt-in).

The heat a governed container makes is not the only heat. On the DGX Spark,
2026-10-23 night, parallel interactive sessions (Claude Code running test
suites, cypher-shell) pushed the load average to 65-81 and the board zone to
95.2 C, and a CPU cap on the serving container cannot reach that load. Those
sessions run under the user manager (``app.slice`` and its scopes), whose
``cpu`` controller is delegated, so ``systemctl --user set-property`` can cap
them without root.

The actuator here follows the same rules as the container actuators:

* the cap per unit comes from the pure rule :func:`heat_governor.policy.unit_cpus`
  on the controller's level;
* the unit's original quota is read fresh (``systemctl --user show``) and written
  to the ledger BEFORE the first change, then restored on exit, when the unit
  leaves the configuration, and by :meth:`Ledger.recover` after a crash;
* every change is ``--runtime``: it lives under ``/run`` and a reboot drops it,
  so a cap can never outlive the machine's uptime;
* a band never RAISES a quota tighter than the unit's own;
* ``apply=False`` logs ``dry-run would ...`` and changes nothing;
* a unit that does not exist (or is inactive) is reported once and skipped,
  then looked for again every :attr:`UnitCapper.RETRY_S`.

Nothing is governed unless the config lists a unit (``[[units]]``).
Docker containers live in ``system.slice`` and are never affected.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed argv only
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

Log = Callable[[str], None]
Run = Callable[[list[str]], "tuple[int, str, str]"]


class UnitError(RuntimeError):
    pass


class UnitMissing(UnitError):
    """The unit does not exist (or is not active): nothing to cap."""


@dataclass(frozen=True)
class UnitInfo:
    loaded: bool
    active: bool
    quota_usec: int | None  # CPU time per second of wall time; None = no quota


_SPAN = re.compile(r"(\d+(?:\.\d+)?)(us|µs|ms|s|min|h)")
_USEC = {"us": 1, "µs": 1, "ms": 1_000, "s": 1_000_000, "min": 60_000_000, "h": 3_600_000_000}


def parse_quota(text: str) -> int | None:
    """``CPUQuotaPerSecUSec`` as systemd prints it (``infinity``, ``1.500000s``,
    ``1min 10s``, ``800ms``) -> microseconds per second, or None for no quota."""
    text = text.strip()
    if text in ("", "infinity"):
        return None
    total, rest = 0.0, text
    for m in _SPAN.finditer(text):
        total += float(m.group(1)) * _USEC[m.group(2)]
        rest = rest.replace(m.group(0), "", 1)
    if rest.strip() or total <= 0:
        raise UnitError(f"cannot read CPUQuotaPerSecUSec={text!r}")
    return round(total)


def percent(usec: int) -> str:
    """Microseconds per second -> the ``CPUQuota=`` percentage (100% = one CPU)."""
    return f"{usec // 10_000}" if usec % 10_000 == 0 else f"{usec / 10_000:.2f}"


def cpus_percent(cpus: float) -> str:
    return percent(max(10_000, round(cpus * 1_000_000)))


def _run(argv: list[str]) -> tuple[int, str, str]:  # pragma: no cover - thin shell
    try:
        # fixed argv; the unit name is validated by the config (UNIT_NAME)
        p = subprocess.run(  # nosec B603
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return p.returncode, p.stdout or "", p.stderr or ""


class SystemctlLike(Protocol):
    def show(self, unit: str) -> UnitInfo: ...
    def set_quota(self, unit: str, percent: str | None) -> None: ...
    def cpu_usage_usec(self, unit: str) -> int | None: ...


CGROUP_ROOT = Path("/sys/fs/cgroup")


def read_usage_usec(stat: Path) -> int | None:
    """``usage_usec`` from a cgroup v2 ``cpu.stat``; None when unreadable."""
    try:
        text = stat.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec" and value.strip().isdigit():
            return int(value)
    return None


class Systemctl:
    """``systemctl --user``, the two calls the actuator needs. Every change is
    ``set-property --runtime``; ``--`` ends the options before the unit name."""

    def __init__(
        self, run: Run = _run, exe: str = "systemctl", cgroup_root: Path = CGROUP_ROOT
    ) -> None:
        self.run = run
        self.exe = exe
        self.cgroup_root = cgroup_root
        self._cgroups: dict[str, Path] = {}  # unit -> its cpu.stat (cached)

    def show(self, unit: str) -> UnitInfo:
        props = ("LoadState", "ActiveState", "CPUQuotaPerSecUSec")
        argv = [self.exe, "--user", "show"]
        for p in props:
            argv += ["-p", p]
        rc, out, err = self.run([*argv, "--", unit])
        if rc != 0:
            raise UnitError(f"systemctl --user show {unit}: {(err or out).strip() or rc}")
        kv = {k: v for k, sep, v in (line.partition("=") for line in out.splitlines()) if sep}
        return UnitInfo(
            loaded=kv.get("LoadState") == "loaded",
            active=kv.get("ActiveState") in ("active", "activating", "reloading"),
            quota_usec=parse_quota(kv.get("CPUQuotaPerSecUSec", "infinity")),
        )

    def _cpu_stat(self, unit: str) -> Path | None:
        """``<cgroup_root><ControlGroup>/cpu.stat`` for ``unit``, or None (no
        cgroup: inactive, or the path would leave the cgroup root)."""
        rc, out, _ = self.run([self.exe, "--user", "show", "-p", "ControlGroup", "--", unit])
        if rc != 0:
            return None
        cg = next(
            (
                ln.split("=", 1)[1].strip()
                for ln in out.splitlines()
                if ln.startswith("ControlGroup=")
            ),
            "",
        )
        if not cg.startswith("/") or ".." in cg.split("/"):
            return None
        return self.cgroup_root / cg.lstrip("/") / "cpu.stat"

    def cpu_usage_usec(self, unit: str) -> int | None:
        """The unit's cumulative CPU time in microseconds (cgroup v2
        ``cpu.stat`` ``usage_usec``). The cgroup path is cached and looked up
        again once when its file has gone (the unit restarted)."""
        cached = self._cgroups.get(unit)
        if cached is not None:
            usec = read_usage_usec(cached)
            if usec is not None:
                return usec
            self._cgroups.pop(unit, None)
        stat = self._cpu_stat(unit)
        if stat is None:
            return None
        usec = read_usage_usec(stat)
        if usec is not None:
            self._cgroups[unit] = stat
        return usec

    def set_quota(self, unit: str, percent: str | None) -> None:
        value = "CPUQuota=" if percent is None else f"CPUQuota={percent}%"
        rc, out, err = self.run(
            [self.exe, "--user", "set-property", "--runtime", "--", unit, value]
        )
        if rc != 0:
            msg = (err or out).strip() or str(rc)
            if "not found" in msg or "not loaded" in msg:
                raise UnitMissing(msg)
            raise UnitError(f"systemctl --user set-property {unit} {value}: {msg}")


def containing(units, cgroup_file: Path = Path("/proc/self/cgroup")) -> list[str]:
    """The configured unit names this process itself runs inside (its own cap
    then bounds the governor too). Unreadable cgroup file: none."""
    try:
        text = cgroup_file.read_text()
    except OSError:
        return []
    parts = {seg for line in text.splitlines() for seg in line.rpartition(":")[2].split("/")}
    return sorted(u for u in units if u in parts)


def _shown(usec: int | None) -> str:
    return "none" if usec is None else f"{percent(usec)}%"


class UnitCapper:
    """Holds one CPUQuota per configured user unit, ledger-first."""

    RETRY_S = 30.0  # how often a missing / inactive unit is looked for again

    def __init__(
        self,
        systemctl: SystemctlLike,
        ledger,
        log: Log,
        *,
        apply: bool,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.systemctl = systemctl
        self.ledger = ledger
        self.log = log
        self.apply = apply
        self.clock = clock
        self.current: dict[str, float | None] = {}
        self._missing: dict[str, float] = {}  # unit -> when to look again
        self.dry: set[str] = set()  # units configured dry_run = true: log only

    def set_dry(self, units: set[str]) -> None:
        """Mark ``units`` dry-run. A unit whose mode flips forgets what it last
        "set" (a dry-run cap was never made), so the next level is applied anew."""
        for unit in self.dry ^ units:
            self.current.pop(unit, None)
        self.dry = set(units)

    def applies(self, unit: str) -> bool:
        """Whether a change to ``unit`` is made (else logged "dry-run would ...")."""
        return self.apply and unit not in self.dry

    def missing_units(self) -> list[str]:
        return sorted(self._missing)

    def set(self, unit: str, cpus: float | None) -> None:
        """``cpus=None`` restores the unit's own quota."""
        if self.current.get(unit, "unset") == cpus:
            return
        if cpus is None and unit not in self.ledger.unit_quota:
            self.current[unit] = None
            return
        now = self.clock()
        if unit in self._missing and now < self._missing[unit]:
            return
        prefix = "" if self.applies(unit) else "dry-run would "
        try:
            if cpus is None:
                self._restore(unit, prefix)
            else:
                self._cap(unit, cpus, prefix)
        except UnitMissing as exc:
            if unit not in self._missing:
                self.log(f"unit {unit}: {exc}; skipped (looked for again every {self.RETRY_S:g} s)")
            self._missing[unit] = now + self.RETRY_S
            return
        if self._missing.pop(unit, None) is not None:
            self.log(f"unit {unit} is back: governing it")
        self.current[unit] = cpus

    def _original(self, unit: str) -> int | None:
        """The unit's quota right now, read fresh (never a remembered value)."""
        info = self.systemctl.show(unit)
        if not info.loaded:
            raise UnitMissing("not found")
        if not info.active:
            raise UnitMissing("inactive")
        return info.quota_usec

    def _cap(self, unit: str, cpus: float, prefix: str) -> None:
        if unit in self.ledger.unit_quota:
            original = self.ledger.unit_quota[unit]
        else:
            original = self._original(unit)
            if self.applies(unit):
                self.ledger.unit_quota[unit] = original
                self.ledger.save()  # record BEFORE acting
        want = round(cpus * 1_000_000)
        if original is not None and original < want:
            want = original  # never raise the unit's own, tighter quota
        pct = percent(max(10_000, want))
        if self.applies(unit):
            self.systemctl.set_quota(unit, pct)
        self.log(f"{prefix}cap unit {unit} CPUQuota={pct}% (cpus={cpus:g})")

    def _restore(self, unit: str, prefix: str) -> None:
        original = self.ledger.unit_quota[unit]
        if self.applies(unit):
            try:
                self.systemctl.set_quota(unit, None if original is None else percent(original))
            except UnitMissing:
                self.ledger.unit_quota.pop(unit, None)
                self.ledger.save()
                self.log(f"unit {unit} is gone: nothing to restore")
                return
            self.ledger.unit_quota.pop(unit, None)
            self.ledger.save()
        self.log(f"{prefix}restore unit {unit} CPUQuota={_shown(original)}")

    def release(self, units) -> None:
        """Restore ``units`` (they left the configuration, or the daemon stops)."""
        for unit in sorted(units):
            try:
                if unit in self.ledger.unit_quota:
                    self._restore(unit, "" if self.applies(unit) else "dry-run would ")
            except UnitError as exc:
                self.log(f"restore unit {unit} failed: {exc} (kept on record)")
            self.current.pop(unit, None)
            self._missing.pop(unit, None)

    def release_all(self) -> None:
        self.release(set(self.ledger.unit_quota) | set(self.current))


def recover_units(ledger, systemctl: SystemctlLike, log: Log) -> None:
    """Restore every unit quota a previous (crashed) run left on record; drop an
    entry once restored or when the unit is gone, keep it when the call failed."""
    for unit, original in sorted(ledger.unit_quota.items()):
        try:
            systemctl.set_quota(unit, None if original is None else percent(original))
            log(f"recover: restored unit {unit} CPUQuota={_shown(original)}")
            del ledger.unit_quota[unit]
        except UnitMissing:
            del ledger.unit_quota[unit]
            log(f"recover: unit {unit} is gone")
        except UnitError as exc:
            log(f"recover: unit {unit}: {exc} (kept on record)")

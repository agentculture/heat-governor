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
* ``apply=False`` (or a unit's ``dry_run``) logs ``dry-run would cap ...`` and
  ``dry-run would restore ...`` and changes nothing; a ledger entry, though,
  means a real change was made, so it is always restored for real;
* a unit that does not exist (or is inactive) is reported once and skipped,
  then looked for again every :attr:`UnitCapper.RETRY_S`; any other systemctl
  error is logged once per message and retried at the same pace;
* a unit gone before its restore keeps its original as an orphan
  (``Ledger.unit_orphans``), restored first when the unit is seen again.

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
NOT_FOUND = "not found"
RETRY_S = 30.0  # how often a missing / failing unit or an unresolved cgroup is tried again


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

    RETRY_S = RETRY_S  # an unresolvable / unreadable cgroup is looked up again after this

    def __init__(
        self,
        run: Run = _run,
        exe: str = "systemctl",
        cgroup_root: Path = CGROUP_ROOT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.run = run
        self.exe = exe
        self.cgroup_root = cgroup_root
        self.clock = clock
        self._cgroups: dict[str, Path] = {}  # unit -> its cpu.stat (cached)
        self._no_cgroup: dict[str, float] = {}  # unit -> when to look its cgroup up again

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
        again once when its file has gone (the unit restarted); a lookup that
        finds no readable ``cpu.stat`` is not repeated for RETRY_S."""
        cached = self._cgroups.get(unit)
        if cached is not None:
            usec = read_usage_usec(cached)
            if usec is not None:
                return usec
            self._cgroups.pop(unit, None)
        now = self.clock()
        if now < self._no_cgroup.get(unit, now):
            return None
        stat = self._cpu_stat(unit)
        usec = None if stat is None else read_usage_usec(stat)
        if usec is None:
            self._no_cgroup[unit] = now + self.RETRY_S
            return None
        self._no_cgroup.pop(unit, None)
        self._cgroups[unit] = stat
        return usec

    def set_quota(self, unit: str, percent: str | None) -> None:
        value = "CPUQuota=" if percent is None else f"CPUQuota={percent}%"
        rc, out, err = self.run(
            [self.exe, "--user", "set-property", "--runtime", "--", unit, value]
        )
        if rc != 0:
            msg = (err or out).strip() or str(rc)
            if NOT_FOUND in msg or "not loaded" in msg:
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


def _quota_arg(usec: int | None) -> str | None:
    return None if usec is None else percent(usec)


DRY = "dry-run would "


class UnitCapper:
    """Holds one CPUQuota per configured user unit, ledger-first."""

    RETRY_S = RETRY_S

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
        self._retry_at: dict[str, float] = {}  # unit -> when to try again
        self._missing: set[str] = set()  # not found / inactive
        self._errors: dict[str, str] = {}  # unit -> the last systemctl error logged
        self.dry: set[str] = set()  # units configured dry_run = true: log only
        self.would: dict[str, float] = {}  # dry-run: unit -> the cap it would hold

    def set_dry(self, units: set[str]) -> None:
        """Mark ``units`` dry-run. A unit whose mode flips forgets what it last
        "set" (a dry-run cap was never made), so the next level is applied anew;
        a real cap it still holds is restored for real on that next change."""
        for unit in self.dry ^ units:
            self.current.pop(unit, None)
            self.would.pop(unit, None)
        self.dry = set(units)

    def applies(self, unit: str) -> bool:
        """Whether a change to ``unit`` is made (else logged "dry-run would ...")."""
        return self.apply and unit not in self.dry

    def missing_units(self) -> list[str]:
        return sorted(self._missing)

    def _holds(self, unit: str) -> bool:
        """Whether a restore of ``unit`` has anything to do."""
        led = self.ledger
        return unit in led.unit_quota or unit in led.unit_orphans or unit in self.would

    def set(self, unit: str, cpus: float | None) -> None:
        """``cpus=None`` restores the unit's own quota."""
        if self.current.get(unit, "unset") == cpus:
            return
        if cpus is None and not self._holds(unit):
            self.current[unit] = None
            return
        now = self.clock()
        if now < self._retry_at.get(unit, now):
            return
        try:
            self._change(unit, cpus)
        except UnitMissing as exc:
            self._missed(unit, exc, now)
            return
        except UnitError as exc:
            self._failed(unit, exc, now)
            return
        self._answered(unit)
        self.current[unit] = cpus

    def _missed(self, unit: str, exc: UnitError, now: float) -> None:
        if unit not in self._missing:
            self.log(f"unit {unit}: {exc}; skipped (looked for again every {self.RETRY_S:g} s)")
        self._missing.add(unit)
        self._retry_at[unit] = now + self.RETRY_S

    def _failed(self, unit: str, exc: UnitError, now: float) -> None:
        """Any other systemctl failure (no user bus, controller not delegated):
        logged once per distinct message, then tried again every RETRY_S."""
        if self._errors.get(unit) != str(exc):
            self._errors[unit] = str(exc)
            self.log(f"unit {unit}: {exc}; tried again every {self.RETRY_S:g} s")
        self._retry_at[unit] = now + self.RETRY_S

    def _answered(self, unit: str) -> None:
        self._retry_at.pop(unit, None)
        if unit in self._missing:
            self._missing.discard(unit)
            self.log(f"unit {unit} is back: governing it")
        if self._errors.pop(unit, None) is not None:
            self.log(f"unit {unit}: systemctl answers again: governing it")

    def _change(self, unit: str, cpus: float | None) -> None:
        if self.apply:
            self._adopt_orphan(unit)
        if not self.applies(unit):
            if self.apply and unit in self.ledger.unit_quota:
                self._restore(unit)  # a real cap made before the unit turned dry
            self._dry_change(unit, cpus)
            return
        if cpus is not None:
            self._cap(unit, cpus)
        elif unit in self.ledger.unit_quota:
            self._restore(unit)

    def _original(self, unit: str) -> int | None:
        """The unit's quota right now, read fresh (never a remembered value)."""
        info = self.systemctl.show(unit)
        if not info.loaded:
            raise UnitMissing(NOT_FOUND)
        if not info.active:
            raise UnitMissing("inactive")
        return info.quota_usec

    def _adopt_orphan(self, unit: str) -> None:
        """A unit that vanished while capped may load again with our ``--runtime``
        drop-in still under /run: its orphaned original becomes the ledger's
        original again (so it is restored, and never replaced by our own cap)."""
        orphans = self.ledger.unit_orphans
        if unit not in orphans:
            return
        if not self.systemctl.show(unit).loaded:
            raise UnitMissing(NOT_FOUND)
        self.ledger.unit_quota[unit] = orphans.pop(unit)
        self.ledger.save()
        self.log(f"unit {unit} is back: restoring the original it had before it vanished")

    def _want(self, cpus: float, original: int | None) -> str:
        want = round(cpus * 1_000_000)
        if original is not None and original < want:
            want = original  # never raise the unit's own, tighter quota
        return percent(max(10_000, want))

    def _cap(self, unit: str, cpus: float) -> None:
        if unit in self.ledger.unit_quota:
            original = self.ledger.unit_quota[unit]
        else:
            original = self._original(unit)
            self.ledger.unit_quota[unit] = original
            self.ledger.save()  # record BEFORE acting
        pct = self._want(cpus, original)
        self.systemctl.set_quota(unit, pct)
        self.log(f"cap unit {unit} CPUQuota={pct}% (cpus={cpus:g})")

    def _dry_change(self, unit: str, cpus: float | None) -> None:
        """Dry-run: read the original fresh, log what would happen, write nothing."""
        if cpus is None:
            if unit in self.would:
                original = self._original(unit)
                del self.would[unit]
                self.log(f"{DRY}restore unit {unit} CPUQuota={_shown(original)}")
            return
        pct = self._want(cpus, self._original(unit))
        self.would[unit] = cpus
        self.log(f"{DRY}cap unit {unit} CPUQuota={pct}% (cpus={cpus:g})")

    def _restore(self, unit: str) -> None:
        """Put the ledger's original back. A ledger entry means a real change was
        made, so this is real whenever the daemon applies, whatever ``dry_run``
        says. A unit gone meanwhile keeps its original as an orphan."""
        original = self.ledger.unit_quota[unit]
        if not self.apply:
            self.log(f"{DRY}restore unit {unit} CPUQuota={_shown(original)}")
            return
        try:
            self.systemctl.set_quota(unit, _quota_arg(original))
        except UnitMissing:
            orphan(self.ledger, unit)
            self.log(f"unit {unit} is gone: its original is kept until it appears again")
            raise
        self.ledger.unit_quota.pop(unit, None)
        self.ledger.save()
        self.log(f"restore unit {unit} CPUQuota={_shown(original)}")

    def release(self, units) -> None:
        """Restore ``units`` (they left the configuration, or the daemon stops)."""
        for unit in sorted(units):
            try:
                if unit in self.ledger.unit_quota:
                    self._restore(unit)
                elif unit in self.would:
                    self._dry_change(unit, None)
            except UnitMissing:
                pass  # logged by _restore; the original is kept as an orphan
            except UnitError as exc:
                self.log(f"restore unit {unit} failed: {exc} (kept on record)")
            self.current.pop(unit, None)
            self.would.pop(unit, None)
            self._missing.discard(unit)
            self._retry_at.pop(unit, None)
            self._errors.pop(unit, None)

    def release_all(self) -> None:
        self.release(set(self.ledger.unit_quota) | set(self.current) | set(self.would))


def orphan(ledger, unit: str) -> None:
    """Move ``unit``'s original from the ledger to its orphans (persisted): the
    unit vanished before it could be restored, but our ``--runtime`` drop-in can
    still apply when it loads again."""
    ledger.unit_orphans[unit] = ledger.unit_quota.pop(unit)
    ledger.save()


def recover_units(ledger, systemctl: SystemctlLike, log: Log) -> None:
    """Restore every unit quota a previous (crashed) run left on record; drop an
    entry once restored, keep it when the call failed, and keep a missing unit's
    original as an orphan. An orphan whose unit is loaded again is restored."""
    for unit, original in sorted(ledger.unit_quota.items()):
        try:
            systemctl.set_quota(unit, _quota_arg(original))
            log(f"recover: restored unit {unit} CPUQuota={_shown(original)}")
            del ledger.unit_quota[unit]
        except UnitMissing:
            orphan(ledger, unit)
            log(f"recover: unit {unit} is gone: its original is kept until it appears again")
        except UnitError as exc:
            log(f"recover: unit {unit}: {exc} (kept on record)")
    for unit, original in sorted(ledger.unit_orphans.items()):
        _recover_orphan(ledger, systemctl, log, unit, original)


def _recover_orphan(ledger, systemctl: SystemctlLike, log: Log, unit: str, original) -> None:
    try:
        if not systemctl.show(unit).loaded:
            return
        systemctl.set_quota(unit, _quota_arg(original))
    except UnitError as exc:
        log(f"recover: orphaned unit {unit}: {exc} (kept on record)")
        return
    del ledger.unit_orphans[unit]
    log(f"recover: restored orphaned unit {unit} CPUQuota={_shown(original)}")

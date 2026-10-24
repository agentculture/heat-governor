"""Actuators: CPU caps and duty-cycle pausing, under the own-pause rule.

Rules every actuator here keeps:

* **Own pauses only.** A container found paused by someone else is left alone
  (and noted once). Only a pause this process issued is ever undone.
* **Serving containers are never paused.** Only role ``batch`` is duty-cycled;
  a ``serving`` container may at most get a CPU cap, and only when its
  ``heat-governor.cpu-cap=true`` label (or the operator's flag) allows it.
* **Crash safety.** Every pause and every first CPU change is written to a
  ledger file *before* it is made. A restarted governor calls :meth:`Ledger.recover`
  and undoes exactly what the ledger says it did: unpause its own pauses,
  restore the original ``NanoCpus`` (and user-unit quotas, :mod:`heat_governor.units`).
* **The original is read fresh.** The limit recorded as a container's original
  comes from a ``docker inspect`` made right before the first cap, never from the
  discovery snapshot, which can still show a cap this process just removed.
* **Dry run** (``apply=False``) logs what it would do and changes nothing.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from heat_governor.docker import Container, DockerError

Log = Callable[[str], None]


class DockerLike(Protocol):
    def inspect(self, ref: str) -> Container: ...
    def pause(self, ref: str) -> None: ...
    def unpause(self, ref: str) -> None: ...
    def set_nano_cpus(self, ref: str, nano: int) -> None: ...


def restore_value(nano: int) -> int:
    """The NanoCpus to write back for an original limit. Docker's update treats 0
    as "leave unchanged", so an original of 0 (no limit) is restored as every
    host CPU -- the same thing in effect (measured on the DGX Spark, 2026-10-23:
    ``docker update --cpus 0`` left a 16-CPU cap in place)."""
    return nano if nano > 0 else (os.cpu_count() or 1) * 1_000_000_000


def _quotas(raw) -> dict[str, int | None]:
    return {k: None if v is None else int(v) for k, v in (raw or {}).items()}


class Ledger:
    """``{"paused": [ids], "nano_cpus": {id: original}, "gpu_locked": bool,
    "unit_quota": {unit: original usec per s, or null for none},
    "unit_orphans": {unit: original}}`` (originals of units that vanished before
    they could be restored), written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.paused: set[str] = set()
        self.nano_cpus: dict[str, int] = {}
        self.gpu_locked = False
        self.unit_quota: dict[str, int | None] = {}
        self.unit_orphans: dict[str, int | None] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.paused = set(data.get("paused", []))
        self.nano_cpus = {k: int(v) for k, v in (data.get("nano_cpus") or {}).items()}
        self.gpu_locked = bool(data.get("gpu_locked", False))
        self.unit_quota = _quotas(data.get("unit_quota"))
        self.unit_orphans = _quotas(data.get("unit_orphans"))

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "paused": sorted(self.paused),
                        "nano_cpus": self.nano_cpus,
                        "gpu_locked": self.gpu_locked,
                        "unit_quota": self.unit_quota,
                        "unit_orphans": self.unit_orphans,
                    }
                )
            )
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)

    def recover(
        self, docker: DockerLike | None, log: Log, *, gpu=None, units=None
    ) -> dict[str, tuple[int, int]]:
        """Undo what a previous (crashed) run left behind -- only its own actions.
        ``units`` is a :class:`heat_governor.units.SystemctlLike`. Returns, per
        container restored, ``(NanoCpus written, the original it stands for)``
        to seed :attr:`CpuCapper.restored` (an original of 0 is written as every
        host CPU, which the next cap must not record as the original)."""
        restored: dict[str, tuple[int, int]] = {}
        if self.gpu_locked and gpu is not None:
            ok, out = gpu.reset()
            log(f"recover: gpu ceiling {'removed' if ok else 'reset FAILED: ' + out}")
            self.gpu_locked = not ok
        if docker is not None:
            # drop an entry only once its action is undone (or the container is gone):
            # a failed restore keeps the original limit on record for the next try
            self._recover_paused(docker, log)
            restored = self._recover_caps(docker, log)
        if units is not None and (self.unit_quota or self.unit_orphans):
            from heat_governor.units import recover_units

            recover_units(self, units, log)
        self.save()
        return restored

    def _recover_paused(self, docker: DockerLike, log: Log) -> None:
        for cid in sorted(self.paused):
            try:
                if docker.inspect(cid).state == "paused":
                    docker.unpause(cid)
                    log(f"recover: unpaused {cid[:12]} (a pause a previous run made)")
                self.paused.discard(cid)
            except DockerError as exc:
                if "404" in str(exc):
                    self.paused.discard(cid)
                log(f"recover: {cid[:12]}: {exc}")

    def _recover_caps(self, docker: DockerLike, log: Log) -> dict[str, tuple[int, int]]:
        restored: dict[str, tuple[int, int]] = {}
        for cid, nano in sorted(self.nano_cpus.items()):
            try:
                docker.set_nano_cpus(cid, restore_value(nano))
                log(f"recover: restored {cid[:12]} NanoCpus={nano}")
                restored[cid] = (restore_value(nano), nano)
                del self.nano_cpus[cid]
            except DockerError as exc:
                if "404" in str(exc):
                    del self.nano_cpus[cid]
                log(f"recover: {cid[:12]}: {exc} (kept on record)")
        return restored


class CpuCapper:
    def __init__(self, docker: DockerLike, ledger: Ledger, log: Log, *, apply: bool) -> None:
        self.docker = docker
        self.ledger = ledger
        self.log = log
        self.apply = apply
        self.current: dict[str, int | None] = {}
        self.set_at: dict[str, float] = {}
        # cid -> (NanoCpus our last restore wrote, the original it stands for); lets
        # an original of 0 (no limit) survive a restore -> cap cycle as 0
        self.restored: dict[str, tuple[int, int]] = {}

    def set(self, c: Container, cpus: int | None, *, observed_at: float | None = None) -> None:
        """``cpus=None`` restores the container's own limit. ``observed_at`` is
        when ``c`` was inspected; only a snapshot newer than our own last change
        can show that someone else changed the limit."""
        if self.current.get(c.id, "unset") == cpus:
            self._reassert(c, cpus, observed_at)
            return
        if cpus is None and c.id not in self.ledger.nano_cpus:
            self.current[c.id] = None
            return
        prefix = "" if self.apply else "dry-run would "
        if cpus is None:
            self._restore(c, prefix)
        else:
            self._cap(c, cpus, prefix)
        self.current[c.id] = cpus
        self.set_at[c.id] = time.time()

    def _reassert(self, c: Container, cpus: int | None, observed_at: float | None) -> None:
        """Someone else changed the limit while we hold a cap: put ours back."""
        fresh = observed_at is not None and observed_at > self.set_at.get(c.id, float("inf"))
        if cpus is not None and self.apply and fresh and c.nano_cpus != int(cpus * 1e9):
            self.set_at[c.id] = time.time()
            self.docker.set_nano_cpus(c.id, int(cpus * 1e9))
            self.log(f"re-assert {c.name} cpus={cpus} (was changed outside the governor)")

    def _restore(self, c: Container, prefix: str) -> None:
        nano = self.ledger.nano_cpus[c.id]
        if self.apply:
            self._write_back(c.id, nano)
        shown = "unlimited" if nano == 0 else f"{nano / 1e9:g}"
        self.log(f"{prefix}restore {c.name} cpus={shown}")

    def _write_back(self, cid: str, nano: int) -> None:
        """Restore ``nano`` (the ledger's original) and drop the ledger entry."""
        self.docker.set_nano_cpus(cid, restore_value(nano))
        self.restored[cid] = (restore_value(nano), nano)
        self.ledger.nano_cpus.pop(cid, None)
        self.ledger.save()

    def original(self, cid: str) -> int:
        """The container's own limit, read fresh from docker right now.

        Never the discovery snapshot: that is up to ``discover_every_s`` old and,
        right after a restore, still shows our previous CAP (2026-10-23 on the
        Spark: a stale 12 became the "original" and every later restore wrote 12
        back). A value equal to what our own last restore wrote stands for the
        original that restore wrote back (so 0, "no limit", survives as 0).
        Raises :class:`DockerError` when the limit cannot be read: no cap then."""
        fresh = self.docker.inspect(cid).nano_cpus
        wrote = self.restored.get(cid)
        if wrote is not None and fresh == wrote[0]:
            return wrote[1]
        return fresh

    def _cap(self, c: Container, cpus: int, prefix: str) -> None:
        if self.apply:
            if c.id not in self.ledger.nano_cpus:
                self.ledger.nano_cpus[c.id] = self.original(c.id)
                self.ledger.save()  # record BEFORE acting
            self.docker.set_nano_cpus(c.id, int(cpus * 1e9))
        self.log(f"{prefix}cap {c.name} cpus={cpus}")

    def release_all(self, containers: dict[str, Container]) -> None:
        for cid in sorted(self.ledger.nano_cpus):  # a copy: the loop pops entries
            c = containers.get(cid)
            try:
                if c is not None:
                    self.set(c, None)
                elif self.apply:
                    self._write_back(cid, self.ledger.nano_cpus[cid])
            except DockerError as exc:
                self.log(f"exit: restore {cid[:12]} failed: {exc}")


class DutyCycler:
    """Pause every batch container for ``duty`` x ``period_s`` of each period.

    A frozen process issues no new GPU kernels (an LLM decode step is 10-30 ms),
    so the heat it makes falls roughly in proportion to the duty. ``duty`` >=
    0.98 holds the containers paused; <= 0.02 keeps them running. Runs in its
    own thread so the rhythm is not tied to the 1 Hz sensing loop.

    Two records of "ours": ``held`` is what this process has paused right now;
    the ledger lists every container it is cycling, written once when cycling
    starts and once when it ends (not on every pause), so a crash anywhere in a
    cycle leaves a ledger entry that the next start's recover can act on --
    recover unpauses only an entry still paused. All pause/unpause work holds
    one lock, and a pause never starts once stop() has begun."""

    def __init__(
        self,
        docker: DockerLike,
        ledger: Ledger,
        log: Log,
        *,
        apply: bool,
        period_s: float = 2.0,
        sleep: Callable[[float], None] | None = None,
        join_s: float = 30.0,
    ) -> None:
        self.docker = docker
        self.ledger = ledger
        self.log = log
        self.apply = apply
        self.period_s = period_s
        self.join_s = join_s  # longer than one docker call can block
        self.duty = 0.0
        self.targets: dict[str, Container] = {}
        self.held: set[str] = set()
        self.foreign: set[str] = set()
        self.cycles = 0
        self.paused_s = 0.0
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._lock = threading.Lock()  # duty / targets
        self._act = threading.RLock()  # every pause / unpause / ledger change
        self._thread: threading.Thread | None = None
        self._last_logged_duty = -1.0

    # -- control from the main loop
    def set(self, duty: float, targets: dict[str, Container]) -> None:
        with self._lock:
            self.duty = max(0.0, min(1.0, duty))
            self.targets = dict(targets)
        if abs(self.duty - self._last_logged_duty) >= 0.1 or (
            self.duty in (0.0, 1.0) and self.duty != self._last_logged_duty
        ):
            self._last_logged_duty = self.duty
            names = ",".join(c.name for c in targets.values()) or "-"
            prefix = "" if self.apply else "dry-run would "
            self.log(f"{prefix}duty={self.duty:.2f} on {names}")

    # -- primitives (own-pause rule)
    def _pause(self, c: Container) -> None:
        with self._act:
            if self._stop.is_set() or c.id in self.held:
                return
            if not self.apply:
                self.held.add(c.id)  # simulated
                return
            try:
                state = self.docker.inspect(c.id).state
            except DockerError:
                return
            if state == "paused":  # paused, and not by us (not in held)
                if c.id not in self.foreign:
                    self.foreign.add(c.id)
                    self.log(f"foreign pause on {c.name} (not ours): leaving it alone")
                return
            if state != "running":
                return
            if c.id in self.foreign:
                self.foreign.discard(c.id)
                self.log(f"{c.name} was unpaused by its owner: governing it again")
            if c.id not in self.ledger.paused:
                self.ledger.paused.add(c.id)
                self.ledger.save()  # record BEFORE acting
            try:
                self.docker.pause(c.id)
                self.held.add(c.id)
            except DockerError as exc:
                self.log(f"pause {c.name} failed: {exc}")

    def _unpause(self, cid: str) -> None:
        with self._act:
            if cid not in self.held:
                return
            if self.apply:
                try:
                    self.docker.unpause(cid)
                except DockerError as exc:
                    self.log(f"unpause {cid[:12]} failed: {exc}")
                    return
            self.held.discard(cid)

    def unpause_all(self) -> None:
        with self._act:
            for cid in sorted(self.held):  # a copy: _unpause discards from held
                self._unpause(cid)

    def release(self, cids) -> None:
        """Unpause and drop from the ledger: the container is no longer cycled."""
        with self._act:
            changed = False
            for cid in sorted(cids):
                self._unpause(cid)
                if cid in self.ledger.paused and cid not in self.held:
                    self.ledger.paused.discard(cid)
                    changed = True
            if changed and self.apply:
                self.ledger.save()

    def release_all(self) -> None:
        with self._act:
            self.release(set(self.held) | set(self.ledger.paused))

    # -- one period
    def cycle(self) -> None:
        with self._lock:
            duty = self.duty
            targets = dict(self.targets)
        # a container that left the target set (or stopped) is released at once
        gone = (set(self.held) | set(self.ledger.paused)) - set(targets)
        if gone:
            self.release(gone)
        self.foreign &= set(targets)
        if not targets or duty <= 0.02:
            self.release_all()
            self._sleep(self.period_s)
            return
        if duty >= 0.98:
            for c in targets.values():
                self._pause(c)
            self.paused_s += self.period_s
            self._sleep(self.period_s)
            return
        on = duty * self.period_s
        t0 = time.monotonic()
        for c in targets.values():
            self._pause(c)
        self._sleep(max(0.0, on - (time.monotonic() - t0)))
        self.unpause_all()
        self.paused_s += on
        self.cycles += 1
        self._sleep(max(0.0, self.period_s - on))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as exc:  # noqa: BLE001 - never die holding a pause
                self.log(f"duty cycle error: {exc}; releasing own pauses")
                self.unpause_all()
                self._stop.wait(self.period_s)
        self.unpause_all()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="duty-cycler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()  # no new pause can start after this
        if self._thread is not None:
            self._thread.join(timeout=self.join_s)
            if self._thread.is_alive():
                self.log("duty cycler did not stop in time; releasing under the lock")
        self.release_all()  # waits for any in-flight pause (same lock)

"""The GPU ceiling the daemon writes, under the operator's law.

Per tick the daemon hands this the policy's band ceiling (:attr:`Decision.gpu_mhz`).
What is written (:meth:`GpuGovernor.plan`):

* no operator ceiling -- the band, when ``daemon.gpu_clock`` is on; with it off
  nothing, except removing a ceiling the daemon still holds for a cleared
  operator ceiling;
* an operator ceiling (:mod:`heat_governor.operator_ceiling`) -- ``min(operator,
  band)`` with ``gpu_clock`` on, the operator's own ceiling with it off. A band
  above the operator ceiling (including "no ceiling") is *held*: logged once per
  change of the wanted value, and published (``gpu_held_by_operator``).

On exit and when ``gpu_clock`` is turned off the operator ceiling is restored,
never reset. Where the hardware shows the ceiling (a Jetson: the helper's
``status`` reads devfreq ``max_freq``) a read-back below what the daemon last
saw after its own write is someone else's hand ceiling: it becomes the
operator's (source ``observed``) until ``gpu-clock release``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from heat_governor.gpuclock import GpuClock
from heat_governor.operator_ceiling import OperatorCeiling

NONE = "none"
_UNSET = object()  # nothing logged yet


@dataclass(frozen=True)
class Plan:
    act: bool  # whether the daemon drives the GPU ceiling this tick
    target: int | None  # the ceiling to hold; None = no ceiling (reset)
    wanted: int | None  # the policy's band (gpu_clock on); None = none / not driving
    held: bool  # the policy wanted more than the operator ceiling allows


def _shown(mhz: int | None) -> str:
    return NONE if mhz is None else str(mhz)


class GpuGovernor:
    RETRY_S = 30.0  # after a failed helper call, wait before trying again
    OBSERVE_S = 5.0  # how often the hardware ceiling is read back

    def __init__(
        self,
        clock: GpuClock | None,
        ledger,
        operator: OperatorCeiling,
        log: Callable[[str], None],
        *,
        apply: bool,
        enabled: bool,
    ) -> None:
        self.clock = clock
        self.ledger = ledger
        self.operator = operator
        self.log = log
        self.apply = apply
        self.enabled = enabled
        self.probed = False
        self.retry_at = 0.0
        self.observe_at = 0.0
        self.baseline: int | None = None  # hardware read-back after our last write
        self._would: object = _UNSET  # dry-run: last target logged
        self._held: object = _UNSET  # last (wanted, operator) logged as held

    # -- the helper
    def needed(self) -> bool:
        return self.enabled or self.operator.mhz is not None

    def probe(self) -> str:
        if self.clock is None:
            self.clock = GpuClock()
        self.clock.probe()
        self.probed = True
        self.baseline = self._read()
        return self.clock.reason

    def ensure(self) -> GpuClock | None:
        """The helper, probed once, when it is needed and available."""
        if not (self.probed or self.needed()):
            return None
        if not self.probed:
            self.probe()
        return self.clock if self.clock.available else None

    def _read(self) -> int | None:
        if self.clock is None or not self.clock.available:
            return None
        return self.clock.read_ceiling()

    def _holding(self) -> bool:
        return bool(self.clock and (self.clock.ceiling_mhz is not None or self.ledger.gpu_locked))

    # -- the law
    def plan(self, band: int | None) -> Plan:
        op = self.operator.mhz
        wanted = band if self.enabled else None
        if op is None:
            return Plan(self.enabled or self._holding(), wanted, wanted, False)
        if self.enabled and band is not None and band <= op:
            return Plan(True, band, band, False)
        return Plan(True, op, wanted, self.enabled)

    def note_held(self, p: Plan, level: str) -> None:
        if not p.held:
            self._held = _UNSET
            return
        key = (p.wanted, p.target)
        if key == self._held:
            return
        self._held = key
        self.log(
            f"gpu: would raise the ceiling to {_shown(p.wanted)} (policy: level {level}) "
            f"-- operator ceiling {p.target} MHz holds"
        )

    def step(self, band: int | None, level: str, now: float) -> Plan:
        """Observe an outside ceiling, then drive the GPU to the plan."""
        self.observe(now)
        p = self.plan(band)
        self.note_held(p, level)
        if p.act:
            self.drive(p.target, now)
        return p

    # -- an outside (hand) ceiling
    def observe(self, now: float) -> None:
        if not self.needed() or now < self.observe_at or self.ensure() is None:
            return
        if self.baseline is None:
            return
        self.observe_at = now + self.OBSERVE_S
        hw = self._read()
        if hw is None or hw >= self.baseline:
            return
        self.baseline = hw
        op = self.operator.mhz
        if op is not None and hw >= op:
            return  # tighter than ours, but not than the law already in force
        self.operator.observe(hw, now)
        self.log(
            f"gpu: observed an outside ceiling of {hw} MHz -- treating it as the operator's (law)"
        )

    def operator_changed(self) -> None:
        """The operator ceiling changed: what the hardware shows now is the baseline,
        so a ceiling just released is not observed again."""
        if self.probed:
            self.baseline = self._read()

    # -- acting
    def drive(self, target: int | None, now: float) -> None:
        g = self.ensure()
        if g is None or target == g.ceiling_mhz or now < self.retry_at:
            return
        if not self.apply:
            if target != self._would:
                self._would = target
                self.log(f"dry-run would set gpu ceiling {target or NONE}")
            return
        if target is None:
            self._reset(g, now)
        else:
            self._lock(g, target, now)

    def _reset(self, g: GpuClock, now: float) -> None:
        ok, out = g.reset()
        if ok:
            self.ledger.gpu_locked = False
            self.ledger.save()
            self.baseline = self._read()
        else:
            self.retry_at = now + self.RETRY_S
        self.log("gpu ceiling removed" if ok else f"gpu reset FAILED: {out}")

    def _lock(self, g: GpuClock, want: int, now: float) -> None:
        self.ledger.gpu_locked = True
        self.ledger.save()  # record BEFORE acting
        ok, out = g.lock(want)
        if ok:
            self.baseline = self._read()
        else:
            self.retry_at = now + self.RETRY_S
        mine = " (the operator's ceiling)" if want == self.operator.mhz else ""
        self.log(f"gpu ceiling {want} MHz{mine}" if ok else f"gpu lock {want} FAILED: {out}")

    def release(self, why: str) -> None:
        """Exit, or ``gpu_clock`` turned off: restore the operator ceiling when one
        is set (never reset), else remove the ceiling the daemon holds."""
        g = self.clock
        if g is None or not self.apply:
            return
        op = self.operator.mhz
        if op is not None:
            self._restore_operator(g, op, why)
            return
        if g.ceiling_mhz is not None or self.ledger.gpu_locked:
            ok, out = g.reset()
            if ok:
                self.ledger.gpu_locked = False
                self.ledger.save()
            self.log(f"{why}: gpu ceiling removed" if ok else f"{why}: gpu reset FAILED: {out}")

    def _restore_operator(self, g: GpuClock, op: int, why: str) -> None:
        if g.ceiling_mhz == op or not g.available:
            return
        self.ledger.gpu_locked = True
        self.ledger.save()
        ok, out = g.lock(op)
        self.log(
            f"{why}: gpu ceiling restored to the operator's {op} MHz"
            if ok
            else f"{why}: gpu lock {op} FAILED: {out}"
        )

    # -- published
    def doc(self, p: Plan) -> dict:
        return {
            "gpu_operator_mhz": self.operator.mhz,
            "gpu_operator_source": self.operator.source,
            "gpu_wanted_mhz": p.wanted,
            "gpu_held_by_operator": p.held,
        }

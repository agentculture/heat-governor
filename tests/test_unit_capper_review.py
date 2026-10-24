"""User-unit actuator review fixes (PR #5): a unit turned dry while capped,
backoff on any systemctl error, orphaned originals of a unit that vanished,
and a dry-run trace that shows the would-be restore. FakeSystemctl only."""

from __future__ import annotations

from pathlib import Path

from heat_governor.actuators import Ledger
from heat_governor.units import UnitCapper, UnitError
from tests.fakes import FakeSystemctl
from tests.test_units import _capper

APP = "app.slice"
GONE = "x.scope"


def _sets(sc: FakeSystemctl) -> list[tuple]:
    return [c for c in sc.calls if c[0] == "set"]


# -- 1: dry_run turned on while a real cap is held -------------------------------


def test_a_unit_turned_dry_while_capped_gets_its_original_back(tmp_path: Path) -> None:
    cap, sc, led, logs, _ = _capper(tmp_path, {APP: 6_000_000})
    cap.set(APP, 4.0)
    cap.set_dry({APP})  # a reload sets dry_run = true
    cap.set(APP, 4.0)  # the same level on the next tick
    assert sc.units[APP] == 6_000_000
    assert led.unit_quota == {}
    assert logs[-1] == "dry-run would cap unit app.slice CPUQuota=400% (cpus=4)"


def test_a_unit_turned_dry_then_cool_is_restored_for_real(tmp_path: Path) -> None:
    cap, sc, led, _, _ = _capper(tmp_path, {APP: None})
    cap.set(APP, 4.0)
    cap.set_dry({APP})
    cap.set(APP, None)
    assert sc.units[APP] is None
    assert Ledger(tmp_path / "l.json").unit_quota == {}


def test_release_restores_a_ledger_entry_for_real_even_when_dry(tmp_path: Path) -> None:
    cap, sc, led, _, _ = _capper(tmp_path, {APP: None})
    cap.set(APP, 4.0)
    cap.dry = {APP}  # dry without a tick in between
    cap.release_all()
    assert sc.units[APP] is None
    assert led.unit_quota == {}


# -- 2: backoff on a plain UnitError ------------------------------------------------


class NoBus(FakeSystemctl):
    message = "Failed to connect to bus"

    def show(self, unit: str):
        self.calls.append(("show", unit))
        if self.message:
            raise UnitError(self.message)
        return super().show(unit)


def _nobus(tmp_path: Path):
    led = Ledger(tmp_path / "l.json")
    sc = NoBus({APP: None})
    logs: list[str] = []
    clock = [100.0]
    cap = UnitCapper(sc, led, logs.append, apply=True, clock=lambda: clock[0])
    return cap, sc, logs, clock


def test_a_systemctl_error_is_logged_once_and_retried_every_retry_s(tmp_path: Path) -> None:
    cap, sc, logs, clock = _nobus(tmp_path)
    for _ in range(5):
        cap.set(APP, 4.0)
        clock[0] += 1.0
    assert len(sc.calls) == 1
    assert len(logs) == 1
    assert "Failed to connect to bus" in logs[0]
    clock[0] += UnitCapper.RETRY_S
    cap.set(APP, 4.0)
    assert len(sc.calls) == 2
    assert len(logs) == 1  # the same error is not logged again


def test_a_different_error_is_logged_and_recovery_governs_again(tmp_path: Path) -> None:
    cap, sc, logs, clock = _nobus(tmp_path)
    cap.set(APP, 4.0)
    sc.message = "Access denied"
    clock[0] += UnitCapper.RETRY_S
    cap.set(APP, 4.0)
    assert len(logs) == 2
    assert "Access denied" in logs[1]
    sc.message = ""
    clock[0] += UnitCapper.RETRY_S
    cap.set(APP, 4.0)
    assert sc.units[APP] == 4_000_000


# -- 4: an orphaned original survives the unit going missing -------------------------


def _cap_then_lose(tmp_path: Path):
    cap, sc, led, logs, clock = _capper(tmp_path, {GONE: None})
    cap.set(GONE, 4.0)
    del sc.units[GONE]
    cap.set(GONE, None)  # gone at restore time
    return cap, sc, led, logs, clock


def test_a_unit_gone_at_restore_keeps_its_original_as_an_orphan(tmp_path: Path) -> None:
    _, _, led, _, _ = _cap_then_lose(tmp_path)
    assert led.unit_quota == {}
    assert Ledger(tmp_path / "l.json").unit_orphans == {GONE: None}


def test_an_orphan_seen_again_is_restored_first(tmp_path: Path) -> None:
    cap, sc, led, _, clock = _cap_then_lose(tmp_path)
    sc.units[GONE] = 4_000_000  # it loads again with our --runtime drop-in in force
    clock[0] += UnitCapper.RETRY_S
    cap.set(GONE, None)
    assert sc.units[GONE] is None
    assert led.unit_orphans == {}


def test_an_orphan_seen_again_under_a_cap_records_the_true_original(tmp_path: Path) -> None:
    cap, sc, led, _, clock = _cap_then_lose(tmp_path)
    sc.units[GONE] = 4_000_000
    clock[0] += UnitCapper.RETRY_S
    cap.set(GONE, 2.0)
    assert led.unit_quota == {GONE: None}  # not our own 4-CPU drop-in
    assert sc.units[GONE] == 2_000_000


def test_recover_keeps_a_missing_units_original_and_restores_it_later(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.json")
    led.unit_quota = {GONE: 3_000_000}
    led.save()
    Ledger(tmp_path / "l.json").recover(None, [].append, units=FakeSystemctl({}))
    assert Ledger(tmp_path / "l.json").unit_orphans == {GONE: 3_000_000}
    sc = FakeSystemctl({GONE: 1_000_000})
    Ledger(tmp_path / "l.json").recover(None, [].append, units=sc)
    assert sc.units[GONE] == 3_000_000
    assert Ledger(tmp_path / "l.json").unit_orphans == {}


# -- 7: a dry-run trace shows the would-be restore ----------------------------------


def test_dry_run_logs_the_would_be_restore(tmp_path: Path) -> None:
    cap, sc, led, logs, _ = _capper(tmp_path, {APP: 6_000_000}, apply=False)
    cap.set(APP, 4.0)
    assert cap.current[APP] == 4.0  # the state shows the would-be cap
    cap.set(APP, None)
    assert logs[-1] == "dry-run would restore unit app.slice CPUQuota=600%"
    assert cap.current[APP] is None
    assert _sets(sc) == []
    assert not (tmp_path / "l.json").exists()  # no ledger writes in dry-run


def test_dry_run_restore_reads_the_original_fresh(tmp_path: Path) -> None:
    cap, sc, _, logs, _ = _capper(tmp_path, {APP: 6_000_000})
    cap.set_dry({APP})
    cap.set(APP, 4.0)
    sc.units[APP] = 8_000_000  # the operator changed it meanwhile
    cap.set(APP, None)
    assert logs[-1] == "dry-run would restore unit app.slice CPUQuota=800%"
    cap.set(APP, None)
    assert len([m for m in logs if "would restore" in m]) == 1

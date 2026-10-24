"""CPU quota on systemd user units: parsing, the pure band rule, config, the
actuator under the ledger-first rule, and the daemon wiring. Every test uses
FakeSystemctl or an injected runner: none sets a property on a real unit."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from heat_governor.actuators import Ledger
from heat_governor.config import ConfigError, load, template, validate
from heat_governor.daemon import Daemon, Options
from heat_governor.platforms import SPARK, THOR
from heat_governor.policy import unit_cpus
from heat_governor.sense import Sensor
from heat_governor.state import read_state
from heat_governor.units import (
    Systemctl,
    UnitCapper,
    UnitError,
    UnitMissing,
    cpus_percent,
    parse_quota,
    percent,
)
from tests.fakes import SPARK_ZONES, FakeSystemctl, make_sys, set_temp

SPARK_BANDS = (("warm", 8.0), ("hot", 4.0), ("critical", 2.0))

# -- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,usec",
    [
        ("infinity", None),
        ("", None),
        ("1.500000s", 1_500_000),
        ("23.500000s", 23_500_000),
        ("1min 10s", 70_000_000),
        ("800ms", 800_000),
        ("4s", 4_000_000),
    ],
)
def test_parse_quota_reads_systemd_timespans(text: str, usec) -> None:
    assert parse_quota(text) == usec


def test_parse_quota_rejects_garbage() -> None:
    with pytest.raises(UnitError):
        parse_quota("lots")


def test_percent_formatting() -> None:
    assert percent(1_500_000) == "150"
    assert percent(1_234_500) == "123.45"
    assert cpus_percent(4) == "400"
    assert cpus_percent(1.5) == "150"


# -- the pure rule -------------------------------------------------------------


def test_unit_cpus_by_level() -> None:
    assert unit_cpus("cool", SPARK_BANDS) is None
    assert unit_cpus("warm", SPARK_BANDS) == 8.0
    assert unit_cpus("hot", SPARK_BANDS) == 4.0
    assert unit_cpus("critical", SPARK_BANDS) == 2.0
    only_hot = (("hot", 4.0),)
    assert unit_cpus("warm", only_hot) is None
    assert unit_cpus("critical", only_hot) == 4.0  # the nearest level below
    assert unit_cpus("critical", ()) is None


# -- config --------------------------------------------------------------------


def test_units_config_is_validated_strictly() -> None:
    ok = {"units": [{"name": "app.slice", "warm": 8, "hot": 4, "critical": 2}]}
    assert validate(ok) == []
    assert validate({"units": [{"name": "app.slice"}]}) == []  # platform default bands
    assert validate({"units": [{"name": "app.slice", "dry_run": True}]}) == []
    bad = [
        ({"units": [{"name": "app.slice", "hot_cpus": 4}]}, "unknown key"),
        ({"units": [{"name": "-.slice"}]}, "name"),
        ({"units": [{"name": "app"}]}, "name"),
        ({"units": [{"name": "a.slice --now"}]}, "name"),
        ({"units": [{"name": "app.slice"}, {"name": "app.slice"}]}, "twice"),
        ({"units": [{"name": "app.slice", "hot": 0}]}, "> 0"),
        ({"units": [{"name": "app.slice", "hot": True}]}, "number"),
        ({"units": [{"name": "app.slice", "warm": 2, "hot": 4}]}, "must not rise"),
        ({"units": {"name": "app.slice"}}, "array of tables"),
        ({"units": [{"name": "app.slice", "dry_run": "yes"}]}, "dry_run"),
    ]
    for doc, needle in bad:
        problems = validate(doc)
        assert any(needle in p for p in problems), (doc, problems)


def test_units_config_loads_rules(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(
        '[[units]]\nname = "app.slice"\nhot = 4\ncritical = 2\n\n[[units]]\nname = "x.scope"\n'
    )
    cfg = load(p)
    assert [(u.name, u.bands) for u in cfg.units] == [
        ("app.slice", (("hot", 4.0), ("critical", 2.0))),
        ("x.scope", ()),
    ]
    p.write_text('[[units]]\nname = "app.slice"\nhot = -1\n')
    with pytest.raises(ConfigError):
        load(p)


def test_template_documents_units_and_stays_valid() -> None:
    text = template(SPARK)
    assert "[[units]]" in text
    import tomllib

    assert validate(tomllib.loads(text)) == []  # the example is commented out


def test_only_spark_declares_default_unit_bands() -> None:
    assert SPARK.unit_bands == SPARK_BANDS
    assert THOR.unit_bands == ()


# -- the actuator -----------------------------------------------------------------


def _capper(tmp_path: Path, units: dict, *, apply=True, **kw):
    led = Ledger(tmp_path / "l.json")
    sc = FakeSystemctl(units, ledger=tmp_path / "l.json", **kw)
    logs: list[str] = []
    clock = [100.0]
    cap = UnitCapper(sc, led, logs.append, apply=apply, clock=lambda: clock[0])
    return cap, sc, led, logs, clock


def test_cap_records_the_original_before_acting_and_restores_it(tmp_path: Path) -> None:
    cap, sc, led, logs, _ = _capper(tmp_path, {"app.slice": None})
    cap.set("app.slice", 4.0)
    assert sc.units["app.slice"] == 4_000_000
    assert sc.on_disk[0]["unit_quota"] == {"app.slice": None}  # written BEFORE the change
    cap.set("app.slice", 2.0)
    assert sc.units["app.slice"] == 2_000_000
    assert led.unit_quota == {"app.slice": None}  # the original, not our 4
    cap.set("app.slice", None)
    assert sc.calls[-1] == ("set", "app.slice", None)  # CPUQuota= (none)
    assert sc.units["app.slice"] is None
    assert Ledger(tmp_path / "l.json").unit_quota == {}
    assert logs == [
        "cap unit app.slice CPUQuota=400% (cpus=4)",
        "cap unit app.slice CPUQuota=200% (cpus=2)",
        "restore unit app.slice CPUQuota=none",
    ]


def test_original_is_read_fresh_after_each_restore(tmp_path: Path) -> None:
    cap, sc, led, _, _ = _capper(tmp_path, {"app.slice": 6_000_000})
    cap.set("app.slice", 4.0)
    cap.set("app.slice", None)
    assert sc.units["app.slice"] == 6_000_000
    cap.set("app.slice", 4.0)
    assert led.unit_quota == {"app.slice": 6_000_000}
    assert [c for c in sc.calls if c[0] == "show"] == [("show", "app.slice")] * 2


def test_a_band_never_raises_a_tighter_original_quota(tmp_path: Path) -> None:
    cap, sc, _, _, _ = _capper(tmp_path, {"app.slice": 2_000_000})
    cap.set("app.slice", 8.0)
    assert sc.units["app.slice"] == 2_000_000


def test_cool_with_nothing_on_record_calls_nothing(tmp_path: Path) -> None:
    cap, sc, _, _, _ = _capper(tmp_path, {"app.slice": None})
    cap.set("app.slice", None)
    cap.set("app.slice", None)
    assert sc.calls == []


def test_dry_run_reads_but_changes_nothing(tmp_path: Path) -> None:
    cap, sc, led, logs, _ = _capper(tmp_path, {"app.slice": None}, apply=False)
    cap.set("app.slice", 4.0)
    cap.set("app.slice", None)
    assert [c for c in sc.calls if c[0] == "set"] == []
    assert led.unit_quota == {}
    assert logs == ["dry-run would cap unit app.slice CPUQuota=400% (cpus=4)"]


def test_a_dry_run_unit_changes_nothing_even_with_apply(tmp_path: Path) -> None:
    cap, sc, led, logs, _ = _capper(tmp_path, {"app.slice": None, "x.scope": None})
    cap.set_dry({"app.slice"})
    cap.set("app.slice", 4.0)
    cap.set("x.scope", 4.0)
    assert [c for c in sc.calls if c[0] == "set"] == [("set", "x.scope", "400")]
    assert led.unit_quota == {"x.scope": None}
    assert logs[0] == "dry-run would cap unit app.slice CPUQuota=400% (cpus=4)"


def test_missing_unit_is_reported_once_and_skipped(tmp_path: Path) -> None:
    cap, sc, led, logs, clock = _capper(tmp_path, {}, inactive=())
    cap.set("app.slice", 4.0)
    cap.set("app.slice", 2.0)
    clock[0] += 5
    cap.set("app.slice", 4.0)
    assert len([m for m in logs if "app.slice" in m]) == 1
    assert "not found" in logs[0]
    assert cap.missing_units() == ["app.slice"]
    assert len(sc.calls) == 1  # rechecked only every RETRY_S
    assert led.unit_quota == {}
    sc.units["app.slice"] = None
    clock[0] += UnitCapper.RETRY_S
    cap.set("app.slice", 4.0)
    assert sc.units["app.slice"] == 4_000_000
    assert cap.missing_units() == []
    assert any("app.slice is back" in m for m in logs)


def test_inactive_unit_counts_as_missing(tmp_path: Path) -> None:
    cap, sc, led, logs, _ = _capper(tmp_path, {"app.slice": None}, inactive=("app.slice",))
    cap.set("app.slice", 4.0)
    assert [c for c in sc.calls if c[0] == "set"] == []
    assert "inactive" in logs[0]


def test_a_unit_gone_before_restore_drops_its_entry(tmp_path: Path) -> None:
    cap, sc, led, logs, _ = _capper(tmp_path, {"x.scope": None})
    cap.set("x.scope", 4.0)
    del sc.units["x.scope"]
    cap.set("x.scope", None)
    assert led.unit_quota == {}
    assert "gone" in logs[-1]


def test_recover_restores_what_a_crashed_run_left(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.json")
    led.unit_quota = {"app.slice": None, "y.slice": 3_000_000, "gone.scope": None}
    led.save()
    sc = FakeSystemctl({"app.slice": 2_000_000, "y.slice": 1_000_000})
    logs: list[str] = []
    Ledger(tmp_path / "l.json").recover(None, logs.append, units=sc)
    assert sc.units == {"app.slice": None, "y.slice": 3_000_000}
    assert Ledger(tmp_path / "l.json").unit_quota == {}


def test_recover_keeps_a_failed_restore_on_record(tmp_path: Path) -> None:
    class Broken(FakeSystemctl):
        def set_quota(self, unit, percent):
            raise UnitError("Failed to connect to bus")

    led = Ledger(tmp_path / "l.json")
    led.unit_quota = {"app.slice": None}
    led.save()
    Ledger(tmp_path / "l.json").recover(None, [].append, units=Broken({"app.slice": 1}))
    assert Ledger(tmp_path / "l.json").unit_quota == {"app.slice": None}


def test_release_all_restores_every_unit(tmp_path: Path) -> None:
    cap, sc, led, _, _ = _capper(tmp_path, {"a.slice": None, "b.slice": 9_000_000})
    cap.set("a.slice", 2.0)
    cap.set("b.slice", 2.0)
    cap.release_all()
    assert sc.units == {"a.slice": None, "b.slice": 9_000_000}
    assert led.unit_quota == {}


# -- the real systemctl wrapper, with an injected runner ----------------------------


def test_systemctl_argv_always_user_and_runtime() -> None:
    calls: list[list[str]] = []

    def run(argv):
        calls.append(argv)
        if argv[2] == "show":
            return 0, "LoadState=loaded\nActiveState=active\nCPUQuotaPerSecUSec=1.500000s\n", ""
        return 0, "", ""

    sc = Systemctl(run=run)
    info = sc.show("app.slice")
    assert (info.loaded, info.active, info.quota_usec) == (True, True, 1_500_000)
    sc.set_quota("app.slice", "400")
    sc.set_quota("app.slice", None)
    assert calls[1] == [
        "systemctl",
        "--user",
        "set-property",
        "--runtime",
        "--",
        "app.slice",
        "CPUQuota=400%",
    ]
    assert calls[2][-1] == "CPUQuota="
    assert calls[0][:3] == ["systemctl", "--user", "show"]
    assert calls[0][-2:] == ["--", "app.slice"]


def test_systemctl_errors() -> None:
    sc = Systemctl(run=lambda argv: (1, "", "Unit x.scope not found."))
    with pytest.raises(UnitMissing):
        sc.set_quota("x.scope", "400")
    sc = Systemctl(run=lambda argv: (1, "", "Failed to connect to bus"))
    with pytest.raises(UnitError):
        sc.show("app.slice")
    sc = Systemctl(run=lambda argv: (0, "LoadState=not-found\nActiveState=inactive\n", ""))
    assert sc.show("x.service").loaded is False


# -- the daemon ------------------------------------------------------------------


def _daemon(tmp_path: Path, sc: FakeSystemctl, *, units=(), apply=True, config=None):
    from heat_governor.config import UnitRule

    sysr = make_sys(tmp_path, SPARK_ZONES)
    opts = Options(
        apply=apply,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        log=[].append,
        units=tuple(UnitRule(n, b) for n, b in units),
        config=config,
    )
    t = [1000.0]

    def clock() -> float:  # 10 s per tick: a step in temperature is no steep rise
        t[0] += 10.0
        return t[0]

    d = Daemon(SPARK, opts, sensor=Sensor(SPARK.zones, sys_root=sysr), systemctl=sc, clock=clock)
    return d, sysr


def test_daemon_caps_units_by_level_reports_and_restores(tmp_path: Path) -> None:
    sc = FakeSystemctl({"app.slice": None})
    d, sysr = _daemon(tmp_path, sc, units=[("app.slice", ())])
    d.start()
    d.tick()
    assert [c for c in sc.calls if c[0] == "set"] == []  # cool
    set_temp(sysr, 0, 92000)  # hot on the Spark's platform bands
    d.tick()
    assert sc.units["app.slice"] == 4_000_000
    d.tick()
    st = read_state(tmp_path / "state.json")
    assert st["units"] == [
        {
            "name": "app.slice",
            "mode": "levels",
            "dry_run": False,
            "cpus": 4.0,
            "use_cpus": None,
            "missing": False,
        }
    ]
    d.shutdown()
    assert sc.units["app.slice"] is None
    assert Ledger(tmp_path / "data" / "ledger.json").unit_quota == {}


def test_own_bands_replace_the_platform_default(tmp_path: Path) -> None:
    sc = FakeSystemctl({"app.slice": None})
    d, sysr = _daemon(tmp_path, sc, units=[("app.slice", (("hot", 6.0),))])
    set_temp(sysr, 0, 92000)
    d.tick()
    assert sc.units["app.slice"] == 6_000_000


def test_daemon_without_units_never_calls_systemctl(tmp_path: Path) -> None:
    sc = FakeSystemctl({"app.slice": None})
    d, sysr = _daemon(tmp_path, sc)
    d.start()
    set_temp(sysr, 0, 99000)
    d.tick()
    d.shutdown()
    assert sc.calls == []
    assert read_state(tmp_path / "state.json")["units"] == []


def test_daemon_dry_run_leaves_units_alone(tmp_path: Path) -> None:
    sc = FakeSystemctl({"app.slice": None})
    d, sysr = _daemon(tmp_path, sc, units=[("app.slice", ())], apply=False)
    set_temp(sysr, 0, 99000)
    d.tick()
    d.shutdown()
    assert [c for c in sc.calls if c[0] == "set"] == []


def test_daemon_honours_a_units_own_dry_run(tmp_path: Path) -> None:
    from heat_governor.config import UnitRule

    sc = FakeSystemctl({"app.slice": None})
    d, sysr = _daemon(tmp_path, sc)
    d._set_units((UnitRule("app.slice", (), dry_run=True),))
    set_temp(sysr, 0, 99000)
    d.tick()
    assert [c for c in sc.calls if c[0] == "set"] == []
    assert read_state(tmp_path / "state.json")["units"][0]["dry_run"] is True
    d._set_units((UnitRule("app.slice", (), dry_run=False),))  # a reload turns it on
    d.tick()
    assert sc.units["app.slice"] == 2_000_000
    d.shutdown()
    assert sc.units["app.slice"] is None


def test_daemon_start_recovers_unit_quotas(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "data" / "ledger.json")
    led.unit_quota = {"app.slice": None}
    led.save()
    sc = FakeSystemctl({"app.slice": 2_000_000})
    d, _ = _daemon(tmp_path, sc)  # no unit configured any more: still recovered
    d.start()
    assert sc.units["app.slice"] is None


def test_reload_that_drops_a_unit_restores_it(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text('[[units]]\nname = "app.slice"\n')
    cfg = load(path)
    sc = FakeSystemctl({"app.slice": None})
    d, sysr = _daemon(tmp_path, sc, units=[("app.slice", ())], config=cfg)
    set_temp(sysr, 0, 92000)
    d.tick()
    assert sc.units["app.slice"] == 4_000_000
    path.write_text("")
    os.utime(path, (cfg.mtime + 5, cfg.mtime + 5))
    d.maybe_reload(d.clock())
    assert d.opts.units == ()
    assert sc.units["app.slice"] is None


def test_containing_names_the_units_this_process_runs_in(tmp_path: Path) -> None:
    from heat_governor.units import containing

    cg = tmp_path / "cgroup"
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/hg.service\n")
    assert containing(["app.slice", "background.slice"], cg) == ["app.slice"]
    assert containing(["app.slice"], tmp_path / "missing") == []


def test_daemon_notes_when_it_runs_inside_a_capped_unit(tmp_path: Path) -> None:
    sc = FakeSystemctl({"app.slice": None})
    d, _ = _daemon(tmp_path, sc, units=[("app.slice", ())])
    logs: list[str] = []
    d.opts.log = logs.append
    cg = tmp_path / "cgroup"
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/hg.service\n")
    d._note_self_inside(cg)
    assert any("runs inside app.slice" in m for m in logs)


def test_unbanded_unit_is_reported_once_and_never_capped(tmp_path: Path) -> None:
    from heat_governor.config import UnitRule
    from heat_governor.platforms import THOR

    sc = FakeSystemctl({"app.slice": None})
    d, sysr = _daemon(tmp_path, sc, units=[("app.slice", ())])
    d.profile = THOR
    logs: list[str] = []
    d.opts.log = logs.append
    assert d.unit_bands(UnitRule("app.slice")) == ()
    assert d.unit_bands(UnitRule("app.slice")) == ()
    assert len([m for m in logs if "declares none" in m]) == 1


def test_run_once_lists_units_and_changes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    from heat_governor.cli import main

    sc = FakeSystemctl({"app.slice": None})
    monkeypatch.setattr("heat_governor.daemon.Systemctl", lambda: sc)
    cfg = tmp_path / "c.toml"
    cfg.write_text('platform = "spark"\n[[units]]\nname = "app.slice"\nhot = 4\n')
    rc = main(
        [
            "run",
            "--once",
            "--no-docker",
            "--config",
            str(cfg),
            "--data-dir",
            str(tmp_path / "d"),
            "--state",
            str(tmp_path / "s.json"),
            "--json",
        ]
    )
    assert rc in (0, None)
    doc = json.loads(capsys.readouterr().out)
    assert doc["units"] == [
        {"name": "app.slice", "mode": "levels", "cpus": unit_cpus(doc["level"], (("hot", 4.0),))}
    ]
    assert [c for c in sc.calls if c[0] == "set"] == []

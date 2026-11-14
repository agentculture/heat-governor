"""Absolute, per-platform memory limits (deviation d3).

Boxes that run full of AI work (thor, the sparks, orin) sit at 8-13% free memory all day, so
percentage limits would fire or scan constantly there. [memory] floor_mib/soft_mib and the
platform profiles' declared limits replace the percentages; precedence is config > platform
> percent."""

import json
import time

import pytest

from heat_governor import config as cfgmod
from heat_governor.cli import main
from heat_governor.config import MemoryRule
from heat_governor.memguard import MemGuard, effective_limits
from heat_governor.memkill import MemKiller
from heat_governor.memtrigger import Trigger
from heat_governor.platforms import BUILTIN, generic_profile
from tests.fakes_proc import add_proc

THOR_TOTAL = 125772


# --- the trigger -----------------------------------------------------------------------------


def test_trigger_uses_absolute_limits_over_percentages():
    t = Trigger(THOR_TOTAL, floor_pct=5, soft_pct=20, floor_mib=3072, soft_mib=6144)
    assert (t.floor_mib, t.soft_mib) == (3072.0, 6144.0)


def test_trigger_percent_defaults_unchanged():
    t = Trigger(1000)
    assert (t.floor_mib, t.soft_mib) == (50.0, 200.0)


def test_thor_normal_load_never_fires_with_platform_limits():
    t = Trigger(THOR_TOTAL, floor_mib=3072, soft_mib=6144)
    fires = [t.step(s, 10464 - (s % 7) * 50) for s in range(600)]  # ~10.4 GB free, jittering
    assert not any(fires)


def test_thor_runaway_still_fires_before_the_floor():
    t = Trigger(THOR_TOTAL, floor_mib=3072, soft_mib=6144)
    fire = None
    avail = 10464.0
    for s in range(60):
        fire = fire or t.step(float(s), avail)
        avail -= 1144  # ~1.2 GB/s
    assert fire is not None and fire.avail_mib >= 2048


# --- config ----------------------------------------------------------------------------------


def _problems(mem: dict) -> list[str]:
    return cfgmod.validate({"memory": mem})


def test_config_accepts_floor_and_soft_mib_together():
    assert _problems({"floor_mib": 3072, "soft_mib": 6144}) == []
    rule = cfgmod.Config(data={"memory": {"floor_mib": 3072, "soft_mib": 6144}}).memory
    assert (rule.floor_mib, rule.soft_mib) == (3072, 6144)


def test_config_defaults_leave_absolute_limits_unset():
    assert (MemoryRule().floor_mib, MemoryRule().soft_mib) == (None, None)


@pytest.mark.parametrize(
    "mem, needle",
    [
        ({"floor_mib": 3072}, "set together"),
        ({"soft_mib": 6144}, "set together"),
        ({"floor_mib": 100, "soft_mib": 6144}, "floor_mib must be at least 256"),
        ({"floor_mib": 4096, "soft_mib": 4096}, "soft_mib must be greater"),
        ({"floor_mib": 3072.5, "soft_mib": 6144}, "wrong type"),
    ],
)
def test_config_rejects_bad_absolute_limits(mem, needle):
    assert any(needle in p for p in _problems(mem)), _problems(mem)


# --- platform defaults -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, limits",
    [("spark", (3072, 6144)), ("thor", (3072, 6144)), ("orin", (2048, 4096))],
)
def test_platform_profiles_declare_memory_limits(name, limits):
    assert BUILTIN[name].memory_limits_mib == limits


def test_ventunoq_declares_small_box_limits():
    assert BUILTIN["ventunoq"].memory_limits_mib == (1024, 2048)


def test_generic_profile_keeps_percentages(tmp_path):
    assert generic_profile(tmp_path).memory_limits_mib is None


# --- precedence ------------------------------------------------------------------------------


def test_precedence_config_over_platform_over_percent():
    rule = MemoryRule(floor_mib=1000, soft_mib=2000)
    assert effective_limits(rule, (3072, 6144), 125000) == (1000.0, 2000.0, "config")
    assert effective_limits(MemoryRule(), (3072, 6144), 125000) == (3072.0, 6144.0, "platform")
    assert effective_limits(MemoryRule(), None, 1000) == (50.0, 200.0, "percent")


# --- the guard -------------------------------------------------------------------------------


def _meminfo(root, total, avail):
    (root / "meminfo").write_text(
        f"MemTotal: {total * 1024} kB\nMemFree: 1 kB\nMemAvailable: {avail * 1024} kB\n"
    )


def _guard(root, rule, platform_limits, events):
    killer = MemKiller(rule, proc_root=root, uid=1000, self_pid=1)
    return MemGuard(
        rule,
        proc_root=root,
        sys_root=root / "sys",
        clock=lambda: 1.0,
        monotonic=lambda: 1.0,
        killer=killer,
        emit=events.append,
        uid=1000,
        self_pid=1,
        platform_limits=platform_limits,
    )


def test_guard_reports_effective_limits_and_source(tmp_path):
    _meminfo(tmp_path, THOR_TOTAL, 10464)
    g = _guard(tmp_path, MemoryRule(action="kill"), (3072, 6144), [])
    g.step()
    assert g.doc()["limits"] == {"floor_mib": 3072, "soft_mib": 6144, "source": "platform"}


def test_guard_on_thor_load_is_calm_with_platform_limits(tmp_path):
    """10.4 GB free on 126 GB is below 20% but above the 6 GB soft limit: no low-memory scans."""
    _meminfo(tmp_path, THOR_TOTAL, 10464)
    add_proc(tmp_path, 1, ppid=0, uid=0, comm="systemd", cmdline=["systemd"], anon_kib=1024)
    events: list[dict] = []
    g = _guard(tmp_path, MemoryRule(action="kill", top_every_s=3600), (3072, 6144), events)
    for _ in range(5):
        g.step()
    tops = [e for e in events if e["event"] == "memory_top"]
    assert len(tops) == 1  # the routine snapshot only, not one per poll
    assert not [
        e for e in events if e["event"].startswith("memory_") and e["event"] != "memory_top"
    ]


# --- CLI -------------------------------------------------------------------------------------


def _cli(capsys, *argv):
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_status_prints_the_effective_limits(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HEAT_GOVERNOR_CONFIG", str(tmp_path / "absent.toml"))
    guard = {
        "action": "kill",
        "armed": True,
        "avail_mib": 10464,
        "last_kill": None,
        "kills_last_hour": 0,
        "limits": {"floor_mib": 3072, "soft_mib": 6144, "source": "platform"},
    }
    src = tmp_path / "state.json"
    src.write_text(json.dumps({"level": "cool", "at": time.time(), "mem_guard": guard}))
    rc, out, err = _cli(capsys, "memory", "status", "--source", str(src))
    assert (rc, err) == (0, "")
    assert "floor 3072 MiB" in out and "soft 6144 MiB" in out and "platform" in out


def test_replay_accepts_absolute_limits(capsys):
    rc, out, _ = _cli(
        capsys,
        "memory",
        "replay",
        "tests/data/memory-2026-11-14.csv",
        "--floor-mib",
        "3072",
        "--soft-mib",
        "6144",
    )
    assert rc == 0 and "floor 3072 MiB soft 6144 MiB" in out


def test_replay_needs_both_absolute_limits(capsys):
    rc, _, err = _cli(
        capsys, "memory", "replay", "tests/data/memory-2026-11-14.csv", "--floor-mib", "3072"
    )
    assert rc == 1 and "together" in err

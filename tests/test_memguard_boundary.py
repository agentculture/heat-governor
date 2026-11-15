"""memguard with ``[[memory.allow]]`` boundaries: blame on every fire, first-crossing events,
far-over action without a trigger fire, once-only would-kills for processes it may not kill.

Fake /proc + /sys under ``tmp_path``, fake pidfd, fake clock; nothing real is signalled.
"""

from __future__ import annotations

from heat_governor.config import AllowRule
from tests.fakes_proc import add_proc
from tests.test_memguard import EPOCH, MIB, falling_wave, make, named, write_meminfo

HOG = AllowRule(name="hog", max_mib=10_000, comm=("hog",))  # hog is 20_000 MiB: far over
BOUND_EV = "memory_boundary_exceeded"


def calm_steps(guard, clock, n=3):
    """n routine scans (one per top_every_s) with memory well above the soft limit."""
    for _ in range(n):
        guard.step()
        clock.mono += guard.rule.top_every_s


# --- far over, no trigger ---------------------------------------------------------------------


def test_far_over_own_process_is_killed_without_a_trigger_fire(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(tmp_path, allow=(HOG,))
    guard.step()
    assert fake.opened == [777]
    (kill,) = named(events, "memory_kill")
    assert kill["reason"] == "boundary"
    assert kill["pid"] == 777
    assert kill["boundary"]["name"] == "hog"
    assert kill["t"] >= EPOCH
    assert kill["blamed"][0]["pid"] == 777
    assert guard.doc()["last_kill"]["pid"] == 777
    assert guard.doc()["kills_last_hour"] == 1


def test_far_over_kill_is_not_repeated_for_the_same_instance(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(tmp_path, allow=(HOG,))
    fake.refuse = -1  # the kill keeps failing: the guard still tries once per instance
    calm_steps(guard, clock)
    assert len(named(events, "memory_would_kill")) == 1


def test_far_over_record_mode_is_one_would_kill(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(tmp_path, action="record", allow=(HOG,))
    calm_steps(guard, clock)
    (would,) = named(events, "memory_would_kill")
    assert would["why_not"] == "record-only"
    assert fake.opened == []


def test_far_over_root_process_is_one_would_kill_only(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(
        tmp_path, allow=(AllowRule(name="vllm", max_mib=10_000, comm=("vllm",)),)
    )
    add_proc(proc, 900, ppid=1, uid=0, comm="vllm", anon_kib=50_000 * MIB)
    calm_steps(guard, clock, n=4)
    (would,) = named(events, "memory_would_kill")
    assert would["why_not"] == "boundary: not killable (uid 0)"
    assert would["pid"] == 900
    assert would["reason"] == "boundary"
    assert would["blamed"][0]["pid"] == 900
    assert fake.opened == []


def test_plain_over_boundary_is_not_acted_on_while_memory_is_fine(tmp_path):
    rule = AllowRule(name="hog", max_mib=18_000, comm=("hog",))  # 20_000 is 11% over: < 50%
    guard, clock, fake, events, _, proc, _ = make(tmp_path, allow=(rule,))
    calm_steps(guard, clock)
    assert not named(events, "memory_would_kill")
    assert fake.opened == []


def test_far_over_budget_is_shared_with_trigger_kills(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(tmp_path, allow=(HOG,))
    guard.killer.import_state({"kills": (clock.mono - 10,) * 3})
    calm_steps(guard, clock)
    (would,) = named(events, "memory_would_kill")
    assert would["why_not"] == "budget"
    assert len(named(events, "memory_budget_exhausted")) == 1
    assert fake.opened == []


def test_total_scope_kills_the_biggest_eligible_member(tmp_path):
    rule = AllowRule(name="w", max_mib=5000, comm=("w",), scope="total")
    guard, clock, fake, events, _, proc, _ = make(tmp_path, allow=(rule,))
    add_proc(proc, 800, ppid=1, uid=0, comm="w", anon_kib=9000 * MIB)
    add_proc(proc, 801, ppid=400, comm="w", anon_kib=3000 * MIB, start=7)
    guard.step()
    (crossed,) = named(events, BOUND_EV)
    assert crossed["pids"] == [800, 801]
    assert crossed["usage_mib"] == 12000.0
    assert fake.opened == [801]


# --- the trigger path -------------------------------------------------------------------------


def test_over_boundary_own_process_is_the_first_victim_on_a_fire(tmp_path):
    rule = AllowRule(name="model", max_mib=10_000, comm=("model",))
    guard, clock, fake, events, _, proc, _ = make(tmp_path, allow=(rule,))
    add_proc(proc, 778, ppid=400, comm="model", anon_kib=12_000 * MIB, start=99)
    falling_wave(guard, clock, proc, fake)
    (kill,) = named(events, "memory_kill")
    assert kill["pid"] == 778
    assert kill["victim"] == "boundary"
    assert kill["blamed"][0]["pid"] == 778


def test_within_boundary_process_is_never_the_victim_on_a_fire(tmp_path):
    rule = AllowRule(name="hog", max_mib=30_000, comm=("hog",))
    guard, clock, fake, events, _, proc, _ = make(tmp_path, allow=(rule,))
    falling_wave(guard, clock, proc, fake)
    assert fake.opened == []
    would = named(events, "memory_would_kill")
    assert would[0]["why_not"].startswith("no-victim")
    assert all(b["pid"] != 777 for e in would for b in e["blamed"])


def test_fire_events_carry_blame_without_boundaries(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(tmp_path, action="record")
    add_proc(proc, 900, ppid=1, uid=0, comm="rootjob", anon_kib=30_000 * MIB)
    falling_wave(guard, clock, proc, fake)
    (would,) = named(events, "memory_would_kill")
    assert [b["pid"] for b in would["blamed"]] == [900, 777]
    assert would["blamed"][0]["boundary"] is None


# --- first crossing ---------------------------------------------------------------------------


def test_first_crossing_event_once_and_reset_with_hysteresis(tmp_path):
    rule = AllowRule(name="hog", max_mib=18_000, comm=("hog",))
    guard, clock, fake, events, _, proc, _ = make(tmp_path, allow=(rule,))
    calm_steps(guard, clock)
    (ev,) = named(events, BOUND_EV)
    assert ev["rule"] == "hog"
    assert ev["pid"] == 777
    assert ev["usage_mib"] == 20000.0
    assert ev["max_mib"] == 18_000
    assert ev["scope"] == "process"
    assert ev["blamed"][0]["pid"] == 777
    assert ev["t"] >= EPOCH
    add_proc(proc, 777, ppid=400, comm="hog", anon_kib=17_500 * MIB, start=4242)  # < 5% under
    calm_steps(guard, clock, n=1)
    add_proc(proc, 777, ppid=400, comm="hog", anon_kib=20_000 * MIB, start=4242)
    calm_steps(guard, clock, n=1)
    assert len(named(events, BOUND_EV)) == 1
    add_proc(proc, 777, ppid=400, comm="hog", anon_kib=17_000 * MIB, start=4242)  # >= 5% under
    calm_steps(guard, clock, n=1)
    add_proc(proc, 777, ppid=400, comm="hog", anon_kib=20_000 * MIB, start=4242)
    calm_steps(guard, clock, n=1)
    assert len(named(events, BOUND_EV)) == 2


# --- doc and cadence --------------------------------------------------------------------------


def test_doc_boundaries_shape(tmp_path):
    rules = (HOG, AllowRule(name="none", max_mib=64, comm=("nothing",), scope="total"))
    guard, clock, fake, events, _, proc, _ = make(tmp_path, action="record", allow=rules)
    before = guard.doc()["boundaries"]
    assert [b["state"] for b in before] == ["idle", "idle"]
    guard.step()
    assert guard.doc()["boundaries"] == [
        {"name": "hog", "max_mib": 10_000, "scope": "process", "usage_mib": 20000.0,
         "state": "far_over", "comm": ["hog"], "cmdline": None, "cgroup": None,
         "source": "config.toml"},
        {"name": "none", "max_mib": 64, "scope": "total", "usage_mib": 0.0, "state": "idle",
         "comm": ["nothing"], "cmdline": None, "cgroup": None, "source": "config.toml"},
    ]  # fmt: skip


MATCHER_KEYS = {"name", "max_mib", "scope", "usage_mib", "state", "comm", "cmdline", "cgroup",
                "source"}  # fmt: skip


def test_every_boundary_entry_carries_matchers_and_source_idle_and_scanned(tmp_path):
    rule = AllowRule(
        name="c", max_mib=64, comm=("a", "b"), cmdline="*x*", cgroup="*y*", source="boundaries.toml"
    )
    guard, clock, *_ = make(tmp_path, action="record", allow=(HOG, rule))
    idle = guard.doc()["boundaries"]
    guard.step()
    scanned = guard.doc()["boundaries"]
    for entries in (idle, scanned):
        assert all(set(b) == MATCHER_KEYS for b in entries)
    assert idle[1]["comm"] == ["a", "b"]
    assert (idle[1]["cmdline"], idle[1]["cgroup"]) == ("*x*", "*y*")
    assert idle[1]["source"] == scanned[1]["source"] == "boundaries.toml"
    assert scanned[0]["state"] == "far_over"
    assert isinstance(scanned[1]["comm"], list)


def test_idle_doc_carries_matchers_and_source():
    from heat_governor import memboundary

    (entry,) = memboundary.idle_doc((HOG,))
    assert set(entry) == MATCHER_KEYS
    assert entry["source"] == "config.toml"


def test_allow_rule_source_defaults_and_is_not_a_toml_key():
    from heat_governor import config

    assert HOG.source == "config.toml"
    assert "source" not in config.ALLOW_KEYS
    cfg = config.Config(
        data={"memory": {"allow": [{"name": "x", "max_mib": 128, "comm": ["x"], "source": "y"}]}}
    )
    assert any("source" in p for p in config.validate(cfg.data))


def test_doc_carries_act_over_pct_kill_budget_and_min_victim_from_config(tmp_path):
    guard, *_ = make(
        tmp_path, action="record", act_over_pct=33.0, max_kills_per_hour=7, min_victim_mib=999
    )
    for _ in range(2):  # the placeholder doc, then a scanned one
        d = guard.doc()
        assert (d["act_over_pct"], d["max_kills_per_hour"], d["min_victim_mib"]) == (33.0, 7, 999)
        guard.step()


def test_doc_boundaries_is_a_copy(tmp_path):
    guard, clock, fake, events, _, proc, _ = make(tmp_path, action="record", allow=(HOG,))
    guard.step()
    guard.doc()["boundaries"][0]["state"] = "x"
    assert guard.doc()["boundaries"][0]["state"] == "far_over"


def test_boundaries_add_no_scans(tmp_path, monkeypatch):
    from heat_governor import memguard

    guard, clock, _, events, _, proc, _ = make(tmp_path, action="record", allow=(HOG,))
    scans = []
    real = memguard.memprocs.scan
    monkeypatch.setattr(memguard.memprocs, "scan", lambda root: scans.append(1) or real(root))
    for _ in range(81):  # 20 s at 0.25 s
        guard.step()
        clock.mono += 0.25
    assert len(scans) == 3


# --- wiring -----------------------------------------------------------------------------------


def test_boundary_event_is_recorded_as_a_memory_event():
    from heat_governor import recorder

    assert BOUND_EV in recorder.MEMORY_EVENTS


def test_boundary_event_is_shipped():
    from heat_governor import daemon

    assert BOUND_EV in daemon.SHIP_EVENTS


# --- a broken boundary path never blocks the trigger ------------------------------------------


def test_failing_assessment_never_blocks_a_trigger_kill(tmp_path, monkeypatch):
    from heat_governor import memguard

    def boom(*_a, **_k):
        raise RuntimeError("assess broke")

    monkeypatch.setattr(memguard.memboundary, "assess", boom)
    guard, clock, fake, events, logs, proc, _ = make(tmp_path, allow=(HOG,))
    falling_wave(guard, clock, proc, fake)
    assert fake.opened == [777]
    assert len([m for m in logs if "assess broke" in m]) == 1


def test_failing_blame_never_blocks_a_trigger_kill(tmp_path, monkeypatch):
    from heat_governor import memguard

    def boom(*_a, **_k):
        raise RuntimeError("blame broke")

    monkeypatch.setattr(memguard.memboundary, "blame", boom)
    guard, clock, fake, events, logs, proc, _ = make(tmp_path)
    falling_wave(guard, clock, proc, fake)
    (kill,) = named(events, "memory_kill")
    assert kill["blamed"] == []


def test_fire_and_far_over_on_the_same_poll_record_one_row(tmp_path):
    rule = AllowRule(name="hog", max_mib=18_000, comm=("hog",))  # 20_000: over, not far
    guard, clock, fake, events, _, proc, _ = make(tmp_path, action="record", allow=(rule,))
    write_meminfo(proc, 5000)  # below the floor: a fire on the second poll
    guard.step()
    clock.mono += 0.25
    add_proc(proc, 777, ppid=400, comm="hog", anon_kib=40_000 * MIB, start=4242)  # far over
    guard.step()
    (would,) = named(events, "memory_would_kill")
    assert would["reason"] == "floor"
    assert would["pid"] == 777

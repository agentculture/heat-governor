"""memkill with boundaries: within-boundary processes are never chosen, over-boundary ones are
chosen first, and ``act_on`` kills one named process under the same protections and budget.

Fake /proc tree and fake pidfd only; nothing real is ever signalled.
"""

from __future__ import annotations

from heat_governor import memboundary, memkill, memprocs
from heat_governor.config import AllowRule, MemoryRule
from tests.fakes_proc import add_proc
from tests.test_memkill import ME, MIB, SHELL, UID, FakeTracker, base_tree, make


def _assess(procs, *rules, pct=50.0):
    return memboundary.assess(procs, rules, pct)


def _proc(root, pid, comm, mib, uid=UID, start=100):
    cg = f"/user.slice/user-1000.slice/{comm}-{pid}.scope"
    add_proc(root, pid, ppid=SHELL, uid=uid, comm=comm, anon_kib=mib * MIB, start=start, cgroup=cg)


# --- choose -----------------------------------------------------------------------------------


def test_within_boundary_fastest_grower_is_never_chosen(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "model", 30_000)
    _proc(tmp_path, 601, "eater", 3000)
    procs = memprocs.scan(tmp_path)
    bounds = _assess(procs, AllowRule(name="model", max_mib=40_000, comm=("model",)))
    k, _ = make(tmp_path)
    c = k.choose(procs, FakeTracker({600: 900.0, 601: 1.0}), bounds=bounds)
    assert c.proc.pid == 601


def test_within_boundary_only_candidate_means_no_victim(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "model", 30_000)
    procs = memprocs.scan(tmp_path)
    bounds = _assess(procs, AllowRule(name="model", max_mib=40_000, comm=("model",)))
    k, _ = make(tmp_path)
    c = k.choose(procs, FakeTracker({600: 900.0}), bounds=bounds)
    assert c.proc is None


def test_over_boundary_process_is_chosen_before_a_faster_grower(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "model", 12_000)
    _proc(tmp_path, 601, "eater", 20_000)
    procs = memprocs.scan(tmp_path)
    bounds = _assess(procs, AllowRule(name="model", max_mib=10_000, comm=("model",)))
    k, _ = make(tmp_path)
    c = k.choose(procs, FakeTracker({600: 0.0, 601: 900.0}), bounds=bounds)
    assert c.proc.pid == 600
    assert c.how == "boundary"
    assert c.boundary["name"] == "model"
    assert c.boundary["over_mib"] == 2000.0


def test_most_over_boundary_process_wins(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "a", 12_000)
    _proc(tmp_path, 601, "b", 15_000)
    procs = memprocs.scan(tmp_path)
    bounds = _assess(
        procs,
        AllowRule(name="a", max_mib=10_000, comm=("a",)),
        AllowRule(name="b", max_mib=14_000, comm=("b",)),
    )
    k, _ = make(tmp_path)
    assert k.choose(procs, FakeTracker(), bounds=bounds).proc.pid == 600


def test_over_boundary_root_process_is_not_chosen_falls_back_to_growth(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "vllm", 50_000, uid=0)
    _proc(tmp_path, 601, "eater", 3000)
    procs = memprocs.scan(tmp_path)
    bounds = _assess(procs, AllowRule(name="vllm", max_mib=10_000, comm=("vllm",)))
    k, _ = make(tmp_path)
    c = k.choose(procs, FakeTracker({601: 5.0}), bounds=bounds)
    assert c.proc.pid == 601
    assert c.how == "process"


def test_choose_without_bounds_is_unchanged(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "model", 30_000)
    procs = memprocs.scan(tmp_path)
    k, _ = make(tmp_path)
    assert k.choose(procs, FakeTracker({600: 9.0})).proc.pid == 600


def test_act_with_bounds_kills_the_over_boundary_process(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "model", 12_000)
    _proc(tmp_path, 601, "eater", 20_000)
    procs = memprocs.scan(tmp_path)
    bounds = _assess(procs, AllowRule(name="model", max_mib=10_000, comm=("model",)))
    k, fd = make(tmp_path)
    (ev,) = k.act(procs, FakeTracker({601: 900.0}), "horizon", bounds=bounds)
    assert ev["event"] == "memory_kill"
    assert ev["pid"] == 600
    assert ev["victim"] == "boundary"
    assert ev["boundary"]["name"] == "model"
    assert fd.opened == [600]


# --- act_on / refusal -------------------------------------------------------------------------

BOUND = {"name": "b", "max_mib": 1000, "usage_mib": 3000.0, "over_mib": 2000.0, "scope": "process"}


def test_act_on_kills_an_eligible_process_through_pidfd(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "eater", 3000)
    procs = memprocs.scan(tmp_path)
    k, fd = make(tmp_path)
    (ev,) = k.act_on(procs, FakeTracker(), procs[600], "boundary", boundary=BOUND)
    assert ev["event"] == "memory_kill"
    assert ev["reason"] == "boundary"
    assert ev["boundary"] == BOUND
    assert ev["victim"] == "boundary"
    assert fd.sent == [(9600, 9)]


def test_act_on_root_process_is_a_would_kill_not_killable(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "vllm", 3000, uid=0)
    procs = memprocs.scan(tmp_path)
    k, fd = make(tmp_path)
    (ev,) = k.act_on(procs, FakeTracker(), procs[600], "boundary", boundary=BOUND)
    assert ev["event"] == "memory_would_kill"
    assert ev["why_not"] == "boundary: not killable (uid 0)"
    assert fd.opened == []


def test_act_on_protected_process_is_not_killable(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "sshd", 3000)
    procs = memprocs.scan(tmp_path)
    k, fd = make(tmp_path)
    (ev,) = k.act_on(procs, FakeTracker(), procs[600], "boundary")
    assert ev["why_not"] == "boundary: not killable (protected)"
    assert fd.opened == []


def test_act_on_the_daemon_itself_is_not_killable(tmp_path):
    base_tree(tmp_path)
    procs = memprocs.scan(tmp_path)
    k, fd = make(tmp_path)
    (ev,) = k.act_on(procs, FakeTracker(), procs[ME], "boundary")
    assert ev["why_not"] == "boundary: not killable (self-or-parent)"
    assert fd.opened == []


def test_act_on_below_min_victim_is_not_killable(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "small", 500)
    procs = memprocs.scan(tmp_path)
    k, fd = make(tmp_path)
    (ev,) = k.act_on(procs, FakeTracker(), procs[600], "boundary")
    assert ev["why_not"] == "boundary: not killable (below min_victim_mib 2048)"
    assert fd.opened == []


def test_act_on_record_mode_never_signals(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "eater", 3000)
    procs = memprocs.scan(tmp_path)
    k, fd = make(tmp_path, rule=MemoryRule(action="record"))
    (ev,) = k.act_on(procs, FakeTracker(), procs[600], "boundary")
    assert ev["why_not"] == "record-only"
    assert fd.opened == []


def test_act_on_shares_the_budget_with_trigger_kills(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "eater", 3000)
    procs = memprocs.scan(tmp_path)
    k, fd = make(tmp_path, rule=MemoryRule(action="kill", max_kills_per_hour=1))
    (first,) = k.act(procs, FakeTracker({600: 5.0}), "horizon")
    assert first["event"] == "memory_kill"
    _proc(tmp_path, 601, "other", 3000)
    procs = memprocs.scan(tmp_path)
    events = k.act_on(procs, FakeTracker(), procs[601], "boundary")
    assert [e["event"] for e in events] == ["memory_would_kill", "memory_budget_exhausted"]
    assert events[0]["why_not"] == "budget"
    assert fd.opened == [600]


def test_refusal_of_a_root_daemon(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "job", 3000, uid=0)
    procs = memprocs.scan(tmp_path)
    k = memkill.MemKiller(MemoryRule(action="kill"), proc_root=tmp_path, uid=0, self_pid=ME)
    assert k.refusal(procs, procs[600]) == "daemon runs as root"


def test_refusal_none_for_an_eligible_process(tmp_path):
    base_tree(tmp_path)
    _proc(tmp_path, 600, "eater", 3000)
    procs = memprocs.scan(tmp_path)
    k, _ = make(tmp_path)
    assert k.refusal(procs, procs[600]) is None

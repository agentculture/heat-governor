"""memboundary: matching, per-rule usage, classification, blame ranking, the once-only watch.

Pure: processes are built as ``memprocs.Proc`` values, no /proc, no threads, no signals.
"""

from __future__ import annotations

from heat_governor import memboundary
from heat_governor.config import AllowRule
from heat_governor.memprocs import Proc

HEX = "0123456789abcdef" * 4  # 64 hex chars


def proc(pid, anon, *, uid=1000, comm="proc", cmdline=None, cgroup="/user.slice/s.scope", start=1):
    return Proc(
        pid=pid,
        ppid=1,
        uid=uid,
        comm=comm,
        cmdline=cmdline if cmdline is not None else comm,
        rss_anon_mib=float(anon),
        rss_mib=float(anon),
        start_ticks=start,
        cgroup=cgroup,
    )


def procs(*ps):
    return {p.pid: p for p in ps}


class FakeTracker:
    def __init__(self, rates=None):
        self.rates = rates or {}

    def growth(self, pid, start):
        return self.rates.get(pid, 0.0)


# --- matching ---------------------------------------------------------------------------------


def test_comm_matches_exact_name():
    rule = AllowRule(name="r", max_mib=100, comm=("vllm",))
    assert memboundary.matches(rule, proc(1, 10, comm="vllm"))


def test_comm_does_not_match_a_prefix():
    rule = AllowRule(name="r", max_mib=100, comm=("vllm",))
    assert not memboundary.matches(rule, proc(1, 10, comm="vllm-worker"))


def test_comm_matches_argv0_basename():
    rule = AllowRule(name="r", max_mib=100, comm=("vllm",))
    p = proc(1, 10, comm="python3", cmdline="/opt/bin/vllm serve x")
    assert memboundary.matches(rule, p)


def test_comm_uses_injected_names_for_exe():
    rule = AllowRule(name="r", max_mib=100, comm=("node",))
    p = proc(1, 10, comm="MainThread", cmdline="x")
    assert memboundary.matches(rule, p, names=lambda q: {q.comm, "node"})


def test_cmdline_glob():
    rule = AllowRule(name="r", max_mib=100, cmdline="*vllm*serve*")
    assert memboundary.matches(rule, proc(1, 10, cmdline="python -m vllm.entry serve m"))


def test_cmdline_glob_miss():
    rule = AllowRule(name="r", max_mib=100, cmdline="*vllm*serve*")
    assert not memboundary.matches(rule, proc(1, 10, cmdline="python -m vllm.entry bench"))


def test_cgroup_glob():
    rule = AllowRule(name="r", max_mib=100, cgroup="*docker-*.scope")
    p = proc(1, 10, cgroup=f"/system.slice/docker-{HEX}.scope")
    assert memboundary.matches(rule, p)


def test_all_given_matchers_must_match():
    rule = AllowRule(name="r", max_mib=100, comm=("python3",), cgroup="*docker-*.scope")
    host = proc(1, 10, comm="python3", cgroup="/user.slice/s.scope")
    assert not memboundary.matches(rule, host)


def test_all_given_matchers_match_together():
    rule = AllowRule(name="r", max_mib=100, comm=("python3",), cgroup="*docker-*.scope")
    boxed = proc(1, 10, comm="python3", cgroup=f"/system.slice/docker-{HEX}.scope")
    assert memboundary.matches(rule, boxed)


def test_tightest_rule_wins():
    loose = AllowRule(name="loose", max_mib=1000, comm=("py",))
    tight = AllowRule(name="tight", max_mib=200, cmdline="*train*")
    p = proc(1, 10, comm="py", cmdline="py train.py")
    assert memboundary.applied_rule((loose, tight), p) is tight


def test_no_rule_matches():
    rule = AllowRule(name="r", max_mib=100, comm=("x",))
    assert memboundary.applied_rule((rule,), proc(1, 10)) is None


# --- classification ---------------------------------------------------------------------------


def test_classify_within_at_the_limit():
    assert memboundary.classify(100.0, 100, 50.0) == "within"


def test_classify_over_just_past_the_limit():
    assert memboundary.classify(100.5, 100, 50.0) == "over"


def test_classify_over_at_the_far_line():
    assert memboundary.classify(150.0, 100, 50.0) == "over"


def test_classify_far_over_past_the_far_line():
    assert memboundary.classify(150.5, 100, 50.0) == "far_over"


def test_classify_act_over_pct_moves_the_far_line():
    assert memboundary.classify(111.0, 100, 10.0) == "far_over"


# --- scope ------------------------------------------------------------------------------------


def test_scope_process_judges_each_match_alone():
    rule = AllowRule(name="w", max_mib=100, comm=("w",))
    a = memboundary.assess(procs(proc(1, 80, comm="w"), proc(2, 120, comm="w")), (rule,), 50.0)
    assert a.state(1) == "within"
    assert a.state(2) == "over"


def test_scope_total_sums_all_matches():
    rule = AllowRule(name="w", max_mib=100, comm=("w",), scope="total")
    a = memboundary.assess(procs(proc(1, 80, comm="w"), proc(2, 40, comm="w")), (rule,), 50.0)
    assert a.state(1) == "over"
    assert a.state(2) == "over"
    assert a.status(1).usage_mib == 120.0
    assert a.status(1).over_mib == 20.0


def test_unmatched_process_is_unbounded():
    rule = AllowRule(name="w", max_mib=100, comm=("w",))
    a = memboundary.assess(procs(proc(1, 80, comm="other")), (rule,), 50.0)
    assert a.state(1) == "unbounded"
    assert a.status(1) is None


def test_within_set_lists_only_within_processes():
    rule = AllowRule(name="w", max_mib=100, comm=("w",))
    a = memboundary.assess(
        procs(proc(1, 80, comm="w"), proc(2, 120, comm="w"), proc(3, 5)), (rule,), 50.0
    )
    assert a.within == frozenset({1})


def test_doc_shape_and_states():
    rules = (
        AllowRule(name="p", max_mib=100, comm=("p",)),
        AllowRule(name="t", max_mib=100, comm=("t",), scope="total"),
        AllowRule(name="idle", max_mib=100, comm=("nobody",)),
    )
    ps = procs(proc(1, 90, comm="p"), proc(2, 300, comm="p"), proc(3, 60, comm="t"))
    doc = memboundary.assess(ps, rules, 50.0).doc()
    assert doc == [
        {"name": "p", "max_mib": 100, "scope": "process", "usage_mib": 300.0, "state": "far_over"},
        {"name": "t", "max_mib": 100, "scope": "total", "usage_mib": 60.0, "state": "within"},
        {"name": "idle", "max_mib": 100, "scope": "process", "usage_mib": 0.0, "state": "idle"},
    ]


def test_idle_doc_without_a_scan():
    rules = (AllowRule(name="x", max_mib=64, comm=("x",)),)
    assert memboundary.idle_doc(rules) == [
        {"name": "x", "max_mib": 64, "scope": "process", "usage_mib": 0.0, "state": "idle"}
    ]


# --- container ids ----------------------------------------------------------------------------


def test_container_id_from_a_systemd_docker_scope():
    cg = f"/system.slice/docker-{HEX}.scope"
    assert memboundary.container_id(cg) == HEX[:12]


def test_container_id_from_a_cgroupfs_docker_path():
    assert memboundary.container_id(f"/docker/{HEX}") == HEX[:12]


def test_container_id_from_containerd():
    cg = f"/kubepods.slice/kubepods-pod1.slice/cri-containerd-{HEX}.scope"
    assert memboundary.container_id(cg) == HEX[:12]


def test_no_container_id_for_a_session():
    assert memboundary.container_id("/user.slice/user-1000.slice/session-4.scope") is None


# --- blame ------------------------------------------------------------------------------------


def test_blame_orders_over_boundary_then_growth_then_size_across_uids():
    rule = AllowRule(name="vllm", max_mib=1000, cgroup="*docker-*")
    ps = procs(
        proc(10, 1500, uid=0, comm="vllm", cgroup=f"/system.slice/docker-{HEX}.scope"),
        proc(20, 300, uid=1000, comm="grower"),
        proc(30, 9000, uid=1001, comm="big"),
        proc(40, 100, uid=1000, comm="tiny"),
    )
    a = memboundary.assess(ps, (rule,), 50.0)
    blamed = memboundary.blame(ps, FakeTracker({20: 50.0}), a)
    assert [b["pid"] for b in blamed] == [10, 20, 30]


def test_blame_entry_shape_for_a_root_container_process():
    rule = AllowRule(name="vllm", max_mib=1000, cgroup="*docker-*")
    cg = f"/system.slice/docker-{HEX}.scope"
    ps = procs(proc(10, 1500, uid=0, comm="vllm", cmdline="vllm --api-key s3cr3t", cgroup=cg))
    a = memboundary.assess(ps, (rule,), 50.0)
    assert memboundary.blame(ps, FakeTracker({10: 2.5}), a) == [
        {
            "pid": 10,
            "uid": 0,
            "comm": "vllm",
            "cmdline": "vllm --api-key ***",
            "rss_anon_mib": 1500.0,
            "growth_mib_s": 2.5,
            "cgroup": cg,
            "container": HEX[:12],
            "boundary": {
                "name": "vllm",
                "max_mib": 1000,
                "usage_mib": 1500.0,
                "over_mib": 500.0,
                "scope": "process",
            },
        }
    ]


def test_blame_negative_growth_counts_as_none():
    ps = procs(proc(1, 500, comm="shrinking"), proc(2, 400, comm="flat"))
    blamed = memboundary.blame(ps, FakeTracker({1: -9.0}), memboundary.assess(ps, (), 50.0))
    assert [b["pid"] for b in blamed] == [1, 2]
    assert blamed[0]["growth_mib_s"] == -9.0


def test_within_boundary_process_is_never_blamed_even_as_fastest_grower():
    rule = AllowRule(name="ok", max_mib=10_000, comm=("model",))
    ps = procs(proc(1, 5000, comm="model"), proc(2, 100, comm="a"), proc(3, 50, comm="b"))
    a = memboundary.assess(ps, (rule,), 50.0)
    blamed = memboundary.blame(ps, FakeTracker({1: 999.0}), a)
    assert 1 not in [b["pid"] for b in blamed]


def test_blame_at_most_n():
    ps = procs(*(proc(i, 100 + i) for i in range(1, 8)))
    assert len(memboundary.blame(ps, None, memboundary.assess(ps, (), 50.0))) == 3


# --- the watch: first crossings and once-only actions -----------------------------------------


def _assess(ps, rules):
    return memboundary.assess(procs(*ps), rules, 50.0)


def test_first_crossing_reported_once_then_reset_with_hysteresis():
    rule = AllowRule(name="w", max_mib=1000, comm=("w",))
    w = memboundary.Watch()
    assert w.crossings(_assess([proc(1, 900, comm="w")], (rule,))) == []
    first = w.crossings(_assess([proc(1, 1100, comm="w")], (rule,)))
    assert [s.rule.name for s in first] == ["w"]
    assert w.crossings(_assess([proc(1, 1200, comm="w")], (rule,))) == []
    assert w.crossings(_assess([proc(1, 960, comm="w")], (rule,))) == []  # under, but < 5%
    assert w.crossings(_assess([proc(1, 1100, comm="w")], (rule,))) == []
    assert w.crossings(_assess([proc(1, 940, comm="w")], (rule,))) == []  # >= 5% under: reset
    assert len(w.crossings(_assess([proc(1, 1100, comm="w")], (rule,)))) == 1


def test_first_crossing_is_per_process_instance():
    rule = AllowRule(name="w", max_mib=1000, comm=("w",))
    w = memboundary.Watch()
    assert len(w.crossings(_assess([proc(1, 1100, comm="w", start=5)], (rule,)))) == 1
    assert len(w.crossings(_assess([proc(1, 1100, comm="w", start=6)], (rule,)))) == 1


def test_first_crossing_for_a_total_lists_all_members():
    rule = AllowRule(name="t", max_mib=1000, comm=("w",), scope="total")
    w = memboundary.Watch()
    got = w.crossings(_assess([proc(1, 600, comm="w"), proc(2, 600, comm="w")], (rule,)))
    assert len(got) == 1
    assert [p.pid for p in got[0].members] == [1, 2]
    assert got[0].usage_mib == 1200.0


def test_due_returns_a_far_over_victim_once():
    rule = AllowRule(name="w", max_mib=1000, comm=("w",))
    w = memboundary.Watch()
    a = _assess([proc(1, 2000, comm="w")], (rule,))
    first = w.due(a, lambda s: s.members[0])
    assert [(s.rule.name, p.pid) for s, p in first] == [("w", 1)]
    assert w.due(a, lambda s: s.members[0]) == []


def test_due_ignores_plain_over():
    rule = AllowRule(name="w", max_mib=1000, comm=("w",))
    a = _assess([proc(1, 1400, comm="w")], (rule,))
    assert memboundary.Watch().due(a, lambda s: s.members[0]) == []


def test_due_total_victim_from_the_picker():
    rule = AllowRule(name="t", max_mib=1000, comm=("w",), scope="total")
    a = _assess([proc(1, 900, comm="w"), proc(2, 900, comm="w")], (rule,))
    due = memboundary.Watch().due(a, lambda s: max(s.members, key=lambda p: p.pid))
    assert [p.pid for _, p in due] == [2]


def test_due_skips_a_set_without_a_victim():
    rule = AllowRule(name="w", max_mib=1000, comm=("w",))
    a = _assess([proc(1, 2000, comm="w")], (rule,))
    assert memboundary.Watch().due(a, lambda s: None) == []

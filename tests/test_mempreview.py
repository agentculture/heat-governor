"""mempreview: a draft rule against a fake /proc tree; nothing is ever signalled."""

from __future__ import annotations

import os

from heat_governor import memboundary, memkill, mempreview, memprocs
from heat_governor.config import AllowRule, MemoryRule
from tests.fakes_proc import add_proc
from tests.test_memkill import ME, MIB, SHELL, UID, base_tree

CID = "ab" * 32


def _p(root, pid, comm, mib, uid=UID, cmdline=None, cgroup="/user.slice/x.scope"):
    add_proc(
        root,
        pid,
        ppid=SHELL,
        uid=uid,
        comm=comm,
        anon_kib=mib * MIB,
        cmdline=cmdline,
        cgroup=cgroup,
    )


def _tree(tmp_path):
    base_tree(tmp_path)
    _p(tmp_path, 600, "hog", 30_000, cgroup=f"/system.slice/docker-{CID}.scope")
    _p(tmp_path, 601, "hog", 11_000)
    _p(tmp_path, 602, "hog", 500)  # below min_victim_mib
    _p(tmp_path, 603, "hog", 40_000, uid=0)  # root: refused
    _p(tmp_path, 604, "other", 40_000)  # unmatched
    return memprocs.scan(tmp_path)


def _run(tmp_path, rule, memory=None, **kw):
    procs = _tree(tmp_path)
    memory = memory or MemoryRule(action="kill")
    out = mempreview.preview(
        procs,
        (rule,),
        memory,
        proc_root=tmp_path,
        uid=UID,
        self_pid=ME,
        user_of=lambda u: "u",
        **kw,
    )
    return procs, memory, out


RULE = AllowRule(name="hog", max_mib=10_000, comm=("hog",))


def test_states_and_verdicts_equal_assess_and_refusal(tmp_path):
    procs, memory, out = _run(tmp_path, RULE)
    bounds = memboundary.assess(procs, (RULE,), memory.act_over_pct)
    killer = memkill.MemKiller(memory, proc_root=tmp_path, uid=UID, self_pid=ME)
    assert {r["pid"] for r in out["processes"]} == {600, 601, 602, 603}
    for r in out["processes"]:
        why = killer.refusal(procs, procs[r["pid"]])
        assert r["state"] == bounds.state(r["pid"])
        assert r["killable"] is (why is None)
        assert r["reason"] == why


def test_result_shape(tmp_path):
    procs, _, out = _run(tmp_path, RULE)
    assert out["matching"] == 4
    assert out["action"] == "kill"
    first = out["processes"][0]
    assert first["pid"] == 603
    assert first["user"] == "u"
    assert first["container"] is None
    big = next(r for r in out["processes"] if r["pid"] == 600)
    assert big["container"] == CID[:12]
    assert big["rss_anon_mib"] == 30_000
    assert big["state"] == "far_over"
    assert big["far_over_killable"] is True


def test_far_over_killable_counts_only_killable_far_over(tmp_path):
    _, _, out = _run(tmp_path, RULE)
    assert out["far_over_killable"] == 1  # 603 is far over but root; 601 is only over


def test_cmdline_is_redacted(tmp_path):
    base_tree(tmp_path)
    _p(tmp_path, 600, "hog", 30_000, cmdline=["hog", "--token=SECRET123"])
    procs = memprocs.scan(tmp_path)
    out = mempreview.preview(procs, (RULE,), MemoryRule(), tmp_path, uid=UID, self_pid=ME)
    assert "SECRET123" not in out["processes"][0]["cmdline"]
    assert memprocs.MASK in out["processes"][0]["cmdline"]


def test_names_agree_with_the_killer_exe_basename(tmp_path):
    base_tree(tmp_path)
    _p(tmp_path, 600, "worker", 30_000)
    os.symlink("/opt/bin/hog", tmp_path / "600" / "exe")
    procs = memprocs.scan(tmp_path)
    out = mempreview.preview(procs, (RULE,), MemoryRule(), tmp_path, uid=UID, self_pid=ME)
    assert [r["pid"] for r in out["processes"]] == [600]
    plain = memboundary.assess(procs, (RULE,), 50.0)
    assert plain.status(600) is None  # default names cannot see the exe


def test_protect_list_and_min_victim_follow_the_memory_rule(tmp_path):
    memory = MemoryRule(action="record", protect=("hog",), min_victim_mib=100)
    _, _, out = _run(tmp_path, RULE, memory)
    assert out["action"] == "record"
    assert {r["reason"] for r in out["processes"] if r["uid"] == UID} == {"protected"}
    assert out["far_over_killable"] == 0


def test_no_match_and_no_rules(tmp_path):
    procs = _tree(tmp_path)
    out = mempreview.preview(procs, (), MemoryRule(), tmp_path, uid=UID, self_pid=ME)
    assert out["matching"] == 0
    assert out["processes"] == []

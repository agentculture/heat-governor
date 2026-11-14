import shutil
import sys

import pytest

from heat_governor import memprocs
from tests.fakes_proc import add_proc

MIB = 1024


def test_scan_fields(tmp_path):
    add_proc(
        tmp_path,
        5,
        ppid=2,
        uid=7,
        comm="py (x)",
        anon_kib=10 * MIB,
        file_kib=5 * MIB,
        start=777,
        cgroup="/a.slice/b.scope",
        cmdline=["py", "-c", "1"],
    )
    p = memprocs.scan(tmp_path)[5]
    assert (p.ppid, p.uid, p.comm, p.start_ticks, p.cgroup) == (
        2,
        7,
        "py (x)",
        777,
        "/a.slice/b.scope",
    )
    assert p.rss_anon_mib == 10 and p.rss_mib == 15 and p.cmdline == "py -c 1"


def test_file_rss_flat_anon_zero_growth_and_growing_anon(tmp_path):
    add_proc(tmp_path, 10, anon_kib=100 * MIB, file_kib=50_000 * MIB)
    add_proc(tmp_path, 11, anon_kib=100 * MIB)
    t = memprocs.Tracker(window_s=60)
    t.update(memprocs.scan(tmp_path), now=0)
    add_proc(tmp_path, 10, anon_kib=100 * MIB, file_kib=90_000 * MIB)
    add_proc(tmp_path, 11, anon_kib=200 * MIB)
    procs = memprocs.scan(tmp_path)
    t.update(procs, now=10)
    assert t.growth(10, 100) == 0.0
    assert t.growth(11, 100) == pytest.approx(10.0)
    assert t.growth(99, 1) == 0.0


def test_window_drops_old_samples(tmp_path):
    t = memprocs.Tracker(window_s=10)
    for i, anon in enumerate([0, 1000, 1010, 1020]):
        add_proc(tmp_path, 3, anon_kib=anon * MIB)
        t.update(memprocs.scan(tmp_path), now=i * 10)
    assert t.growth(3, 100) == pytest.approx(1.0)


def test_vanished_pid_skipped(tmp_path):
    add_proc(tmp_path, 1, comm="systemd")
    add_proc(tmp_path, 2)
    shutil.rmtree(tmp_path / "2")
    (tmp_path / "3").mkdir()  # half-gone: no files
    (tmp_path / "4").mkdir()
    (tmp_path / "4" / "status").write_text("Name:\tx\n")  # truncated
    assert set(memprocs.scan(tmp_path)) == {1}
    assert memprocs.scan(tmp_path / "nope") == {}


def _tree(tmp_path):
    sc = "/user.slice/session-545.scope"
    add_proc(tmp_path, 1, ppid=0, uid=0, comm="systemd", cgroup="/init.scope")
    add_proc(tmp_path, 100, ppid=1, uid=0, comm="sshd", cgroup=sc)
    add_proc(tmp_path, 101, ppid=100, comm="bash", cgroup=sc, anon_kib=5 * MIB)
    add_proc(tmp_path, 102, ppid=101, comm="python", cgroup=sc, anon_kib=300 * MIB)
    add_proc(tmp_path, 103, ppid=102, comm="worker", cgroup=sc, anon_kib=200 * MIB)
    add_proc(tmp_path, 200, ppid=1, comm="app", cgroup="/user.slice/app.slice", anon_kib=50 * MIB)
    add_proc(tmp_path, 300, ppid=1, uid=0, comm="rootd", cgroup="/system.slice", anon_kib=999 * MIB)
    return memprocs.scan(tmp_path)


def test_ancestry_and_groups(tmp_path):
    procs = _tree(tmp_path)
    assert memprocs.ancestors(procs, 103) == [102, 101, 100, 1]
    assert memprocs.top_level(procs, 103) == 101
    assert memprocs.top_level(procs, 200) == 200
    g = memprocs.groups(procs, 1000)
    assert [(x["pid"], x["rss_anon_mib"], x["procs"]) for x in g] == [(101, 505, 3), (200, 50, 1)]


def test_top_entries(tmp_path):
    procs = _tree(tmp_path)
    t = memprocs.Tracker()
    t.update(procs, now=0)
    rows = memprocs.top(procs, t, 2)
    assert [r["pid"] for r in rows] == [300, 102]
    assert set(rows[0]) == {
        "pid",
        "ppid",
        "uid",
        "comm",
        "cmdline",
        "rss_anon_mib",
        "rss_mib",
        "growth_mib_s",
        "start_t",
        "cgroup",
    }
    assert rows[0]["start_t"] == 100 and rows[0]["cgroup"] == "/system.slice"


def test_top_redacts_cmdline(tmp_path):
    add_proc(tmp_path, 9, anon_kib=MIB, cmdline=["svc", "--token=abc123"])
    row = memprocs.top(memprocs.scan(tmp_path), None, 1)[0]
    assert "abc123" not in row["cmdline"] and "--token=" in row["cmdline"]


def test_redact():
    akia = "AKIA" + "Z" * 16
    r = memprocs.redact
    assert "hunter2" not in r("x --token=hunter2") and "--token=" in r("x --token=hunter2")
    assert "s3cr" not in r(["tool", "--api-key", "s3cr3t"])
    assert "--api-key" in r(["tool", "--api-key", "s3cr3t"])
    assert "pw1" not in r("run PASSWORD=pw1 go")
    assert "PASSWORD=" in r("run PASSWORD=pw1 go")
    assert "HOME=" in r("env HOME=/home/x cmd") and "/home/x" not in r("env HOME=/home/x cmd")
    for hdr in ("curl -H 'Authorization: Bearer abcdefgh' u", "curl -H Authorization:Basic zzz1 u"):
        assert "abcdefgh" not in r(hdr) and "zzz1" not in r(hdr)
    assert akia not in r(f"aws {akia}")
    assert "--verbose" in r("x --token --verbose")
    assert len(r("a " * 1000)) == 512


def test_redact_passes_scan_secrets():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "scan_secrets", Path(__file__).parent.parent / "scripts" / "scan-secrets.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scan_secrets"] = mod
    spec.loader.exec_module(mod)
    akia = "AKIA" + "Q" * 16
    raw = (
        f"x --token={'t' * 30} --api-key {'k' * 30} PASSWORD={'p' * 30} "
        f"{akia} secret_key={'s' * 30}"
    )
    out = memprocs.redact(raw)
    assert not any(c in out for c in ("t" * 30, "k" * 30, "p" * 30, "s" * 30, akia))
    hits = [mod._ASSIGNMENT_RE.search(out)] + [
        __import__("re").search(p, out) for p, _ in mod._KNOWN_TOKEN_PATTERNS
    ]
    assert not any(hits)

"""``heat-governor memory`` and ``ship``, plus the report and unit updates."""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import pytest

from heat_governor import ship as shipmod
from heat_governor.cli import main
from heat_governor.cli._commands import governing
from heat_governor.explain import known_paths
from tests.fake_s3 import BUCKET, FAKE_ACCESS_KEY, FAKE_SECRET_KEY, REGION, FakeS3
from tests.fakes_proc import add_proc

CSV = Path(__file__).parent / "data" / "memory-2026-11-14.csv"


def run(capsys, *argv):
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.setenv("HEAT_GOVERNOR_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("HEAT_GOVERNOR_STATE", str(tmp_path / "no-state.json"))


# -- overview / bare noun ---------------------------------------------------------


@pytest.mark.parametrize("noun,needle", [("memory", "memory replay"), ("ship", "ship fetch")])
@pytest.mark.parametrize("json_mode", [False, True])
def test_overview_and_bare_noun(capsys, noun, needle, json_mode):
    extra = ["--json"] if json_mode else []
    for argv in ([noun, "overview"], [noun]):
        rc, out, err = run(capsys, *argv, *extra)
        assert (rc, err) == (0, "")
        assert needle in out
        if json_mode:
            assert json.loads(out)["subject"] == f"heat-governor {noun}"


def test_catalog_learn_overview_list_new_paths(capsys):
    paths = known_paths()
    for p in [
        ("memory",),
        ("memory", "overview"),
        ("memory", "status"),
        ("memory", "top"),
        ("memory", "replay"),
        ("ship",),
        ("ship", "overview"),
        ("ship", "test"),
        ("ship", "fetch"),
    ]:
        assert p in paths
    _, lj, _ = run(capsys, "learn", "--json")
    cmds = [c["path"] for c in json.loads(lj)["commands"]]
    for p in (["memory", "replay"], ["memory", "top"], ["ship", "test"], ["ship", "fetch"]):
        assert p in cmds
    _, lt, _ = run(capsys, "learn")
    assert "memory replay" in lt
    assert "ship fetch" in lt
    _, ov, _ = run(capsys, "overview")
    assert "memory" in ov
    assert "ship" in ov
    rc, out, _ = run(capsys, "explain", "memory", "replay")
    assert rc == 0
    assert "replay" in out
    _, cfg, _ = run(capsys, "explain", "config")
    assert "[memory]" in cfg
    assert "[ship]" in cfg


# -- memory status ----------------------------------------------------------------


def write_state(tmp_path, mem_guard=None):
    doc = {"level": "cool", "admit": True, "at": time.time(), "temp_c": 40.0}
    if mem_guard is not None:
        doc["mem_guard"] = mem_guard
    p = tmp_path / "state.json"
    p.write_text(json.dumps(doc))
    return p


GUARD = {
    "action": "kill",
    "armed": True,
    "avail_mib": 30000,
    "last_kill": {"t": 1794677500.0, "pid": 42, "comm": "python", "rss_mib": 9000, "repeat": 1},
    "kills_last_hour": 1,
}


@pytest.mark.parametrize("json_mode", [False, True])
def test_status_prints_mem_guard(capsys, tmp_path, json_mode):
    src = write_state(tmp_path, GUARD)
    rc, out, err = run(capsys, "memory", "status", "--source", str(src), *_j(json_mode))
    assert (rc, err) == (0, "")
    if json_mode:
        assert json.loads(out) == {"configured": True, "mem_guard": GUARD}
    else:
        assert "kill" in out
        assert "30000" in out
        assert "python" in out
        assert "pid 42" in out


def _j(json_mode):
    return ["--json"] if json_mode else []


@pytest.mark.parametrize("json_mode", [False, True])
def test_status_not_configured(capsys, tmp_path, json_mode):
    src = write_state(tmp_path)
    rc, out, err = run(capsys, "memory", "status", "--source", str(src), *_j(json_mode))
    assert (rc, err) == (0, "")
    if json_mode:
        assert json.loads(out)["configured"] is False
    else:
        assert "memory guard not configured ([memory] absent)" in out


def test_status_no_daemon_is_environment_error(capsys, tmp_path):
    rc, out, err = run(capsys, "memory", "status", "--source", str(tmp_path / "nope.json"))
    assert (rc, out) == (2, "")
    assert "no daemon state" in err
    rc, out, err = run(capsys, "memory", "status", "--source", str(tmp_path / "x"), "--json")
    assert (rc, out) == (2, "")
    assert json.loads(err)["code"] == 2


# -- memory top -------------------------------------------------------------------


@pytest.fixture
def proc_root(tmp_path):
    root = tmp_path / "proc"
    add_proc(root, 10, comm="small", anon_kib=1024 * 10)
    add_proc(root, 11, comm="big", anon_kib=1024 * 3000, cmdline=["big", "--token", "hunter2"])
    add_proc(root, 12, comm="mid", anon_kib=1024 * 500)
    return root


@pytest.mark.parametrize("json_mode", [False, True])
def test_top(capsys, proc_root, json_mode):
    rc, out, err = run(
        capsys, "memory", "top", "--n", "2", "--proc-root", str(proc_root), *_j(json_mode)
    )
    assert (rc, err) == (0, "")
    assert "hunter2" not in out
    if json_mode:
        rows = json.loads(out)
        assert [r["comm"] for r in rows] == ["big", "mid"]
        assert rows[0]["rss_anon_mib"] == 3000
    else:
        assert out.index("big") < out.index("mid")
        assert "small" not in out


def test_top_bad_n_and_missing_root(capsys, tmp_path, proc_root):
    rc, out, err = run(capsys, "memory", "top", "--n", "0", "--proc-root", str(proc_root))
    assert (rc, out) == (1, "")
    rc, out, err = run(capsys, "memory", "top", "--proc-root", str(tmp_path / "gone"))
    assert (rc, out) == (2, "")
    assert "cannot read" in err


# -- memory replay ----------------------------------------------------------------


@pytest.mark.parametrize("json_mode", [False, True])
def test_replay_csv_lists_five_fires(capsys, json_mode):
    rc, out, err = run(capsys, "memory", "replay", str(CSV), *_j(json_mode))
    assert (rc, err) == (0, "")
    if json_mode:
        doc = json.loads(out)
        assert doc["total_mib"] == 45939
        assert doc["total_source"] == "file header"
        assert len(doc["fires"]) == 5
        assert {"t", "utc", "local", "reason", "avail_mib", "rate_mib_s"} <= set(doc["fires"][0])
    else:
        assert "5 fires" in out
        assert "MemTotal 45939" in out


def test_replay_params_change_result(capsys):
    rc, out, _ = run(capsys, "memory", "replay", str(CSV), "--floor-pct", "0.1", "--json")
    assert rc == 0
    assert json.loads(out)["params"]["floor_pct"] == 0.1
    rc, out, err = run(capsys, "memory", "replay", str(CSV), "--floor-pct", "-1")
    assert (rc, out) == (1, "")


def test_replay_records_jsonl(capsys, tmp_path):
    f = tmp_path / "records.jsonl"
    lines = [json.dumps({"t": 1.0, "event": "start"}), "not json"]
    for i in range(10):
        lines.append(json.dumps({"t": 100.0 + i, "system": {"mem:avail_mib": 100}}))
    lines.append(json.dumps({"t": 200.0, "temp_c": 40.0, "system": {"mem:used_pct": 5}}))
    f.write_text("\n".join(lines) + "\n")
    rc, out, err = run(capsys, "memory", "replay", str(f), "--total-mib", "10000", "--json")
    assert (rc, err) == (0, "")
    doc = json.loads(out)
    assert doc["total_source"] == "--total-mib"
    assert len(doc["fires"]) == 1
    assert doc["fires"][0]["reason"] == "floor"


def test_replay_jsonl_total_from_meminfo(capsys, tmp_path, monkeypatch):
    f = tmp_path / "r.jsonl"
    f.write_text(json.dumps({"t": 1.0, "system": {"mem:avail_mib": 5}}) + "\n")
    monkeypatch.setattr("heat_governor.cli._commands.memory._local_total_mib", lambda: 8000.0)
    rc, out, _ = run(capsys, "memory", "replay", str(f), "--json")
    assert rc == 0
    assert json.loads(out)["total_source"] == "/proc/meminfo"


def test_replay_errors(capsys, tmp_path):
    rc, out, err = run(capsys, "memory", "replay", str(tmp_path / "missing.csv"))
    assert (rc, out) == (1, "")
    assert "missing.csv" in err
    empty = tmp_path / "e.jsonl"
    empty.write_text("{}\n")
    rc, _, err = run(capsys, "memory", "replay", str(empty), "--total-mib", "100")
    assert rc == 1
    assert "mem:avail_mib" in err
    rc, _, _ = run(capsys, "memory", "replay", str(empty), "--total-mib", "0")
    assert rc == 1


# -- ship -------------------------------------------------------------------------


@pytest.fixture
def s3():
    with FakeS3() as fake:
        yield fake


@pytest.fixture
def ship_env(s3, tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[ship]\nendpoint = "{s3.endpoint}"\nbucket = "{BUCKET}"\nregion = "{REGION}"\n'
        'prefix = "fleet"\nmachine = "spark-a"\n'
    )
    monkeypatch.setenv("HEAT_GOVERNOR_CONFIG", str(cfg))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", FAKE_SECRET_KEY)
    return s3


@pytest.mark.parametrize("json_mode", [False, True])
def test_ship_test_ok(capsys, ship_env, json_mode):
    rc, out, err = run(capsys, "ship", "test", *_j(json_mode))
    assert (rc, err) == (0, "")
    assert FAKE_SECRET_KEY not in out
    if json_mode:
        doc = json.loads(out)
        assert doc["ok"] is True
        assert doc["machine"] == "spark-a"
        assert doc["key"].startswith("fleet/spark-a/_probe/")
        assert doc["latency_ms"] >= 0
    else:
        assert "ok" in out
        assert "ms" in out


def test_ship_test_machine_from_fleet_local(capsys, ship_env, tmp_path, monkeypatch):
    cfg = Path(tmp_path / "config.toml")
    cfg.write_text(cfg.read_text().replace('machine = "spark-a"\n', ""))
    fleet = tmp_path / "fleet.toml"
    fleet.write_text('[[machines]]\nname = "box9"\ntransport = "local"\n')
    monkeypatch.setattr("heat_governor.fleet.default_fleet_path", lambda environ=None: fleet)
    rc, out, err = run(capsys, "ship", "test", "--json")
    assert (rc, err) == (0, "")
    assert json.loads(out)["machine"] == "box9"


def test_ship_test_no_machine_is_user_error(capsys, ship_env, tmp_path, monkeypatch):
    cfg = Path(tmp_path / "config.toml")
    cfg.write_text(cfg.read_text().replace('machine = "spark-a"\n', ""))
    monkeypatch.setattr(
        "heat_governor.fleet.default_fleet_path", lambda environ=None: tmp_path / "none.toml"
    )
    rc, out, err = run(capsys, "ship", "test")
    assert (rc, out) == (1, "")
    assert "machine" in err


def test_ship_test_missing_config_and_credentials(capsys, tmp_path, monkeypatch, s3):
    rc, out, err = run(capsys, "ship", "test")  # no config at all
    assert (rc, out) == (1, "")
    assert "[ship]" in err
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[ship]\nendpoint = "{s3.endpoint}"\nbucket = "{BUCKET}"\nmachine = "m"\n')
    monkeypatch.setenv("HEAT_GOVERNOR_CONFIG", str(cfg))
    rc, out, err = run(capsys, "ship", "test", "--json")
    assert (rc, out) == (1, "")
    assert "credentials" in json.loads(err)["message"]


def test_ship_test_s3_failure_is_env_error(capsys, ship_env):
    ship_env.always = (403, "AccessDenied")
    rc, out, err = run(capsys, "ship", "test")
    assert (rc, out) == (2, "")
    assert "403" in err
    assert "AccessDenied" in err
    assert FAKE_SECRET_KEY not in err
    assert FAKE_ACCESS_KEY not in err


def seed(s3, machine="spark-a", boot="boot-1", t=1794677400.0):
    c = shipmod.S3Client(
        s3.endpoint, REGION, BUCKET, shipmod.Credentials(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    )
    ev = {"event": "memory_kill", "t": t, "pid": 42, "comm": "python", "rss_mib": 9000}
    c.put(shipmod.event_key("fleet", machine, boot, "memory_kill", t), json.dumps(ev).encode())
    c.put(
        shipmod.postmortem_key("fleet", machine, boot),
        json.dumps({"reason": "oom", "t": t}).encode(),
    )
    rows = "".join(json.dumps({"t": t + i, "temp_c": 40}) + "\n" for i in range(3))
    c.put(shipmod.blackbox_key("fleet", machine, boot), gzip.compress(rows.encode()))


@pytest.mark.parametrize("json_mode", [False, True])
def test_ship_fetch(capsys, ship_env, tmp_path, json_mode):
    seed(ship_env)
    seed(ship_env, boot="boot-old", t=1000.0)
    out_dir = tmp_path / "dl"
    rc, out, err = run(
        capsys, "ship", "fetch", "--machine", "spark-a", "--out", str(out_dir), *_j(json_mode)
    )
    assert (rc, err) == (0, "")
    assert (out_dir / "postmortem.json").exists()
    assert len(list((out_dir / "events").glob("*.json"))) == 1
    assert (out_dir / "blackbox.jsonl.gz").exists()
    if json_mode:
        doc = json.loads(out)
        assert doc["boot"] == "boot-1"
        assert doc["blackbox"]["rows"] == 3
        assert doc["events"][0]["kind"] == "memory_kill"
        assert doc["events"][0]["pid"] == 42
        assert doc["postmortem"]["reason"] == "oom"
    else:
        assert "boot-1" in out
        assert "memory_kill" in out
        assert "python" in out
        assert "3 rows" in out


def test_ship_fetch_explicit_boot_and_default_dir(capsys, ship_env, tmp_path, monkeypatch):
    seed(ship_env)
    seed(ship_env, boot="boot-old", t=1000.0)
    monkeypatch.chdir(tmp_path)
    rc, out, err = run(
        capsys, "ship", "fetch", "--machine", "spark-a", "--boot", "boot-old", "--json"
    )
    assert (rc, err) == (0, "")
    assert json.loads(out)["boot"] == "boot-old"
    assert (tmp_path / "heat-governor-fetch" / "spark-a" / "boot-old" / "postmortem.json").exists()


def test_ship_fetch_nothing_found_and_missing_machine(capsys, ship_env, tmp_path):
    rc, out, err = run(capsys, "ship", "fetch", "--machine", "ghost", "--out", str(tmp_path / "d"))
    assert (rc, out) == (1, "")
    assert "ghost" in err
    rc, out, err = run(capsys, "ship", "fetch")
    assert rc == 1
    assert out == ""


def test_ship_fetch_partial_boot_still_works(capsys, ship_env, tmp_path):
    c = shipmod.S3Client(
        ship_env.endpoint, REGION, BUCKET, shipmod.Credentials(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    )
    c.put(shipmod.postmortem_key("fleet", "m1", "b"), b'{"reason": "x"}')
    rc, out, err = run(
        capsys, "ship", "fetch", "--machine", "m1", "--out", str(tmp_path / "o"), "--json"
    )
    assert (rc, err) == (0, "")
    doc = json.loads(out)
    assert doc["events"] == []
    assert doc["blackbox"] is None


# -- report and unit --------------------------------------------------------------


def test_report_prints_mem_block(capsys, tmp_path, monkeypatch):
    entry = {
        "last_record_utc": "2026-11-14T10:00:00Z",
        "restart_utc": "2026-11-14T10:05:00Z",
        "gap_s": 300,
        "max_temp_c": 50.0,
        "window_s": 60.0,
        "last": {"level": "cool", "duty": 1.0},
        "mem": {"min_avail_mib": 0, "max_swap_used_mib": 4096, "peak_psi_memory_full": 98.0},
        "events": [{"event": "memory_kill"}, {"event": "memory_kill"}, {"event": "oom_kill"}],
    }
    monkeypatch.setattr(governing.recorder, "report", lambda *a, **k: [entry])
    rc, out, err = run(capsys, "report", "--data-dir", str(tmp_path))
    assert (rc, err) == (0, "")
    assert "min avail 0 MiB" in out
    assert "swap 4096 MiB" in out
    assert "peak PSI full 98.0" in out
    assert "2 memory_kill" in out
    assert "1 oom_kill" in out
    rc, out, _ = run(capsys, "report", "--data-dir", str(tmp_path), "--json")
    assert json.loads(out)["reboots"][0]["mem"]["min_avail_mib"] == 0


def test_report_without_mem_data_is_unchanged(capsys, tmp_path, monkeypatch):
    entry = {
        "last_record_utc": "a",
        "restart_utc": "b",
        "gap_s": 1,
        "max_temp_c": 5.0,
        "window_s": 60.0,
        "last": {"level": "cool", "duty": 1.0},
        "mem": {"min_avail_mib": None, "max_swap_used_mib": None, "peak_psi_memory_full": None},
        "events": [],
    }
    monkeypatch.setattr(governing.recorder, "report", lambda *a, **k: [entry])
    _, out, _ = run(capsys, "report", "--data-dir", str(tmp_path))
    assert "memory:" not in out


def test_unit_has_memory_min(capsys):
    _, out, _ = run(capsys, "unit")
    assert "MemoryMin=128M" in out
    _, out, _ = run(capsys, "unit", "--json")
    assert "MemoryMin=128M" in json.loads(out)["unit"]

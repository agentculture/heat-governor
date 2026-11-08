"""The tensor-parallel pair: config, the shared decision, the peer link, and the
daemon governing a rank by the hotter box (no real network but localhost)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from heat_governor import client
from heat_governor.config import Config, PairRule, template, validate
from heat_governor.daemon import Daemon, Options
from heat_governor.pair import (
    PeerLink,
    PeerReading,
    PeerStatus,
    decide,
    fetch_state,
    one_band_hotter,
    one_level_hotter,
    tighter,
)
from heat_governor.platforms import SPARK
from heat_governor.sense import Sensor
from heat_governor.state import StateServer, read_state
from tests.fakes import SPARK_ZONES, FakeDocker, container, make_sys, set_temp

PAIR = {"peer": "10.77.77.2:9476", "containers": ["sparkrun_*"]}
BANDS = [4, 8, 12]
CONC = SPARK.concurrency


# -- config -----------------------------------------------------------------------


def test_pair_config_valid_and_parsed() -> None:
    data = {"pair": {**PAIR, "listen": "10.77.77.1:9476", "stale_after_s": 8, "poll_s": 0.5}}
    assert validate(data) == []
    rule = Config(data=data).pair
    assert rule == PairRule("10.77.77.2:9476", ("sparkrun_*",), "10.77.77.1:9476", 8.0, 0.5)
    assert rule.matches("sparkrun_qwen_rank0")
    assert not rule.matches("model-gear-vllm-primary")
    assert Config().pair is None


@pytest.mark.parametrize(
    ("pair", "needle"),
    [
        ({"containers": ["x"]}, "pair.peer is required"),
        ({"peer": "10.77.77.2"}, "pair.peer must be HOST:PORT"),
        ({"peer": "h:99999", "containers": ["x"]}, "pair.peer must be HOST:PORT"),
        ({"peer": "h:1"}, "pair.containers is required"),
        ({"peer": "h:1", "containers": []}, "non-empty list"),
        ({"peer": "h:1", "containers": [""]}, "non-empty list"),
        ({"peer": "h:1", "containers": "x"}, "pair.containers has the wrong type"),
        ({**PAIR, "listen": "nope"}, "pair.listen must be HOST:PORT"),
        ({**PAIR, "poll_s": 0.05}, "pair.poll_s must be between"),
        ({**PAIR, "stale_after_s": 1}, "pair.stale_after_s must be at least"),
        ({**PAIR, "cpu_cap": "yes"}, "pair.cpu_cap has the wrong type"),
        ({**PAIR, "poll_s": True}, "pair.poll_s has the wrong type"),
        ({**PAIR, "peers": "x"}, "unknown key pair.'peers'"),
    ],
)
def test_pair_config_rejects(pair: dict, needle: str) -> None:
    problems = validate({"pair": pair})
    assert any(needle in p for p in problems), problems


def test_pair_must_be_a_table() -> None:
    assert "[pair] must be a table" in validate({"pair": "x"})


def test_pair_container_configured_as_batch_is_rejected() -> None:
    data = {
        "pair": PAIR,
        "containers": [
            {"name": "sparkrun_qwen", "role": "batch"},
            {"name": "sparkrun_other", "role": "serving", "cpu_cap": True},
            {"name": "job", "role": "batch"},
        ],
    }
    problems = validate(data)
    assert problems == [
        "containers[0]: 'sparkrun_qwen' is a pair container and must not be batch "
        "(pausing one rank hangs the other)"
    ]


def test_template_documents_pair_and_still_parses() -> None:
    text = template(SPARK)
    assert "# [pair]" in text
    assert validate(tomllib.loads(text)) == []


# -- the shared decision (pure) ------------------------------------------------------


def _reading(level: str, cpus: int | None, **kw) -> PeerReading:
    base = dict(
        host="spark2",
        level=level,
        cpus=cpus,
        effective_c=90.0,
        temp_c=89.0,
        at=1.0,
        seen=1.0,
        gpu_operator_mhz=2200,
        gpu_ceiling_mhz=2200,
        has_gpu_operator=True,
    )
    return PeerReading(**{**base, **kw})


def _ok(level: str, cpus: int | None) -> PeerStatus:
    return PeerStatus("ok", _reading(level, cpus), 0.5, None)


def test_pair_takes_the_hotter_box() -> None:
    peer_hot = decide("cool", None, _ok("hot", 4), cpu_bands=BANDS, concurrency=CONC)
    assert (peer_hot.level, peer_hot.cpus, peer_hot.admit, peer_hot.basis) == (
        "hot",
        4,
        False,
        "peer",
    )
    local_hot = decide("warm", 8, _ok("cool", None), cpu_bands=BANDS, concurrency=CONC)
    assert (local_hot.level, local_hot.cpus, local_hot.basis) == ("warm", 8, "local")
    assert local_hot.concurrency_factor == 0.5
    same = decide("warm", 8, _ok("warm", 8), cpu_bands=BANDS, concurrency=CONC)
    assert same.basis == "equal"


def test_both_daemons_pick_the_same_band() -> None:
    a = decide("warm", 8, _ok("hot", 4), cpu_bands=BANDS, concurrency=CONC)
    b = decide("hot", 4, _ok("warm", 8), cpu_bands=BANDS, concurrency=CONC)
    assert (a.level, a.cpus, a.admit) == (b.level, b.cpus, b.admit) == ("hot", 4, False)


@pytest.mark.parametrize("status", ["stale", "unreachable"])
def test_lost_peer_is_one_band_hotter_than_local(status: str) -> None:
    lost = PeerStatus(status, _reading("cool", None), 30.0, "timed out")
    d = decide("cool", None, lost, cpu_bands=BANDS, concurrency=CONC)
    assert (d.level, d.cpus, d.admit, d.basis) == ("warm", 12, True, "peer_lost")
    d = decide("warm", 8, lost, cpu_bands=BANDS, concurrency=CONC)
    assert (d.level, d.cpus, d.admit) == ("hot", 4, False)
    d = decide("critical", 4, lost, cpu_bands=BANDS, concurrency=CONC)
    assert (d.level, d.cpus) == ("critical", 4)


def test_band_helpers() -> None:
    assert tighter(None, 8) == 8
    assert tighter(8, None) == 8
    assert tighter(4, 8) == 4
    assert tighter(None, None) is None
    assert one_band_hotter(12, BANDS) == 8
    assert one_band_hotter(10, BANDS) == 8  # between bands: the next one down
    assert one_band_hotter(2, BANDS) == 2  # tighter than every band: stays
    assert one_band_hotter(None, []) is None
    assert one_band_hotter(6, []) == 6
    assert one_level_hotter("critical") == "critical"


# -- the peer link ----------------------------------------------------------------


class FakePeer:
    """The peer daemon's /state: ``doc`` is served, ``error`` raised when set."""

    def __init__(self, doc: dict | None = None) -> None:
        self.doc = doc or peer_doc("cool", None)
        self.error: Exception | None = None
        self.calls: list[tuple[str, float]] = []

    def __call__(self, peer: str, timeout: float) -> dict:
        self.calls.append((peer, timeout))
        if self.error:
            raise self.error
        return dict(self.doc)


def peer_doc(level: str, cpus: int | None, *, at: float = 1.0, **kw) -> dict:
    return {
        "schema": 1,
        "host": "spark2",
        "level": level,
        "cpus": cpus,
        "at": at,
        "effective_c": 85.0,
        "gpu_operator_mhz": 2200,
        **kw,
    }


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_peer_link_ok_stale_unreachable_and_recovered() -> None:
    clk, peer = Clock(), FakePeer()
    link = PeerLink(PairRule("p:1", ("x",), stale_after_s=5.0, poll_s=1.0), fetch=peer, clock=clk)
    assert link.status(clk()).status == "unreachable"  # never read yet
    link.poll_once()
    assert link.status(clk()).ok
    assert peer.calls == [("p:1", 1.0)]
    clk.t += 6  # the peer still answers, but its document stopped advancing
    link.poll_once()
    st = link.status(clk())
    assert (st.status, st.age_s) == ("stale", 6.0)
    peer.error = OSError("connection refused")
    link.poll_once()
    st = link.status(clk())
    assert (st.status, st.error) == ("unreachable", "connection refused")
    assert st.reading is not None  # the last good reading is kept
    peer.error, peer.doc = None, peer_doc("warm", 8, at=2.0)
    link.poll_once()
    st = link.status(clk())
    assert st.ok
    assert st.reading.level == "warm"
    assert st.age_s == 0.0


def test_peer_link_short_failure_inside_the_window_is_still_ok() -> None:
    clk, peer = Clock(), FakePeer()
    link = PeerLink(PairRule("p:1", ("x",), stale_after_s=5.0), fetch=peer, clock=clk)
    link.poll_once()
    peer.error = OSError("timed out")
    clk.t += 2
    link.poll_once()
    assert link.status(clk()).ok  # one missed poll is not a lost peer


def test_peer_link_rejects_a_document_that_is_no_decision() -> None:
    clk = Clock()
    for doc in ({"schema": 1, "level": "unknown"}, {"level": "cool"}, peer_doc("cool", 0)):
        link = PeerLink(PairRule("p:1", ("x",)), fetch=FakePeer(doc), clock=clk)
        link.poll_once()
        assert link.status(clk()).status == "unreachable"


def test_peer_link_new_peer_forgets_the_old_one() -> None:
    clk = Clock()
    link = PeerLink(PairRule("p:1", ("x",)), fetch=FakePeer(), clock=clk)
    link.poll_once()
    link.configure(PairRule("p:1", ("y",), stale_after_s=20.0))
    assert link.status(clk()).ok  # same peer: keeps the reading
    link.configure(PairRule("q:2", ("y",)))
    assert link.status(clk()).status == "unreachable"


def test_fetch_state_from_a_real_state_server_on_localhost() -> None:
    srv = StateServer("127.0.0.1", 0)
    srv.doc = peer_doc("hot", 4)
    srv.start()
    try:
        host, port = srv.address[:2]
        assert fetch_state(f"{host}:{port}", 2.0)["level"] == "hot"
        link = PeerLink(PairRule(f"{host}:{port}", ("x",), poll_s=0.2, stale_after_s=5.0))
        link.start()
        link.start()  # idempotent
        assert link.running
        link.stop()
        assert not link.running
        assert link.status(link.clock()).ok
    finally:
        srv.stop()
    with pytest.raises(OSError):
        fetch_state(f"{host}:{port}", 0.5)  # nothing listens any more


# -- the daemon -------------------------------------------------------------------


def _pair_daemon(tmp_path: Path, dk: FakeDocker, peer: FakePeer, **pair_kw):
    sysr = make_sys(tmp_path, SPARK_ZONES)
    clk = Clock()
    logs: list[str] = []
    rule = PairRule(PAIR["peer"], ("sparkrun_*",), **pair_kw)
    opts = Options(
        apply=True,
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        log=logs.append,
        pair=rule,
        record=False,
    )
    d = Daemon(
        SPARK,
        opts,
        sensor=Sensor(SPARK.zones, sys_root=sysr),
        docker=dk,
        clock=clk,
        peer_link=PeerLink(rule, fetch=peer, clock=clk),
    )
    return d, sysr, clk, logs


def _docker() -> FakeDocker:
    return FakeDocker(
        container("r0", "sparkrun_qwen_rank0", "batch", nano=0),  # labelled batch: overridden
        container("b1", "job", "batch", nano=0),
        container("s1", "lane", "serving", cap=True, nano=0),
    )


def test_pair_container_capped_by_the_hotter_peer_and_never_paused(tmp_path: Path) -> None:
    dk, peer = _docker(), FakePeer(peer_doc("hot", 4))
    d, sysr, clk, logs = _pair_daemon(tmp_path, dk, peer)
    d.discover()
    assert d.pair_ids == {"r0"}
    assert d.containers["r0"].role == "serving"
    assert set(d.batch()) == {"b1"}
    assert any("sparkrun_qwen_rank0 is a pair container" in m for m in logs)
    d.tick()  # local cool (83.8 C), peer hot
    assert dk.c["r0"].nano_cpus == 4_000_000_000  # the pair's band: the peer's
    assert dk.c["s1"].nano_cpus == 0  # non-pair serving: local band (no cap when cool)
    assert dk.c["b1"].nano_cpus == 0
    st = read_state(tmp_path / "state.json")
    assert st["level"] == "cool"
    assert st["admit"] is True  # the host signal stays local
    pair = st["pair"]
    assert (pair["level"], pair["admit"], pair["cpus"], pair["basis"]) == ("hot", False, 4, "peer")
    assert (pair["peer_status"], pair["peer_host"], pair["gpu_operator_match"]) == (
        "ok",
        "spark2",
        False,  # here: no operator ceiling; the peer: 2200 MHz
    )
    # full duty on everything batch: the rank is never paused
    set_temp(sysr, 0, 99000)
    clk.t += 1
    d.tick()
    d.cycler.cycle()
    assert ("pause", "r0") not in dk.calls
    assert ("pause", "b1") in dk.calls
    d.cycler.stop()


def test_pair_published_rank_is_marked_and_record_carries_pair(tmp_path: Path) -> None:
    dk, peer = _docker(), FakePeer(peer_doc("warm", 8))
    d, _, clk, _ = _pair_daemon(tmp_path, dk, peer)
    d.discover()
    d.tick()
    clk.t += 1
    d.tick()
    st = read_state(tmp_path / "state.json")
    assert {c["name"]: c["pair"] for c in st["containers"]} == {
        "sparkrun_qwen_rank0": True,
        "job": False,
        "lane": False,
    }
    assert st["pair"]["containers"] == ["sparkrun_qwen_rank0"]
    assert d._pair_record() == {"pair_level": "warm", "pair_cpus": 8, "peer_status": "ok"}


def test_local_hotter_than_peer_drives_the_rank(tmp_path: Path) -> None:
    dk, peer = _docker(), FakePeer(peer_doc("cool", None))
    d, sysr, _, _ = _pair_daemon(tmp_path, dk, peer)
    set_temp(sysr, 0, 89000)  # local warm: band 8
    d.discover()
    d.tick()
    assert dk.c["r0"].nano_cpus == 8_000_000_000
    assert dk.c["s1"].nano_cpus == 8_000_000_000  # non-pair: the same local band
    assert read_state(tmp_path / "state.json")["pair"]["basis"] == "local"


def test_stale_peer_one_band_hotter_logged_once_then_recovered(tmp_path: Path) -> None:
    dk, peer = _docker(), FakePeer(peer_doc("cool", None))
    d, _, clk, logs = _pair_daemon(tmp_path, dk, peer, stale_after_s=5.0)
    d.discover()
    d.tick()
    assert dk.c["r0"].nano_cpus == 0  # both cool: no cap
    peer.error = OSError("timed out")
    for _ in range(8):
        clk.t += 1
        d.tick()
    lost = [m for m in logs if "held one band hotter" in m]
    assert len(lost) == 1
    assert "unreachable (timed out)" in lost[0]
    assert dk.c["r0"].nano_cpus == 12_000_000_000  # one band hotter than local (none)
    assert dk.c["s1"].nano_cpus == 0  # non-pair containers: untouched
    st = read_state(tmp_path / "state.json")
    assert st["admit"] is True  # the host's own signal is unaffected
    assert (st["pair"]["peer_status"], st["pair"]["level"], st["pair"]["peer_error"]) == (
        "unreachable",
        "warm",
        "timed out",
    )
    peer.error, peer.doc = None, peer_doc("cool", None, at=50.0)
    clk.t += 1
    d.tick()
    assert sum("reached -- sharing the hotter band" in m for m in logs) == 2  # start + back
    clk.t += 1
    d.tick()
    assert read_state(tmp_path / "state.json")["pair"]["peer_status"] == "ok"
    d.capper.release_all(d.containers)


def test_gpu_ceiling_mismatch_warned_once_per_change(tmp_path: Path) -> None:
    dk, peer = _docker(), FakePeer(peer_doc("cool", None, gpu_operator_mhz=1800))
    d, _, clk, logs = _pair_daemon(tmp_path, dk, peer)
    d.operator.config_mhz = 2200
    for _ in range(3):
        clk.t += 1
        peer.doc = {**peer.doc, "at": clk.t}
        d.tick()
    warn = [m for m in logs if "operator GPU ceiling differs" in m]
    assert len(warn) == 1
    assert "here 2200 MHz, peer spark2 1800 MHz" in warn[0]
    peer.doc = {**peer.doc, "gpu_operator_mhz": 2200}
    clk.t += 1
    d.tick()
    assert any("operator GPU ceilings agree again (2200 MHz)" in m for m in logs)
    assert read_state(tmp_path / "state.json")["pair"]["gpu_operator_match"] is True
    assert d.gpuclock is None or ("lock", 1800) not in getattr(d.gpuclock, "calls", [])


def test_old_peer_without_operator_field_is_not_compared(tmp_path: Path) -> None:
    doc = peer_doc("cool", None)
    del doc["gpu_operator_mhz"]
    d, _, _, logs = _pair_daemon(tmp_path, _docker(), FakePeer(doc))
    d.operator.config_mhz = 2200
    d.tick()
    assert not any(m.startswith("pair:") and "ceiling" in m for m in logs)
    assert read_state(tmp_path / "state.json")["pair"]["gpu_operator_match"] is None


def test_no_pair_leaves_everything_local(tmp_path: Path) -> None:
    sysr = make_sys(tmp_path, SPARK_ZONES)
    dk = _docker()
    opts = Options(
        apply=True, state_path=tmp_path / "s.json", data_dir=tmp_path / "d", log=[].append
    )
    d = Daemon(SPARK, opts, sensor=Sensor(SPARK.zones, sys_root=sysr), docker=dk)
    d.discover()
    d.tick()
    st = read_state(tmp_path / "s.json")
    assert st["pair"] is None
    assert d.pair_link is None
    assert d.containers["r0"].role == "batch"  # no pair: its label rules


def test_reload_adds_changes_and_drops_the_pair(tmp_path: Path) -> None:
    dk, peer = _docker(), FakePeer(peer_doc("hot", 4))
    d, _, clk, logs = _pair_daemon(tmp_path, dk, peer)
    d.set_pair(PairRule(PAIR["peer"], ("sparkrun_*",), stale_after_s=20.0))
    assert d.pair_link.rule.stale_after_s == 20.0
    d.set_pair(None)
    assert d.pair_link is None
    assert d.opts.pair is None
    d.discover()
    d.tick()
    assert read_state(tmp_path / "state.json")["pair"] is None
    assert d.containers["r0"].role == "batch"
    assert any("pair: off" in m for m in logs)
    d.set_pair(PairRule(PAIR["peer"], ("sparkrun_*",)))
    assert d.pair_link is not None
    assert not d.pair_link.running


def test_reload_flags_pair_listen_as_restart_only(tmp_path: Path) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[pair]\npeer = "h:1"\ncontainers = ["x"]\nlisten = "127.0.0.1:{free}"\n')
    from heat_governor.config import load

    cfg = load(cfg_path)
    dk = FakeDocker()
    sysr = make_sys(tmp_path, SPARK_ZONES)
    logs: list[str] = []
    opts = Options(
        state_path=tmp_path / "s.json",
        data_dir=tmp_path / "d",
        log=logs.append,
        config=cfg,
        pair=cfg.pair,
        reload_every_s=0.0,
        record=False,
    )
    d = Daemon(
        SPARK,
        opts,
        sensor=Sensor(SPARK.zones, sys_root=sysr),
        docker=dk,
        peer_link=PeerLink(cfg.pair, fetch=FakePeer()),
    )
    assert d.pair_server is not None  # the extra listen address
    cfg_path.write_text('[pair]\npeer = "h:1"\ncontainers = ["x"]\nlisten = "127.0.0.1:2"\n')
    cfg.mtime = -1.0
    d.maybe_reload(10.0)
    assert any("restart" in m and "pair.listen" in m for m in logs)
    d.pair_server.httpd.server_close()


def test_pair_listen_same_as_listen_binds_once() -> None:
    rule = PairRule("h:1", ("x",), listen="127.0.0.1:9")
    d = Daemon.__new__(Daemon)  # only the address check, no sockets
    d.opts = Options(listen=("127.0.0.1", 9), pair=rule)
    assert d._pair_server(rule) is None
    assert d._pair_server(None) is None


# -- the client --------------------------------------------------------------------


def test_client_reads_the_pair_signal(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    import json
    import time

    doc = {"at": time.time(), "level": "cool", "admit": True, "concurrency_factor": 1.0}
    path.write_text(json.dumps({**doc, "pair": None}))
    adm = client.read(str(path), pair=True)
    assert (adm.level, adm.admit, adm.reason) == ("cool", True, "no-pair")
    pair = {"level": "hot", "admit": False, "concurrency_factor": 0.0, "basis": "peer"}
    path.write_text(json.dumps({**doc, "pair": pair}))
    assert client.read(str(path)).admit is True  # the host signal by default
    adm = client.read(str(path), pair=True)
    assert (adm.level, adm.admit, adm.reason) == ("hot", False, "pair:peer")
    with pytest.raises(TimeoutError):
        client.wait(str(path), timeout=0.0, poll_s=0.0, pair=True)
    assert client.Gate(2, str(path), pair=True)._allowed() == 0


# -- the CLI ---------------------------------------------------------------------


def _cli(capsys, *argv):
    from heat_governor.cli import main

    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_state_pair(tmp_path: Path, capsys) -> None:
    import json
    import time

    from heat_governor.state import write_state

    p = tmp_path / "s.json"
    pair = {
        "peer": "10.77.77.2:9476",
        "peer_status": "unreachable",
        "peer_error": "timed out",
        "level": "warm",
        "admit": True,
        "concurrency_factor": 0.5,
        "cpus": 12,
        "basis": "peer_lost",
        "gpu_operator_match": None,
    }
    base = {"at": time.time(), "level": "cool", "admit": True, "concurrency_factor": 1.0}
    write_state(p, {**base, "pair": pair})
    rc, out, _ = _cli(capsys, "state", "--source", str(p), "--pair", "--json")
    assert rc == 0
    assert json.loads(out)["level"] == "warm"
    rc, out, _ = _cli(capsys, "state", "--source", str(p))
    assert rc == 0
    assert "pair: level=warm admit=True cpus=12 basis=peer_lost" in out
    assert "unreachable (timed out)" in out
    rc, out, _ = _cli(capsys, "wait", "--source", str(p), "--pair", "--timeout", "0", "--json")
    assert rc == 0
    assert json.loads(out)["level"] == "warm"


def _pair_config(tmp_path: Path, peer: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'platform = "spark"\n[pair]\npeer = "{peer}"\ncontainers = ["sparkrun_*"]\n')
    return cfg


def test_cli_run_once_with_pair_reads_the_peer(tmp_path: Path, capsys, monkeypatch) -> None:
    import json

    from heat_governor.cli._commands import governing

    srv = StateServer("127.0.0.1", 0)
    srv.doc = peer_doc("hot", 4, at=__import__("time").time())
    srv.start()
    host, port = srv.address[:2]
    dk = FakeDocker(container("r0", "sparkrun_qwen", None))
    monkeypatch.setattr(governing, "_docker", lambda args, cfg: dk)
    cfg = _pair_config(tmp_path, f"{host}:{port}")
    try:
        common = ["--config", str(cfg), "--state", str(tmp_path / "s.json")]
        common += ["--data-dir", str(tmp_path / "d")]
        rc, out, _ = _cli(capsys, "run", "--once", "--json", *common)
        assert rc == 0
        doc = json.loads(out)
        assert doc["pair"]["peer_status"] == "ok"
        assert (doc["pair"]["level"], doc["pair"]["cpus"]) == ("hot", 4)
        assert doc["pair"]["containers"] == ["sparkrun_qwen"]
        rc, out, _ = _cli(capsys, "run", "--once", *common)
        # The local level comes from this host's real sensors (a CI runner has none and
        # reads hot), so the basis is "peer" or "equal" depending on where the test runs.
        assert re.search(r"pair: level=hot admit=False cpus=4 basis=(peer|equal)\b", out), out
        assert "containers: sparkrun_qwen" in out
    finally:
        srv.stop()
    assert dk.calls == []  # --once never acts


def test_cli_pair_without_docker_is_env_error(tmp_path: Path, capsys) -> None:
    cfg = _pair_config(tmp_path, "10.77.77.2:9476")
    rc, _, err = _cli(
        capsys, "run", "--once", "--no-docker", "--config", str(cfg), "--data-dir", str(tmp_path)
    )
    assert rc == 2
    assert "hint:" in err


def test_cli_config_show_and_overview_carry_pair(tmp_path: Path, capsys) -> None:
    import json

    cfg = _pair_config(tmp_path, "10.77.77.2:9476")
    rc, out, _ = _cli(capsys, "config", "show", "--config", str(cfg), "--json")
    assert rc == 0
    assert json.loads(out)["pair"]["peer"] == "10.77.77.2:9476"
    rc, out, _ = _cli(capsys, "config", "overview", "--config", str(cfg))
    assert rc == 0
    assert "pair peer 10.77.77.2:9476" in out


# -- review fixes (PR #6) -----------------------------------------------------------


class InspectCounting(FakeDocker):
    """A FakeDocker that counts inspects and can fail them (a transient daemon error)."""

    def __init__(self, *containers, fail_inspect: bool = False) -> None:
        super().__init__(*containers)
        self.inspects: list[str] = []
        self.fail_inspect = fail_inspect

    def inspect(self, ref: str):
        from heat_governor.docker import DockerError

        self.inspects.append(ref)
        if self.fail_inspect:
            raise DockerError("transient")
        return super().inspect(ref)


@pytest.mark.parametrize(
    "error",
    [
        __import__("http.client").client.IncompleteRead(b"x"),
        __import__("http.client").client.BadStatusLine("x"),
        RuntimeError("anything"),
    ],
)
def test_1_poll_once_never_raises(error: Exception) -> None:
    clk, peer = Clock(), FakePeer()
    link = PeerLink(PairRule("p:1", ("x",)), fetch=peer, clock=clk)
    peer.error = error
    link.poll_once()  # must not raise
    assert link.status(clk()).status == "unreachable"


def test_1_non_finite_peer_values_are_a_failed_read() -> None:
    clk = Clock()
    for doc in (peer_doc("cool", float("inf")), peer_doc("cool", None, at=float("nan"))):
        link = PeerLink(PairRule("p:1", ("x",)), fetch=FakePeer(doc), clock=clk)
        link.poll_once()
        assert link.status(clk()).status == "unreachable"


def test_1_poller_thread_survives_any_error() -> None:
    import time

    calls = {"n": 0}

    def fetch(peer: str, timeout: float) -> dict:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return peer_doc("warm", 8, at=time.time())

    link = PeerLink(PairRule("p:1", ("x",), poll_s=0.2, stale_after_s=5.0), fetch=fetch)
    link.poll_once = _wrap_raising(link.poll_once)  # even a raising poll_once
    link.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not link.status(time.time()).ok:
            time.sleep(0.05)
        assert link.running
        assert link.status(time.time()).ok
    finally:
        link.stop()


def _wrap_raising(fn):
    state = {"first": True}

    def wrapped() -> None:
        if state["first"]:
            state["first"] = False
            raise RuntimeError("poll_once itself raised")
        fn()

    return wrapped


def test_1_broken_link_falls_through_to_peer_lost(tmp_path: Path, monkeypatch) -> None:
    dk, peer = _docker(), FakePeer(peer_doc("cool", None))
    d, _, clk, logs = _pair_daemon(tmp_path, dk, peer)
    d.discover()
    d.tick()
    assert d._pair.basis == "equal"

    def broken(now):
        raise RuntimeError("status broke")

    monkeypatch.setattr(d.pair_link, "status", broken)
    clk.t += 1
    d.tick()
    assert (d._pair.basis, d._pair.level, d._pair.cpus) == ("peer_lost", "warm", 12)
    assert dk.c["r0"].nano_cpus == 12_000_000_000  # not frozen at the last value


def test_1_failing_decision_falls_through_to_peer_lost(tmp_path: Path, monkeypatch) -> None:
    from heat_governor import daemon as daemon_mod
    from heat_governor.pair import decide as real

    def flaky(level, cpus, peer, **kw):
        if peer.ok:
            raise OverflowError("bad peer value")
        return real(level, cpus, peer, **kw)

    monkeypatch.setattr(daemon_mod, "pair_decide", flaky)
    d, _, _, logs = _pair_daemon(tmp_path, _docker(), FakePeer(peer_doc("cool", None)))
    d.tick()
    assert d._pair.basis == "peer_lost"
    assert any("decision failed" in m for m in logs)


def test_1_dead_poller_is_restarted_not_polled_in_the_tick(tmp_path: Path) -> None:
    peer = FakePeer(peer_doc("cool", None))
    d, _, _, _ = _pair_daemon(tmp_path, _docker(), peer)
    d.pair_link.started = True  # start() ran, but the thread is gone
    restarted = []
    d.pair_link.start = lambda: restarted.append(True)
    d.tick()
    assert restarted == [True]
    assert peer.calls == []


def test_2_labelled_batch_rank_never_reaches_the_cycler_when_inspect_fails(
    tmp_path: Path,
) -> None:
    dk = InspectCounting(
        container("r0", "sparkrun_qwen_rank0", "batch"),
        container("b1", "job", "batch"),
        fail_inspect=True,
    )
    d, sysr, _, _ = _pair_daemon(tmp_path, dk, FakePeer())
    d.discover()
    assert "r0" in d.pair_ids
    assert d.containers["r0"].role == "serving"
    assert set(d.batch()) == {"b1"}
    set_temp(sysr, 0, 99000)
    d.tick()
    d.cycler.cycle()
    assert ("pause", "r0") not in dk.calls
    d.cycler.stop()


def test_2_batch_filters_pair_containers_by_name_too(tmp_path: Path) -> None:
    dk = _docker()
    d, _, _, _ = _pair_daemon(tmp_path, dk, FakePeer())
    d.containers = {"r0": dk.c["r0"], "b1": dk.c["b1"]}  # r0 still role=batch, not in pair_ids
    assert d.pair_ids == set()
    assert set(d.batch()) == {"b1"}


def test_3_turning_pair_cpu_cap_off_restores_the_rank(tmp_path: Path) -> None:
    # An explicit 7-CPU original, so "released" can never equal the 4-CPU cap,
    # whatever the host's CPU count (an unlimited original restores to os.cpu_count()).
    dk = FakeDocker(container("r0", "sparkrun_qwen_rank0", "batch", nano=7_000_000_000))
    peer = FakePeer(peer_doc("hot", 4))
    d, _, clk, logs = _pair_daemon(tmp_path, dk, peer)
    d.discover()
    d.tick()
    assert dk.c["r0"].nano_cpus == 4_000_000_000
    d.set_pair(PairRule(PAIR["peer"], ("sparkrun_*",), cpu_cap=False))
    d.discover()
    assert dk.c["r0"].nano_cpus == 7_000_000_000  # its own limit is back
    assert "r0" not in d.ledger.nano_cpus
    assert any("no longer CPU-capped" in m for m in logs)
    clk.t += 1
    d.tick()
    assert dk.c["r0"].nano_cpus == 7_000_000_000  # and stays released


def test_3_rule_cpu_cap_off_on_reload_restores_a_serving_container(tmp_path: Path) -> None:
    from heat_governor.config import ContainerRule

    dk = FakeDocker(container("s1", "lane", None, nano=7_000_000_000))
    d, sysr, clk, _ = _pair_daemon(tmp_path, dk, FakePeer())
    d.opts.use_labels = False
    d.opts.containers = (ContainerRule("lane", "serving", True),)
    set_temp(sysr, 0, 91000)
    d.discover()
    d.tick()
    assert dk.c["s1"].nano_cpus == 4_000_000_000
    d.opts.containers = (ContainerRule("lane", "serving", False),)
    d.discover()
    assert dk.c["s1"].nano_cpus == 7_000_000_000
    assert "s1" not in d.ledger.nano_cpus


@pytest.mark.parametrize(
    "spec",
    ["http://10.77.77.2:9476", "10.77.77.2:9476/state", "::1:9476", "h:0", "h: 1", "[]:1", ""],
)
def test_4_host_port_rejects_urls_and_junk(spec: str) -> None:
    from heat_governor.config import host_port

    assert host_port(spec) is None


def test_4_host_port_rules() -> None:
    from heat_governor.config import host_port

    assert host_port("[::1]:9476") == ("::1", 9476)
    assert host_port("10.77.77.2:9476") == ("10.77.77.2", 9476)
    assert host_port(":9476") is None
    assert host_port(":9476", default_host="127.0.0.1") == ("127.0.0.1", 9476)
    problems = validate({"pair": {**PAIR, "peer": "http://10.77.77.2:9476"}})
    assert any("pair.peer must be HOST:PORT (no scheme or path" in p for p in problems)
    assert "daemon.listen must be HOST:PORT (no scheme or path)" in validate(
        {"daemon": {"listen": "http://127.0.0.1:9476"}}
    )


@pytest.mark.parametrize(
    ("daemon_listen", "pair_listen", "clash"),
    [
        ("0.0.0.0:9476", "10.77.77.1:9476", True),
        ("127.0.0.1:9476", "0.0.0.0:9476", True),
        ("[::]:9476", "10.77.77.1:9476", True),
        ("10.77.77.1:9476", "10.77.77.1:9476", False),  # same: served once
        ("127.0.0.1:9476", "10.77.77.1:9476", False),
        ("0.0.0.0:9476", "10.77.77.1:9477", False),
    ],
)
def test_5_wildcard_listen_clash_is_a_config_error(daemon_listen, pair_listen, clash) -> None:
    problems = validate(
        {"daemon": {"listen": daemon_listen}, "pair": {**PAIR, "listen": pair_listen}}
    )
    assert any("would fail (address in use)" in p for p in problems) is clash


def test_5_wildcard_cli_listen_skips_the_pair_server(tmp_path: Path) -> None:
    logs: list[str] = []
    rule = PairRule("h:1", ("x",), listen="10.77.77.1:9476")
    d = Daemon.__new__(Daemon)
    d.opts = Options(listen=("0.0.0.0", 9476), pair=rule, log=logs.append)
    d.recorder = None
    assert d._pair_server(rule) is None
    assert any("shares port 9476" in m for m in logs)


def test_6_run_once_binds_no_listener(tmp_path: Path, capsys, monkeypatch) -> None:
    import socket

    from heat_governor.cli._commands import governing

    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'platform = "spark"\n[daemon]\nlisten = "127.0.0.1:{port}"\n'
        f'[pair]\npeer = "127.0.0.1:1"\ncontainers = ["x"]\nlisten = "127.0.0.2:{port}"\n'
    )
    monkeypatch.setattr(governing, "_docker", lambda args, cfg: FakeDocker())
    try:
        rc, out, err = _cli(
            capsys,
            "run",
            "--once",
            "--config",
            str(cfg),
            "--state",
            str(tmp_path / "s.json"),
            "--data-dir",
            str(tmp_path / "d"),
        )
    finally:
        busy.close()
    assert rc == 0, err  # the live daemon's ports are busy: a preview must still run
    assert "pair: level=" in out


def test_7_peer_published_stale_window_is_honoured() -> None:
    clk = Clock()
    peer = FakePeer(peer_doc("cool", None, stale_after_s=30.0))
    link = PeerLink(PairRule("p:1", ("x",), stale_after_s=5.0), fetch=peer, clock=clk)
    link.poll_once()
    clk.t += 10
    st = link.status(clk())
    assert st.ok
    assert link.stale_after(st.reading) == 30.0  # its cadence, not ours
    clk.t += 21
    assert link.status(clk()).status == "stale"
    capped = PeerLink(
        PairRule("p:1", ("x",)), fetch=FakePeer(peer_doc("cool", None, stale_after_s=1e9))
    )
    capped.poll_once()
    assert capped.stale_after(capped.status(0).reading) == 600.0
    short = PeerLink(
        PairRule("p:1", ("x",), stale_after_s=10.0),
        fetch=FakePeer(peer_doc("cool", None, stale_after_s=5.0)),
    )
    short.poll_once()
    assert short.stale_after(short.status(0).reading) == 10.0  # ours when longer


def test_7_pair_doc_publishes_the_effective_window(tmp_path: Path) -> None:
    peer = FakePeer(peer_doc("cool", None, stale_after_s=30.0))
    d, _, _, _ = _pair_daemon(tmp_path, _docker(), peer)
    d.tick()
    assert read_state(tmp_path / "state.json")["pair"]["stale_after_s"] == 30.0


def test_8_result_for_a_replaced_peer_is_discarded() -> None:
    clk = Clock()
    link: PeerLink

    def fetch(peer: str, timeout: float) -> dict:
        link.configure(PairRule("b:2", ("x",)))  # a reload lands mid-read
        return peer_doc("hot", 4)

    link = PeerLink(PairRule("a:1", ("x",)), fetch=fetch, clock=clk)
    link.poll_once()
    st = link.status(clk())
    assert st.status == "unreachable"
    assert st.reading is None  # A's doc is not B's


def test_8_failure_for_a_replaced_peer_is_not_recorded() -> None:
    link: PeerLink

    def fetch(peer: str, timeout: float) -> dict:
        link.configure(PairRule("b:2", ("x",)))
        raise OSError("a is down")

    link = PeerLink(PairRule("a:1", ("x",)), fetch=fetch, clock=Clock())
    link.poll_once()
    assert link.status(0).error == "not read yet"


def test_8_frozen_peer_ages_although_it_answers() -> None:
    clk, peer = Clock(), FakePeer(peer_doc("cool", None, at=5.0))
    link = PeerLink(PairRule("p:1", ("x",), stale_after_s=5.0), fetch=peer, clock=clk)
    for _ in range(7):
        link.poll_once()  # every read succeeds, but `at` never changes
        clk.t += 1
    st = link.status(clk())
    assert (st.status, st.age_s, st.error) == ("stale", 7.0, None)
    peer.doc = peer_doc("cool", None, at=6.0)
    link.poll_once()
    assert link.status(clk()).ok


def test_9_one_level_list_and_one_address_parser() -> None:
    from heat_governor import pair, policy
    from heat_governor.cli._commands.governing import _listen
    from heat_governor.cli._errors import CliError

    assert pair.LEVELS is policy.LEVELS
    assert _listen(":9476") == ("127.0.0.1", 9476)
    assert _listen("[::1]:9476") == ("::1", 9476)
    with pytest.raises(CliError):
        _listen("http://127.0.0.1:9476")


def test_10_discovery_inspects_only_ranks_not_already_found(tmp_path: Path) -> None:
    dk = InspectCounting(
        container("r0", "sparkrun_qwen_rank0", "batch"),  # labelled: found by labels
        container("r1", "sparkrun_qwen_rank1", None),  # unlabelled: must be inspected
        container("o1", "other", None),
    )
    d, _, _, _ = _pair_daemon(tmp_path, dk, FakePeer())
    d.discover()
    assert d.pair_ids == {"r0", "r1"}
    assert dk.inspects == ["sparkrun_qwen_rank1"]

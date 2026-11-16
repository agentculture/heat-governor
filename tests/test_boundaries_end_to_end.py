"""The reader service against the real on-box command (in process): the JSON contract between
``boundaries_service`` and ``cli/_commands/boundaries`` is exercised without a hand-built fake
reply. The box is a tmp config dir and a fake /proc tree; nothing leaves the test."""

from __future__ import annotations

import json

import pytest

from heat_governor import boundaries_file
from heat_governor import boundaries_service as svc
from heat_governor import config as cfgmod
from heat_governor.cli._commands import boundaries as bnd
from heat_governor.transport import BoundariesResult
from tests.fakes_proc import add_proc
from tests.test_memkill import ME, MIB, SHELL, UID, base_tree

HOG = {"name": "hog", "comm": ["hog"], "max_mib": 100_000, "scope": "process"}
TIGHT = {"name": "hog", "comm": ["hog"], "max_mib": 10_000, "scope": "process"}


class RealBox:
    """A transport whose ``boundaries`` runs the box's own functions on a tmp config."""

    def __init__(self, cfg, proc):
        self.cfg, self.proc = cfg, proc

    def boundaries(self, verb, body, *, config_path):
        assert config_path == str(self.cfg)
        kw = {"proc_root": self.proc, "uid": UID, "self_pid": ME}
        if verb == "list":
            out = bnd.run_list(self.cfg)
        elif verb == "apply":
            out = bnd.run_apply(json.loads(body), self.cfg, **kw)
        else:
            out = bnd.run_preview(json.loads(body), self.cfg, **kw)
        return BoundariesResult(bnd.exit_code(out), json.dumps(out), "")


@pytest.fixture
def box(tmp_path):
    cfg = tmp_path / "cfg" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('[memory]\naction = "kill"\n')
    proc = tmp_path / "proc"
    proc.mkdir()
    base_tree(proc)
    add_proc(proc, 600, ppid=SHELL, uid=UID, comm="hog", anon_kib=30_000 * MIB)
    transport = RealBox(cfg, proc)
    thermal = {"version": "0.19.0", "config": str(cfg)}
    snap = {"machines": [{"name": "m", "state": "ok", "thermal": thermal}]}
    service = svc.BoundariesService(
        lambda: snap, lambda name: transport, audit_path=tmp_path / "audit.jsonl"
    )
    return service, cfg, tmp_path / "audit.jsonl"


def _apply(service, op, rule, base, **extra):
    request = {"base_version": base, "operation": op, "rule": rule, **extra}
    return service.apply("m", request, "tester")


def test_add_list_change_delete_round_trip(box):
    service, cfg, audit = box
    first = service.list("m")
    assert first["outcome"] == "ok"
    assert first["version"] == boundaries_file.MISSING_VERSION

    saved = _apply(service, "add", HOG, first["version"])
    assert saved["outcome"] == "saved"
    merged = cfgmod.load(cfg).memory.allow
    assert [(r.name, r.max_mib, r.source) for r in merged] == [("hog", 100_000, "boundaries.toml")]

    listed = service.list("m")
    assert listed["version"] == saved["version"]
    assert [r["name"] for r in listed["rules"]] == ["hog"]

    stale = _apply(service, "set", dict(HOG, max_mib=90_000), first["version"])
    assert stale["outcome"] == "conflict"
    assert cfgmod.load(cfg).memory.allow[0].max_mib == 100_000

    gone = _apply(service, "rm", {"name": "hog"}, saved["version"])
    assert gone["outcome"] == "saved"
    assert cfgmod.load(cfg).memory.allow == ()

    outcomes = [json.loads(line)["outcome"] for line in audit.read_text().splitlines()]
    assert outcomes.count("saved") == 2
    assert outcomes.count("conflict") == 1


def test_a_change_that_would_kill_is_refused_on_the_box_unless_allowed(box):
    service, cfg, _ = box
    base = service.list("m")["version"]
    refused = _apply(service, "add", TIGHT, base)
    assert refused["outcome"] == "would_kill"
    assert [p["pid"] for p in refused["processes"]] == [600]
    assert not (cfg.parent / "boundaries.toml").exists()

    forced = _apply(service, "add", TIGHT, base, allow_kill=True)
    assert forced["outcome"] == "saved"
    assert forced["forced"] is True


def test_preview_and_a_match_all_rule(box):
    service, _, _ = box
    seen = service.preview("m", HOG)
    assert seen["outcome"] == "ok"
    assert seen["matching"] == 1
    everything = {"name": "all", "cmdline": "*", "max_mib": 4096}
    base = service.list("m")["version"]
    assert _apply(service, "add", everything, base)["outcome"] == "invalid"

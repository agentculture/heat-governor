"""The reader's boundaries service and ``boundaries --machine``: fake transports only."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from heat_governor import boundaries_service as svc
from heat_governor.cli import main
from heat_governor.cli._commands import mesh
from heat_governor.fleet import Fleet, Machine
from heat_governor.fleet_reader import FleetReader, default_transport
from heat_governor.transport import BoundariesResult, LocalTransport, SshTransport, Unreachable
from tests.test_fleet_reader import FakeTransport, doc

CFG = "/etc/hg/config.toml"
HOG = {"name": "hog", "comm": ["hog"], "max_mib": 4096, "scope": "process"}


def _version(rules: list) -> str:
    if not rules:
        return "missing"
    return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()


class BoxTransport(FakeTransport):
    """A fake box: serves /state and the boundaries command over an in-memory rule list."""

    def __init__(self, version="0.19.0", config=CFG, rules=None, config_rules=()):
        super().__init__()
        self.state = doc(1_700_000_000.0, version=version, config=config)
        self.rules: list = list(rules or [])
        self.config_rules = list(config_rules)
        self.calls = []
        self.bodies = []
        self.apply_fail = None  # "after" | "before": raise Unreachable on apply
        self.list_fail = False

    def fetch(self) -> dict:
        self.calls.append("fetch")
        return dict(self.state, mem_guard=self._guard())

    def _guard(self) -> dict:
        rows = [dict(r, source="config.toml") for r in self.config_rules]
        rows += [dict(r, source="boundaries.toml") for r in self.rules]
        return {"action": "record", "armed": True, "boundaries": rows}

    def _listing(self) -> dict:
        rows = [dict(r, source="config.toml", editable=False) for r in self.config_rules]
        rows += [dict(r, source="boundaries.toml", editable=True) for r in self.rules]
        return {
            "outcome": "ok",
            "rules": rows,
            "version": _version(self.rules),
            "exists": bool(self.rules),
            "error": None,
            "config_path": CFG,
            "boundaries_path": "/etc/hg/boundaries.toml",
        }

    def boundaries(self, verb, body, *, config_path):
        self.calls.append(verb)
        self.bodies.append((verb, body, config_path))
        if verb == "list":
            if self.list_fail:
                raise Unreachable("ssh: connection lost")
            return BoundariesResult(0, json.dumps(self._listing()), "")
        if verb == "preview":
            out = {"outcome": "ok", "action": "record", "matching": 0, "processes": []}
            return BoundariesResult(0, json.dumps(out), "")
        return self._apply(json.loads(body))

    def _apply(self, req):
        if self.apply_fail == "before":
            raise Unreachable("ssh: connection lost")
        if req["base_version"] != _version(self.rules):
            out = {"outcome": "conflict", "version": _version(self.rules), "rules": self.rules}
            return BoundariesResult(1, json.dumps(out), "")
        name = req["rule"]["name"]
        rest = [r for r in self.rules if r["name"] != name]
        rule = {"scope": "process", **req["rule"]}
        self.rules = rest if req["operation"] == "rm" else [*rest, rule]
        if self.apply_fail == "after":
            raise Unreachable("ssh: timed out")
        out = {"outcome": "saved", "version": _version(self.rules), "rules": self.rules}
        return BoundariesResult(0, json.dumps(out), "")


def make(tmp_path, box=None, name="a", state="ok"):
    box = box or BoxTransport()
    fleet = Fleet(machines=(Machine(name, "ssh", "a.example"),), poll_s=10.0, timeout_s=5.0)
    reader = FleetReader(fleet, transport_factory=lambda m: box)
    reader.poll_once(name)
    service = svc.BoundariesService(
        reader.snapshot, lambda n: box, audit_path=tmp_path / "state" / "audit.jsonl"
    )
    return service, box


def audit_lines(tmp_path):
    path = tmp_path / "state" / "audit.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def req(op="add", rule=HOG, base="missing", **extra):
    return {"base_version": base, "operation": op, "rule": rule, **extra}


# -- version parsing and the gate ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,want",
    [
        ("0.19.0", (0, 19, 0)),
        ("0.19.0.dev3", (0, 19, 0)),
        ("0.19.0rc1", (0, 19, 0)),
        ("1.2.3+local", (1, 2, 3)),
        ("0.10.0", (0, 10, 0)),
        ("", None),
        (None, None),
        ("abc", None),
        ("0.19", None),
        (19, None),
    ],
)
def test_parse_version(text, want):
    assert svc.parse_version(text) == want


@pytest.mark.parametrize("ver", ["0.18.1", "0.9.9", "garbage", None, ""])
def test_old_or_unparseable_version_is_refused_before_any_transport_call(tmp_path, ver):
    service, box = make(tmp_path, BoxTransport(version=ver))
    box.calls.clear()
    out = service.apply("a", req(), "me@host")
    assert out["outcome"] == svc.VERSION_TOO_OLD
    assert svc.MIN_VERSION_TEXT in out["message"]
    assert out["needed_version"] == "0.19.0"
    assert box.calls == []
    assert svc.http_status(out) == 422
    last = audit_lines(tmp_path)[-1]
    assert last["outcome"] == svc.VERSION_TOO_OLD
    assert last["machine"] == "a"


def test_list_and_preview_are_gated_too(tmp_path):
    service, box = make(tmp_path, BoxTransport(version="0.18.0"))
    box.calls.clear()
    assert service.list("a")["outcome"] == svc.VERSION_TOO_OLD
    assert service.preview("a", {"name": "x", "comm": ["x"], "max_mib": 1})["outcome"] == (
        svc.VERSION_TOO_OLD
    )
    assert box.calls == []


def test_dev_version_of_the_first_release_passes(tmp_path):
    service, _ = make(tmp_path, BoxTransport(version="0.19.0.dev4"))
    assert service.list("a")["outcome"] == "ok"


def test_unknown_machine_and_no_config_path(tmp_path):
    service, box = make(tmp_path)
    assert service.list("nope")["outcome"] == svc.UNKNOWN_MACHINE
    assert svc.http_status({"outcome": svc.UNKNOWN_MACHINE}) == 404
    service, box = make(tmp_path, BoxTransport(config=None))
    box.calls.clear()
    out = service.apply("a", req(), "x")
    assert out["outcome"] == svc.NO_CONFIG_PATH
    assert box.calls == []


def test_machine_not_reporting(tmp_path):
    box = BoxTransport()
    fleet = Fleet(machines=(Machine("a", "ssh", "h"),), poll_s=10.0, timeout_s=5.0)
    reader = FleetReader(fleet, transport_factory=lambda m: box)  # never polled
    service = svc.BoundariesService(reader.snapshot, lambda n: box, audit_path=tmp_path / "au")
    out = service.apply("a", req(), "x")
    assert out["outcome"] == svc.MACHINE_UNREACHABLE
    assert svc.http_status(out) == 502
    assert box.calls == []


def test_every_outcome_has_a_status():
    for outcome in svc.BOX_OUTCOMES:
        assert outcome in svc.HTTP_STATUS
    for name in (
        svc.UNKNOWN_MACHINE,
        svc.MACHINE_UNREACHABLE,
        svc.VERSION_TOO_OLD,
        svc.NO_CONFIG_PATH,
        svc.BAD_REPLY,
        svc.NOT_SAVED,
        svc.UNKNOWN,
        svc.AUDIT_FAILED,
    ):
        assert svc.http_status({"outcome": name}) >= 400


# -- request rebuilding --------------------------------------------------------------------


def test_request_is_rebuilt_from_validated_fields(tmp_path):
    service, box = make(tmp_path)
    out = service.apply("a", req(allow_kill=True), "x")
    assert out["outcome"] == "saved"
    verb, body, path = next(b for b in box.bodies if b[0] == "apply")
    assert path == CFG
    assert json.loads(body) == {
        "base_version": "missing",
        "operation": "add",
        "rule": HOG,
        "allow_kill": True,
    }


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        {**req(), "token": "s3cret"},
        req(rule={**HOG, "extra": 1}),
        req(op="drop"),
        req(allow_kill="yes"),
        req(rule="hog"),
        req("rm", {"name": "hog", "max_mib": 5}),
        {"operation": "add", "rule": HOG},
    ],
)
def test_malformed_requests_are_invalid_and_send_nothing(tmp_path, bad):
    service, box = make(tmp_path)
    box.calls.clear()
    out = service.apply("a", bad, "x")
    assert out["outcome"] == "invalid"
    assert out["problems"]
    assert box.calls == []
    assert audit_lines(tmp_path) == []


# -- audit ---------------------------------------------------------------------------------


def test_apply_writes_attempt_then_final_line_with_mode_0600(tmp_path):
    service, box = make(tmp_path, BoxTransport(rules=[HOG]))
    new = {**HOG, "max_mib": 8192}
    out = service.apply("a", req("set", new, base=_version(box.rules)), "ori@laptop")
    assert out["outcome"] == "saved"
    lines = audit_lines(tmp_path)
    assert [x["outcome"] for x in lines] == ["attempt", "saved"]
    final = lines[-1]
    assert final["label"] == "ori@laptop"
    assert final["machine"] == "a"
    assert final["operation"] == "set"
    assert final["old"] == HOG
    assert final["new"] == new
    assert final["time"]
    mode = stat.S_IMODE(os.stat(tmp_path / "state" / "audit.jsonl").st_mode)
    assert mode == 0o600


def test_rm_audit_has_no_new_rule_and_add_has_no_old_rule(tmp_path):
    service, box = make(tmp_path, BoxTransport(rules=[HOG]))
    service.apply("a", req("rm", {"name": "hog"}, base=_version(box.rules)), "x")
    service.apply("a", req("add", {**HOG, "name": "n2"}, base=_version(box.rules)), "x")
    lines = audit_lines(tmp_path)
    assert lines[1]["old"] == HOG
    assert lines[1]["new"] is None
    assert lines[3]["old"] is None
    assert lines[3]["new"]["name"] == "n2"


def test_audit_is_append_only_across_services_and_fixes_a_wide_mode(tmp_path):
    path = tmp_path / "state" / "audit.jsonl"
    path.parent.mkdir()
    path.write_text('{"old":"line"}\n')
    os.chmod(path, 0o666)
    service, box = make(tmp_path)
    service.apply("a", req(), "x")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert audit_lines(tmp_path)[0] == {"old": "line"}
    assert len(audit_lines(tmp_path)) == 3


def test_audit_never_holds_tokens_or_process_data(tmp_path):
    service, box = make(tmp_path)
    service.apply("a", req(), "x")
    text = (tmp_path / "state" / "audit.jsonl").read_text()
    assert "token" not in text.lower()
    assert "processes" not in text


def test_audit_failure_fails_the_write_before_the_transport_call(tmp_path):
    service, box = make(tmp_path)
    (tmp_path / "state").mkdir()
    os.mkdir(tmp_path / "state" / "audit.jsonl")  # a directory: cannot be opened for append
    box.calls.clear()
    out = service.apply("a", req(), "x")
    assert out["outcome"] == svc.AUDIT_FAILED
    assert "apply" not in box.calls
    assert box.rules == []


# -- lost response -------------------------------------------------------------------------


def test_lost_response_after_apply_is_recovered_as_saved(tmp_path):
    service, box = make(tmp_path)
    box.apply_fail = "after"
    out = service.apply("a", req(), "x")
    assert out["outcome"] == "saved"
    assert out["recovered"] is True
    assert out["rules"] == [HOG]
    assert audit_lines(tmp_path)[-1]["outcome"] == "saved"


def test_repeating_a_recovered_request_changes_nothing(tmp_path):
    service, box = make(tmp_path)
    box.apply_fail = "after"
    service.apply("a", req(), "x")
    box.apply_fail = None
    before = list(box.rules)
    again = service.apply("a", req(), "x")
    assert again["outcome"] == "saved"
    assert again["idempotent"] is True
    assert box.rules == before


def test_repeating_an_rm_is_idempotent_too(tmp_path):
    service, box = make(tmp_path, BoxTransport(rules=[HOG]))
    base = _version(box.rules)
    assert service.apply("a", req("rm", {"name": "hog"}, base=base), "x")["outcome"] == "saved"
    again = service.apply("a", req("rm", {"name": "hog"}, base=base), "x")
    assert again["outcome"] == "saved"
    assert again["idempotent"] is True
    assert box.rules == []


def test_a_real_conflict_stays_a_conflict(tmp_path):
    other = {**HOG, "name": "other"}
    service, box = make(tmp_path, BoxTransport(rules=[other]))
    out = service.apply("a", req("add", HOG, base="missing"), "x")
    assert out["outcome"] == "conflict"


def test_lost_response_before_delivery_reports_not_saved(tmp_path):
    service, box = make(tmp_path)
    box.apply_fail = "before"
    out = service.apply("a", req(), "x")
    assert out["outcome"] == svc.NOT_SAVED
    assert box.rules == []


def test_lost_response_and_unreadable_file_is_unknown(tmp_path):
    service, box = make(tmp_path)
    box.apply_fail = "after"
    calls = {"n": 0}
    real = box.boundaries

    def flaky(verb, body, *, config_path):
        if verb == "list":
            calls["n"] += 1
            if calls["n"] > 1:  # the pre-apply list works, the recovery list does not
                raise Unreachable("down")
        return real(verb, body, config_path=config_path)

    box.boundaries = flaky
    out = service.apply("a", req(), "x")
    assert out["outcome"] == svc.UNKNOWN
    assert svc.http_status(out) == 504


def test_lost_response_with_someone_elses_change_is_a_conflict(tmp_path):
    service, box = make(tmp_path)
    box.apply_fail = "before"
    real = box.boundaries

    def racing(verb, body, *, config_path):
        if verb == "apply":
            box.rules = [{**HOG, "name": "other"}]
        return real(verb, body, config_path=config_path)

    box.boundaries = racing
    out = service.apply("a", req(), "x")
    assert out["outcome"] == "conflict"
    assert out["recovered"] is True


def test_bad_reply_and_box_error(tmp_path):
    service, box = make(tmp_path)
    box.boundaries = lambda *a, **k: BoundariesResult(0, "not json", "")
    assert service.list("a")["outcome"] == svc.BAD_REPLY
    box.boundaries = lambda *a, **k: BoundariesResult(2, "", '{"message":"no /proc"}\n')
    out = service.list("a")
    assert out["outcome"] == svc.BOX_ERROR
    box.boundaries = lambda *a, **k: BoundariesResult(0, '{"outcome":"weird"}', "")
    assert service.preview("a", {"name": "x", "comm": ["x"], "max_mib": 1})["outcome"] == (
        svc.BAD_REPLY
    )


def test_pre_apply_list_failure_refuses_the_write(tmp_path):
    service, box = make(tmp_path)
    box.list_fail = True
    out = service.apply("a", req(), "x")
    assert out["outcome"] == svc.MACHINE_UNREACHABLE
    assert "apply" not in box.calls


# -- the fleet reader passes the machine's bin ---------------------------------------------


def test_default_transport_passes_the_machine_bin():
    fleet = Fleet(machines=(), timeout_s=3)
    make_t = default_transport(fleet)
    ssh = make_t(Machine("s", "ssh", "spark", bin="/opt/hg/bin/heat-governor"))
    assert isinstance(ssh, SshTransport)
    assert ssh.bin_path == "/opt/hg/bin/heat-governor"
    local = make_t(Machine("l", "local", bin="/opt/hg/bin/heat-governor"))
    assert isinstance(local, LocalTransport)
    assert local.bin_path == "/opt/hg/bin/heat-governor"


# -- the CLI --machine ---------------------------------------------------------------------

FLEET_TOML = """\
poll_s = 1
timeout_s = 1
[[machines]]
name = "box"
host = "box.example"
"""


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    fleet = tmp_path / "fleet.toml"
    fleet.write_text(FLEET_TOML)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    box = BoxTransport(rules=[HOG], config_rules=[{"name": "cfg", "comm": ["c"], "max_mib": 9}])
    monkeypatch.setattr(mesh, "transport_factory", lambda m: box)
    monkeypatch.setattr("getpass.getuser", lambda: "ori")
    monkeypatch.setattr("socket.gethostname", lambda: "laptop")

    def run(*argv, stdin=None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO(stdin))
        rc = main(["boundaries", *argv, "--machine", "box", "--fleet", str(fleet), "--json"])
        out = capsys.readouterr()
        return rc, out.out, out.err

    return run, box, tmp_path / "xdg" / "heat-governor" / svc.AUDIT_FILE


def test_cli_list_machine_equals_the_boundary_data_in_mesh_status(cli, tmp_path, capsys):
    run, box, _ = cli
    rc, out, _ = run("list")
    assert rc == 0
    listed = json.loads(out)
    fleet = tmp_path / "fleet.toml"
    assert (
        main(["mesh", "status", "--fleet", str(fleet), "--url", "http://127.0.0.1:1", "--json"])
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    row = status["machines"][0]
    assert row["mem_guard"]["boundary_count"] == len(listed["rules"])
    snap = mesh._direct_snapshot(mesh._load(__import__("argparse").Namespace(fleet=str(fleet))))
    guard = snap["machines"][0]["thermal"]["mem_guard"]
    keys = ("name", "max_mib", "scope", "comm", "source")

    def pick(rows):
        return [{k: r.get(k) for k in keys} for r in rows]

    assert pick(listed["rules"]) == pick(guard["boundaries"])
    machine_state = next(m for m in snap["machines"] if m["name"] == "box")
    assert pick(listed["rules"]) == pick(machine_state["thermal"]["mem_guard"]["boundaries"])


def test_cli_set_add_rm_go_through_the_service_with_user_at_host(cli):
    run, box, audit = cli
    rc, out, _ = run("set", "--name", "hog", "--max-mib", "8192")
    assert rc == 0
    assert json.loads(out)["outcome"] == "saved"
    assert box.rules[0]["max_mib"] == 8192
    assert box.rules[0]["comm"] == ["hog"]  # unset flags keep the old rule's values
    assert run("add", "--name", "n2", "--comm", "x", "--max-mib", "10")[0] == 0
    assert run("rm", "--name", "n2")[0] == 0
    lines = [json.loads(x) for x in audit.read_text().splitlines()]
    assert {x["label"] for x in lines} == {"ori@laptop"}
    assert [x["outcome"] for x in lines] == ["attempt", "saved"] * 3
    assert stat.S_IMODE(os.stat(audit).st_mode) == 0o600


def test_cli_apply_and_preview_machine(cli):
    run, box, _ = cli
    body = json.dumps(req("rm", {"name": "hog"}, base=_version(box.rules)))
    rc, out, _ = run("apply", stdin=body)
    assert rc == 0
    assert box.rules == []
    rc, out, _ = run("preview", stdin=json.dumps({"rule": HOG}))
    assert rc == 0
    assert json.loads(out)["outcome"] == "ok"


def test_cli_old_machine_is_refused_with_the_needed_version(cli):
    run, box, audit = cli
    box.state["version"] = "0.18.1"
    rc, out, err = run("set", "--name", "hog", "--max-mib", "1")
    assert rc == 1
    assert "0.19.0" in err
    assert "apply" not in box.calls
    assert box.rules[0]["max_mib"] == 4096


def test_cli_unknown_machine(cli, tmp_path, monkeypatch, capsys):
    run, box, _ = cli
    rc = main(
        [
            "boundaries",
            "list",
            "--machine",
            "zzz",
            "--fleet",
            str(tmp_path / "fleet.toml"),
            "--json",
        ]
    )
    assert rc == 1
    assert "zzz" in capsys.readouterr().err

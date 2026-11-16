"""``heat-governor boundaries``: list, add, set, rm, apply, preview on a tmp config dir."""

from __future__ import annotations

import io
import json

import pytest

from heat_governor import boundaries_file
from heat_governor.cli import main
from heat_governor.cli._commands import boundaries as bnd
from heat_governor.explain import known_paths, resolve
from tests.fakes_proc import add_proc
from tests.test_memkill import ME, MIB, SHELL, UID, base_tree


def run(capsys, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HEAT_GOVERNOR_CONFIG", str(tmp_path / "absent" / "config.toml"))
    cfg = tmp_path / "cfg" / "config.toml"
    cfg.parent.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    base_tree(proc)
    add_proc(proc, 600, ppid=SHELL, uid=UID, comm="hog", anon_kib=30_000 * MIB)
    return cfg, proc


def go(capsys, monkeypatch, env, verb, *args, body=None):
    cfg, proc = env
    argv = ["boundaries", verb, "--config", str(cfg), "--proc-root", str(proc)]
    argv += ["--uid", str(UID), "--self-pid", str(ME), "--json", *args]
    rc, out, err = run(capsys, *argv, stdin=body, monkeypatch=monkeypatch)
    return rc, (json.loads(out) if out.strip() else None), err


def req(op, rule, base=boundaries_file.MISSING_VERSION, **extra):
    return json.dumps({"base_version": base, "operation": op, "rule": rule, **extra})


OK_RULE = {"name": "hog", "comm": ["hog"], "max_mib": 100_000}
TIGHT = {"name": "hog", "comm": ["hog"], "max_mib": 10_000}


# -- shape: overview, explain, usage ---------------------------------------------------


@pytest.mark.parametrize("json_mode", [False, True])
def test_bare_noun_prints_overview(capsys, json_mode):
    extra = ["--json"] if json_mode else []
    for argv in (["boundaries", "overview"], ["boundaries"]):
        rc, out, err = run(capsys, *argv, *extra)
        assert rc == 0
        assert err == ""
        assert "boundaries apply" in out


def test_explain_resolves_every_boundaries_path():
    paths = [p for p in known_paths() if p[:1] == ("boundaries",)]
    assert len(paths) == 8
    for p in paths:
        assert "heat-governor boundaries" in resolve(p)


def test_learn_lists_boundaries_verbs(capsys):
    _, out, _ = run(capsys, "learn", "--json")
    cmds = [c["path"] for c in json.loads(out)["commands"]]
    for verb in ("list", "add", "set", "rm", "apply", "preview"):
        assert ["boundaries", verb] in cmds


# -- list --------------------------------------------------------------------------------


def test_list_reports_rules_sources_and_version(capsys, monkeypatch, env):
    cfg, _ = env
    cfg.write_text('[[memory.allow]]\nname = "fromcfg"\ncomm = ["x"]\nmax_mib = 500\n')
    boundaries_file.write(cfg.parent / "boundaries.toml", "missing", [OK_RULE])
    rc, out, err = go(capsys, monkeypatch, env, "list")
    assert rc == 0
    assert err == ""
    assert out["outcome"] == "ok"
    by = {r["name"]: r for r in out["rules"]}
    assert by["fromcfg"]["source"] == "config.toml"
    assert by["fromcfg"]["editable"] is False
    assert by["hog"]["source"] == "boundaries.toml"
    assert by["hog"]["editable"] is True
    assert out["version"] == boundaries_file.read(cfg.parent / "boundaries.toml").version
    assert out["error"] is None


def test_list_without_files_and_with_rejected_file(capsys, monkeypatch, env):
    cfg, _ = env
    rc, out, _ = go(capsys, monkeypatch, env, "list")
    assert rc == 0
    assert out["rules"] == []
    assert out["version"] == "missing"
    assert out["exists"] is False
    (cfg.parent / "boundaries.toml").write_text("not = [valid")
    rc, out, _ = go(capsys, monkeypatch, env, "list")
    assert rc == 0
    assert "not valid TOML" in out["error"]
    assert out["rules"] == []


def test_list_text_mode(capsys, env):
    cfg, _ = env
    boundaries_file.write(cfg.parent / "boundaries.toml", "missing", [OK_RULE])
    rc, out, err = run(capsys, "boundaries", "list", "--config", str(cfg))
    assert rc == 0
    assert err == ""
    assert "hog:" in out
    assert "boundaries.toml version" in out


def test_default_config_path_is_used_without_flag(capsys, env):
    rc, out, _ = run(capsys, "boundaries", "list", "--json")
    assert rc == 0
    assert json.loads(out)["config_path"].endswith("absent/config.toml")


# -- apply -------------------------------------------------------------------------------


def test_apply_add_saves_and_lists(capsys, monkeypatch, env):
    cfg, _ = env
    rc, out, err = go(capsys, monkeypatch, env, "apply", body=req("add", OK_RULE))
    assert rc == 0
    assert err == ""
    assert out["outcome"] == "saved"
    assert out["rules"] == [OK_RULE]
    assert out["guard_configured"] is False
    snap = boundaries_file.read(cfg.parent / "boundaries.toml")
    assert snap.rules == [OK_RULE]
    assert out["version"] == snap.version


def test_apply_set_and_rm(capsys, monkeypatch, env):
    cfg, _ = env
    path = cfg.parent / "boundaries.toml"
    v1 = boundaries_file.write(path, "missing", [OK_RULE]).version
    bigger = {**OK_RULE, "max_mib": 200_000}
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("set", bigger, v1))
    assert rc == 0
    assert boundaries_file.read(path).rules == [bigger]
    v2 = out["version"]
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("rm", {"name": "hog"}, v2))
    assert rc == 0
    assert boundaries_file.read(path).rules == []


def test_apply_rejects_unknown_rule_keys_with_validator_message(capsys, monkeypatch, env):
    cfg, _ = env
    bad = {**OK_RULE, "source": "config.toml"}
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("add", bad))
    assert rc == 1
    assert out["outcome"] == "invalid"
    assert any("unknown key" in p and "source" in p for p in out["problems"])
    assert not (cfg.parent / "boundaries.toml").exists()


@pytest.mark.parametrize(
    "rule,needle",
    [
        ({"name": "x", "comm": ["*"], "max_mib": 500}, "matches every process"),
        ({"name": "x", "comm": ["a"], "max_mib": 1}, "at least"),
        ({"name": "x", "max_mib": 500}, "needs at least one"),
    ],
)
def test_apply_invalid_rules_write_nothing(capsys, monkeypatch, env, rule, needle):
    cfg, _ = env
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("add", rule))
    assert rc == 1
    assert out["outcome"] == "invalid"
    assert needle in " ".join(out["problems"])
    assert not (cfg.parent / "boundaries.toml").exists()


@pytest.mark.parametrize("body", ["not json", "[]", '{"operation": "add"}', req("zap", OK_RULE)])
def test_apply_bad_requests_are_invalid(capsys, monkeypatch, env, body):
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=body)
    assert rc == 1
    assert out["outcome"] == "invalid"


def test_apply_bad_request_text_goes_to_stderr(capsys, monkeypatch, env):
    cfg, proc = env
    rc, out, err = run(
        capsys,
        "boundaries",
        "apply",
        "--config",
        str(cfg),
        "--proc-root",
        str(proc),
        stdin="nope",
        monkeypatch=monkeypatch,
    )
    assert rc == 1
    assert out == ""
    assert "invalid" in err


def test_apply_add_existing_and_set_rm_missing_are_invalid(capsys, monkeypatch, env):
    cfg, _ = env
    path = cfg.parent / "boundaries.toml"
    v1 = boundaries_file.write(path, "missing", [OK_RULE]).version
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("add", OK_RULE, v1))
    assert rc == 1
    assert "already exists" in out["problems"][0]
    ghost = {**OK_RULE, "name": "ghost"}
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("set", ghost, v1))
    assert "no rule named" in out["problems"][0]
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("rm", {"name": "ghost"}, v1))
    assert "no rule named" in out["problems"][0]
    assert boundaries_file.read(path).version == v1


def test_apply_rm_takes_only_a_name(capsys, monkeypatch, env):
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("rm", OK_RULE))
    assert rc == 1
    assert out["outcome"] == "invalid"


def test_apply_conflict_returns_current_rules_and_writes_nothing(capsys, monkeypatch, env):
    cfg, _ = env
    path = cfg.parent / "boundaries.toml"
    boundaries_file.write(path, "missing", [OK_RULE])
    before = path.read_bytes()
    other = {"name": "other", "comm": ["o"], "max_mib": 4096}
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("add", other, "stale"))
    assert rc == 1
    assert out["outcome"] == "conflict"
    assert out["rules"] == [OK_RULE]
    assert out["version"] == boundaries_file.read(path).version
    assert path.read_bytes() == before


def test_apply_name_in_config_toml_is_refused(capsys, monkeypatch, env):
    cfg, _ = env
    cfg.write_text('[[memory.allow]]\nname = "hog"\ncomm = ["x"]\nmax_mib = 500000\n')
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("add", OK_RULE))
    assert rc == 1
    assert out["outcome"] == "invalid"
    assert "defined in both" in " ".join(out["problems"])
    assert not (cfg.parent / "boundaries.toml").exists()


def test_apply_refuses_when_boundaries_file_is_rejected(capsys, monkeypatch, env):
    cfg, _ = env
    path = cfg.parent / "boundaries.toml"
    path.write_text("broken = [")
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("add", OK_RULE))
    assert rc == 1
    assert out["outcome"] == "invalid"
    assert path.read_text() == "broken = ["


# -- the kill check ----------------------------------------------------------------------


def test_apply_would_kill_writes_nothing_and_returns_process(capsys, monkeypatch, env):
    cfg, _ = env
    rc, out, err = go(capsys, monkeypatch, env, "apply", body=req("add", TIGHT))
    assert rc == 1
    assert err == ""
    assert out["outcome"] == "would_kill"
    assert [p["pid"] for p in out["processes"]] == [600]
    assert out["far_over_killable"] == 1
    assert out["guard_configured"] is False
    assert not (cfg.parent / "boundaries.toml").exists()


def test_apply_would_kill_regardless_of_configured_action(capsys, monkeypatch, env):
    cfg, _ = env
    cfg.write_text('[memory]\naction = "record"\n')
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("add", TIGHT))
    assert rc == 1
    assert out["outcome"] == "would_kill"
    assert out["guard_configured"] is True
    assert out["action"] == "record"


def test_apply_would_kill_text_goes_to_stderr(capsys, env):
    cfg, proc = env
    body = req("add", TIGHT)
    import sys

    sys.stdin = io.StringIO(body)
    try:
        rc, out, err = run(
            capsys, "boundaries", "apply", "--config", str(cfg), "--proc-root", str(proc),
            "--uid", str(UID), "--self-pid", str(ME),
        )  # fmt: skip
    finally:
        sys.stdin = sys.__stdin__
    assert rc == 1
    assert out == ""
    assert "would kill" in err
    assert "pid 600" in err


def test_apply_allow_kill_writes(capsys, monkeypatch, env):
    cfg, _ = env
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("add", TIGHT, allow_kill=True))
    assert rc == 0
    assert out["outcome"] == "saved"
    assert out["forced"] is True
    assert [p["pid"] for p in out["processes"]] == [600]
    assert boundaries_file.read(cfg.parent / "boundaries.toml").rules == [TIGHT]


def test_apply_checks_whole_would_be_set_with_config_rules(capsys, monkeypatch, env):
    cfg, _ = env
    cfg.write_text('[[memory.allow]]\nname = "tight"\ncomm = ["hog"]\nmax_mib = 1000\n')
    rc, out, _ = go(capsys, monkeypatch, env, "apply", body=req("add", OK_RULE))
    assert rc == 1
    assert out["outcome"] == "would_kill"


# -- preview -----------------------------------------------------------------------------


def test_preview_returns_matches_and_writes_nothing(capsys, monkeypatch, env):
    cfg, _ = env
    rc, out, err = go(capsys, monkeypatch, env, "preview", body=json.dumps({"rule": TIGHT}))
    assert rc == 0
    assert err == ""
    assert out["outcome"] == "ok"
    assert out["matching"] == 1
    assert out["far_over_killable"] == 1
    assert out["processes"][0]["state"] == "far_over"
    assert out["guard_configured"] is False
    assert not (cfg.parent / "boundaries.toml").exists()


def test_preview_invalid_draft(capsys, monkeypatch, env):
    rc, out, _ = go(capsys, monkeypatch, env, "preview", body=json.dumps({"rule": {"name": "x"}}))
    assert rc == 1
    assert out["outcome"] == "invalid"


def test_missing_proc_root_is_an_environment_error(capsys, monkeypatch, env):
    cfg, proc = env
    rc, out, err = run(
        capsys, "boundaries", "preview", "--config", str(cfg), "--proc-root", str(proc / "gone"),
        "--json", stdin=json.dumps({"rule": TIGHT}), monkeypatch=monkeypatch,
    )  # fmt: skip
    assert rc == 2
    assert out == ""
    assert json.loads(err)["code"] == 2


def test_invalid_config_toml_is_an_environment_error(capsys, monkeypatch, env):
    cfg, _ = env
    cfg.write_text("[memory\n")
    rc, out, err = go(capsys, monkeypatch, env, "list")
    assert rc == 2
    assert out is None
    assert json.loads(err)["code"] == 2


# -- add / set / rm flags ----------------------------------------------------------------


def flags(capsys, env, verb, *args):
    cfg, proc = env
    return run(
        capsys, "boundaries", verb, "--config", str(cfg), "--proc-root", str(proc),
        "--uid", str(UID), "--self-pid", str(ME), "--json", *args,
    )  # fmt: skip


def test_add_set_rm_flags_go_through_apply(capsys, env):
    cfg, _ = env
    path = cfg.parent / "boundaries.toml"
    rc, out, _ = flags(
        capsys, env, "add", "--name", "hog", "--comm", "hog", "--comm", "hog2", "--max-mib", "99999"
    )
    assert rc == 0
    assert json.loads(out)["outcome"] == "saved"
    assert boundaries_file.read(path).rules == [
        {"name": "hog", "comm": ["hog", "hog2"], "max_mib": 99999}
    ]
    rc, out, _ = flags(
        capsys, env, "set", "--name", "hog", "--max-mib", "88888", "--scope", "total"
    )
    assert rc == 0
    rule = boundaries_file.read(path).rules[0]
    assert rule["comm"] == ["hog", "hog2"]
    assert (rule["max_mib"], rule["scope"]) == (88888, "total")
    rc, out, _ = flags(capsys, env, "rm", "--name", "hog")
    assert rc == 0
    assert boundaries_file.read(path).rules == []


def test_add_flag_would_kill_and_allow_kill(capsys, env):
    cfg, _ = env
    args = ("--name", "hog", "--comm", "hog", "--max-mib", "10000")
    rc, out, _ = flags(capsys, env, "add", *args)
    assert rc == 1
    assert json.loads(out)["outcome"] == "would_kill"
    rc, out, _ = flags(capsys, env, "add", *args, "--allow-kill")
    assert rc == 0
    assert json.loads(out)["outcome"] == "saved"


def test_add_without_max_mib_is_invalid(capsys, env):
    rc, out, _ = flags(capsys, env, "add", "--name", "hog", "--comm", "hog")
    assert rc == 1
    assert "max_mib is required" in " ".join(json.loads(out)["problems"])


def test_exit_code_helper():
    assert bnd.exit_code({"outcome": "saved"}) == 0
    assert bnd.exit_code({"outcome": "ok"}) == 0
    for name in ("conflict", "would_kill", "invalid"):
        assert bnd.exit_code({"outcome": name}) == 1

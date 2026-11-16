"""Strict ``[[memory.allow]]`` boundaries and ``[memory] act_over_pct``."""

from __future__ import annotations

import os
import tomllib

import pytest

from heat_governor import config as cfgmod
from heat_governor.daemon import Daemon, Options
from heat_governor.platforms import SPARK
from heat_governor.sense import Sensor
from tests.fakes import SPARK_ZONES, make_sys

GOOD = {
    "name": "vllm",
    "comm": ["vllm", "python3"],
    "cmdline": "*vllm*serve*",
    "cgroup": "*docker-*.scope",
    "max_mib": 102400,
    "scope": "total",
}


def _problems(allow, **mem) -> list[str]:
    return cfgmod.validate({"memory": {"allow": allow, **mem}})


def test_good_rule_validates_and_parses():
    data = {"memory": {"allow": [GOOD], "act_over_pct": 25}}
    assert cfgmod.validate(data) == []
    m = cfgmod.Config(data=data).memory
    assert m.act_over_pct == 25.0
    assert m.allow == (
        cfgmod.AllowRule(
            name="vllm",
            max_mib=102400,
            comm=("vllm", "python3"),
            cmdline="*vllm*serve*",
            cgroup="*docker-*.scope",
            scope="total",
        ),
    )


def test_defaults_without_allow():
    m = cfgmod.Config(data={"memory": {}}).memory
    assert m.allow == ()
    assert m.act_over_pct == 50.0


def test_scope_defaults_to_process_and_one_matcher_is_enough():
    data = {"memory": {"allow": [{"name": "a", "cgroup": "*x*", "max_mib": 64}]}}
    assert cfgmod.validate(data) == []
    rule = cfgmod.Config(data=data).memory.allow[0]
    assert rule.scope == "process"
    assert rule.comm == ()
    assert rule.cmdline is None


def test_toml_array_of_tables_parses():
    text = '[memory]\n[[memory.allow]]\nname = "x"\ncomm = ["x"]\nmax_mib = 100\n'
    data = tomllib.loads(text)
    assert cfgmod.validate(data) == []
    assert cfgmod.Config(data=data).memory.allow[0].name == "x"


def test_allow_must_be_a_list():
    probs = cfgmod.validate({"memory": {"allow": {"name": "x"}}})
    assert probs == ["memory.allow has the wrong type"]


def test_allow_item_must_be_a_table():
    assert _problems([3]) == ["memory.allow[0] must be a table"]


@pytest.mark.parametrize(
    "patch,needle",
    [
        ({"name": None}, "memory.allow[0].name is required"),
        ({"name": ""}, "memory.allow[0].name must be a non-empty string"),
        ({"name": 3}, "memory.allow[0].name has the wrong type"),
        ({"max_mib": None}, "memory.allow[0].max_mib is required"),
        ({"max_mib": 63}, "memory.allow[0].max_mib must be at least 64"),
        ({"max_mib": 100.5}, "memory.allow[0].max_mib has the wrong type"),
        ({"max_mib": True}, "memory.allow[0].max_mib has the wrong type"),
        ({"scope": "group"}, "memory.allow[0].scope must be one of process, total"),
        ({"comm": "vllm"}, "memory.allow[0].comm has the wrong type"),
        ({"comm": []}, "memory.allow[0].comm must be a non-empty list of process names"),
        ({"comm": ["ok", ""]}, "memory.allow[0].comm must be a non-empty list of process names"),
        ({"comm": [3]}, "memory.allow[0].comm must be a non-empty list of process names"),
        ({"cmdline": ""}, "memory.allow[0].cmdline must be a non-empty glob"),
        ({"cgroup": ""}, "memory.allow[0].cgroup must be a non-empty glob"),
        ({"cgroup": 5}, "memory.allow[0].cgroup has the wrong type"),
        ({"bogus": 1}, "unknown key memory.allow[0].'bogus'"),
    ],
)
def test_bad_rule(patch, needle):
    item = {**GOOD, **patch}
    item = {k: v for k, v in item.items() if v is not None}
    probs = _problems([item])
    assert any(needle in p for p in probs), probs


def test_rule_without_a_matcher():
    probs = _problems([{"name": "x", "max_mib": 100}])
    assert probs == ["memory.allow[0] needs at least one of comm, cmdline, cgroup"]


def test_duplicate_names():
    probs = _problems([GOOD, {**GOOD, "max_mib": 200}])
    assert probs == ["memory.allow[1].name 'vllm' is a duplicate"]


@pytest.mark.parametrize("bad", [0, 1001, "50", True])
def test_act_over_pct_bad(bad):
    probs = cfgmod.validate({"memory": {"act_over_pct": bad}})
    assert any("memory.act_over_pct" in p for p in probs), probs


@pytest.mark.parametrize("ok", [1, 1000, 12.5])
def test_act_over_pct_edges_ok(ok):
    assert cfgmod.validate({"memory": {"act_over_pct": ok}}) == []


def test_template_mentions_allow_and_stays_valid():
    text = cfgmod.template(SPARK)
    assert cfgmod.validate(tomllib.loads(text)) == []
    assert "# [[memory.allow]]" in text
    assert "act_over_pct" in text


# -- boundaries.toml: the page-managed sibling file (#18, PR 2) --------------------------

RULE_A = '[[memory.allow]]\nname = "a"\ncomm = ["a"]\nmax_mib = 100\n'
RULE_B = '[[memory.allow]]\nname = "b"\ncomm = ["b"]\nmax_mib = 200\n'


def _write(tmp_path, cfg: str | None, bounds: str | None):
    path = tmp_path / "config.toml"
    if cfg is not None:
        path.write_text(cfg)
    if bounds is not None:
        (tmp_path / "boundaries.toml").write_text(bounds)
    return path


def test_rules_merge_from_both_files_tagged_with_their_source(tmp_path):
    path = _write(tmp_path, "[memory]\n" + RULE_A, RULE_B)
    allow = cfgmod.load(path).memory.allow
    assert [(r.name, r.source) for r in allow] == [("a", "config.toml"), ("b", "boundaries.toml")]


def test_boundaries_sit_beside_the_loaded_config_not_the_environment(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(other))
    (other / "heat-governor").mkdir()
    (other / "heat-governor" / "boundaries.toml").write_text(RULE_B)
    path = _write(tmp_path, "", RULE_A)
    assert [r.name for r in cfgmod.load(path).memory.allow] == ["a"]


def test_a_name_in_both_files_names_both_files(tmp_path):
    path = _write(tmp_path, "[memory]\n" + RULE_A, RULE_A)
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(path)
    text = str(exc.value)
    assert "config.toml" in text
    assert "boundaries.toml" in text


def test_boundaries_accepts_only_memory_allow_tables(tmp_path):
    for body in ("[daemon]\npoll_s = 2\n", '[memory]\naction = "kill"\n', "x = 1\n"):
        path = _write(tmp_path, "", body)
        with pytest.raises(cfgmod.ConfigError):
            cfgmod.load(path)


def test_an_invalid_boundaries_file_at_startup_is_an_error(tmp_path):
    path = _write(tmp_path, "", "[[memory.allow]]\nname = 'x'\n")
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(path)
    assert "boundaries.toml" in str(exc.value)


@pytest.mark.parametrize(
    "matchers",
    ['cmdline = "*"', 'cgroup = "**"', 'cgroup = "/*"', 'cmdline = "*[!x]*"', 'cmdline = "?*"'],
)
@pytest.mark.parametrize("where", ["config", "boundaries"])
def test_a_match_all_rule_is_rejected_in_either_file(tmp_path, matchers, where):
    rule = f'[[memory.allow]]\nname = "all"\n{matchers}\nmax_mib = 100\n'
    path = _write(
        tmp_path,
        "[memory]\n" + rule if where == "config" else "",
        rule if where == "boundaries" else None,
    )
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(path)
    assert "matches every process" in str(exc.value)


def test_a_comm_list_is_never_match_all_because_names_are_compared_exactly():
    # memboundary.matches() intersects comm with the process's names: "*" is a literal name
    for extra in ({}, {"cmdline": "*"}, {"cgroup": "/*"}):
        items = [{"name": "x", "comm": ["*"], "max_mib": 100, **extra}]
        assert cfgmod.allow_problems(items) == []


def test_a_rejected_boundaries_file_never_doubles_a_name_config_now_defines(tmp_path):
    rule = '[[memory.allow]]\nname = "{}"\ncomm = ["a"]\nmax_mib = 100\n'
    path = _write(tmp_path, "[memory]\n", rule.format("hog") + rule.format("other"))
    good = cfgmod.load(path)
    path.write_text("[memory]\n" + rule.format("hog"))
    cfg = cfgmod.load(path, last_good=good)
    assert cfg.boundaries_error
    assert [r.name for r in cfg.memory.allow] == ["hog", "other"]
    assert [r.source for r in cfg.memory.allow] == [cfgmod.CONFIG_FILE, cfgmod.BOUNDARIES_FILE]


def test_a_write_that_lands_during_the_read_is_seen_as_a_change(tmp_path, monkeypatch):
    rule = '[[memory.allow]]\nname = "a"\ncomm = ["a"]\nmax_mib = 100\n'
    path = _write(tmp_path, "[memory]\n", rule)
    bpath = path.parent / cfgmod.BOUNDARIES_FILE
    real = cfgmod.load_boundaries

    def racing(p, data=None):
        out = real(p, data)
        bpath.write_text(rule.replace("100", "200"))  # lands after the read
        os.utime(bpath, (10**9, 10**9))
        return out

    monkeypatch.setattr(cfgmod, "load_boundaries", racing)
    cfg = cfgmod.load(path)
    assert cfg.memory.allow[0].max_mib == 100
    assert cfgmod.changed(cfg)


def test_a_wildcard_next_to_a_real_matcher_is_fine():
    items = [{"name": "x", "comm": ["bash"], "cmdline": "*", "max_mib": 100}]
    assert cfgmod.allow_problems(items) == []


def test_mtime_is_a_pair_and_deleting_boundaries_is_a_change(tmp_path):
    path = _write(tmp_path, "", RULE_A)
    cfg = cfgmod.load(path)
    assert cfg.mtime[0] is not None
    assert cfg.mtime[1] is not None
    assert not cfgmod.changed(cfg)
    (tmp_path / "boundaries.toml").unlink()
    assert cfgmod.changed(cfg)
    assert cfgmod.load(path).memory.allow == ()


def test_touching_only_boundaries_is_a_change(tmp_path):
    path = _write(tmp_path, "", RULE_A)
    cfg = cfgmod.load(path)
    later = cfg.mtime[1] + 5
    os.utime(tmp_path / "boundaries.toml", (later, later))
    assert cfgmod.changed(cfg)


def test_last_good_keeps_boundaries_on_an_invalid_file(tmp_path):
    path = _write(tmp_path, "", RULE_A)
    good = cfgmod.load(path)
    (tmp_path / "boundaries.toml").write_text("not [valid")
    new = cfgmod.load(path, last_good=good)
    assert [r.name for r in new.memory.allow] == ["a"]
    assert "boundaries.toml" in new.boundaries_error


# -- the daemon's reload bookkeeping --------------------------------------------------------


def _daemon(tmp_path, cfg):
    sysr = make_sys(tmp_path, SPARK_ZONES)
    opts = Options(
        state_path=tmp_path / "state.json",
        data_dir=tmp_path / "data",
        log=lambda _m: None,
        config=cfg,
        platform="spark",
        reload_every_s=0.0,
        record=False,
        listeners=False,
    )
    return Daemon(SPARK, opts, sensor=Sensor(SPARK.zones, sys_root=sysr), docker=None)


_BUMPS = iter(range(10, 10**6, 10))


def _bump(path):
    # each bump moves further ahead, so two rewrites inside one clock tick still differ
    st = path.stat().st_mtime + next(_BUMPS)
    os.utime(path, (st, st))


def test_touching_only_boundaries_reloads_and_deleting_it_drops_the_rules(tmp_path):
    path = _write(tmp_path, "", RULE_A)
    d = _daemon(tmp_path, cfgmod.load(path))
    bpath = tmp_path / "boundaries.toml"
    bpath.write_text(RULE_B)
    _bump(bpath)
    d.maybe_reload(1.0)
    assert [r.name for r in d.opts.config.memory.allow] == ["b"]
    bpath.unlink()
    d.maybe_reload(2.0)
    assert d.opts.config.memory.allow == ()


def test_invalid_boundaries_keep_the_last_good_while_a_config_edit_applies(tmp_path):
    path = _write(tmp_path, "", RULE_A)
    d = _daemon(tmp_path, cfgmod.load(path))
    bpath = tmp_path / "boundaries.toml"
    bpath.write_text("[[memory.allow]]\nname = 'x'\n")
    _bump(bpath)
    path.write_text("[daemon]\npoll_s = 3.0\n")
    _bump(path)
    d.maybe_reload(1.0)
    assert d.opts.config.daemon["poll_s"] == 3.0
    assert [r.name for r in d.opts.config.memory.allow] == ["a"]
    assert "boundaries.toml" in d.config_error
    bpath.write_text(RULE_B)
    _bump(bpath)
    d.maybe_reload(2.0)
    assert d.config_error is None
    assert [r.name for r in d.opts.config.memory.allow] == ["b"]

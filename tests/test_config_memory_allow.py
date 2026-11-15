"""Strict ``[[memory.allow]]`` boundaries and ``[memory] act_over_pct``."""

from __future__ import annotations

import tomllib

import pytest

from heat_governor import config as cfgmod
from heat_governor.platforms import SPARK

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

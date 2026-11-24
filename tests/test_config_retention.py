"""[daemon] keep_days and log_max_mib: defaults, strict validation, generated config."""

from __future__ import annotations

import tomllib

import pytest

from heat_governor import config as cfgmod
from heat_governor.daemon import Daemon
from heat_governor.platforms import SPARK


def _problems(**daemon: object) -> list[str]:
    return cfgmod.validate({"daemon": daemon})


def _load(tmp_path, body: str) -> cfgmod.Config:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return cfgmod.load(path)


def test_defaults() -> None:
    cfg = cfgmod.Config(path=None)
    assert cfg.keep_days == 7
    assert cfg.log_max_mib == 10


@pytest.mark.parametrize("value", [1, 7, 365])
def test_keep_days_accepts_positive_ints(value: int) -> None:
    assert _problems(keep_days=value) == []


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, 7.0, "7"])
def test_keep_days_refused(value: object) -> None:
    assert any("keep_days" in p for p in _problems(keep_days=value))


@pytest.mark.parametrize("value", [0, -1, -0.5, True, "10"])
def test_log_max_mib_refused(value: object) -> None:
    assert any("log_max_mib" in p for p in _problems(log_max_mib=value))


@pytest.mark.parametrize("value", [1, 10, 0.5, 2.5])
def test_log_max_mib_accepts_positive_numbers(value: float) -> None:
    assert _problems(log_max_mib=value) == []


def test_load_raises_config_error_for_zero_keep_days(tmp_path) -> None:
    with pytest.raises(cfgmod.ConfigError):
        _load(tmp_path, "[daemon]\nkeep_days = 0\n")
    with pytest.raises(cfgmod.ConfigError):
        _load(tmp_path, "[daemon]\nkeep_days = -3\n")
    with pytest.raises(cfgmod.ConfigError):
        _load(tmp_path, "[daemon]\nlog_max_mib = 0\n")


def test_load_reads_values(tmp_path) -> None:
    cfg = _load(tmp_path, "[daemon]\nkeep_days = 3\nlog_max_mib = 2.5\n")
    assert cfg.keep_days == 3
    assert cfg.log_max_mib == 2.5


def test_template_has_commented_lines_in_daemon_table() -> None:
    text = cfgmod.template(SPARK)
    daemon_table = text.split("[daemon]", 1)[1].split("\n[", 1)[0]
    assert "# keep_days = 7" in daemon_table
    assert "# log_max_mib = 10" in daemon_table
    assert cfgmod.validate(tomllib.loads(text)) == []


def test_not_restart_only() -> None:
    assert "keep_days" not in Daemon.RESTART_ONLY
    assert "log_max_mib" not in Daemon.RESTART_ONLY

"""Keep every test away from the operator's real config and state files."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HEAT_GOVERNOR_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("HEAT_GOVERNOR_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))

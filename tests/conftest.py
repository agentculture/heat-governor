"""Keep every test away from the operator's real config and state files."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HEAT_GOVERNOR_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("HEAT_GOVERNOR_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def _no_prune(records_dir, now, keep_days, **_kw) -> dict:
    return {"deleted": [], "compressed": [], "bytes_freed": 0, "errors": [], "warnings": []}


@pytest.fixture(autouse=True)
def _no_background_retention(monkeypatch):
    """A Daemon built without ``prune=`` gets a no-op retention pass in tests.

    Many daemon tests call ``start()`` without ``shutdown()``; with the real pass their
    background retention thread would delete the fixtures' old-dated day files while the
    test still reads them. Tests about retention pass ``prune=`` explicitly (their own
    ``heat_governor.retention`` import is untouched)."""
    import types

    import heat_governor.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "retention", types.SimpleNamespace(prune_pass=_no_prune))

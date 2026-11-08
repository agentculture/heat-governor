"""scripts/sonar-gate.py: CI fails while SonarCloud has unaccepted issues or hotspots."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts" / "sonar-gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("sonar_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load()


def test_project_key_read_from_properties(tmp_path: Path) -> None:
    props = tmp_path / "s.properties"
    props.write_text("# c\nsonar.organization=o\nsonar.projectKey = o_repo\n")
    assert gate.project_key(props) == "o_repo"


def test_scope_pull_request_and_branch() -> None:
    assert gate.scope_params("9", None) == ({"pullRequest": "9"}, {"pullRequest": "9"})
    issues, hotspots = gate.scope_params(None, "main")
    assert issues == {"branch": "main", "inNewCodePeriod": "true"}
    assert hotspots == issues


def test_scope_needs_one() -> None:
    with pytest.raises(SystemExit):
        gate.scope_params(None, None)


def test_queries_ask_for_unresolved_issues_and_unreviewed_hotspots(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_get(path, params, token):
        calls.append((path, params))
        if path == "/api/issues/search":
            item = {"component": "k:a.py", "line": 3, "rule": "python:S1", "message": "m"}
            return {"total": 1, "issues": [{**item, "severity": "MAJOR"}]}
        return {"paging": {"total": 0}, "hotspots": []}

    monkeypatch.setattr(gate, "_get", fake_get)
    found = gate.findings("k", "9", None, "t")
    assert found == ["issue python:S1 MAJOR a.py:3: m"]
    issues = dict(calls[0][1])
    assert issues["resolved"] == "false"  # accepted / false positive are resolutions
    assert issues["pullRequest"] == "9"
    assert calls[1][1]["status"] == "TO_REVIEW"


def test_paging_reads_every_page(monkeypatch) -> None:
    pages = {"1": list(range(100)), "2": list(range(5))}

    def fake_get(path, params, token):
        return {"total": 105, "issues": pages[params["p"]]}

    monkeypatch.setattr(gate, "_get", fake_get)
    assert len(gate._paged("/api/issues/search", {}, "", "issues")) == 105


def test_exit_codes(monkeypatch, capsys, tmp_path: Path) -> None:
    props = tmp_path / "s.properties"
    props.write_text("sonar.projectKey=k\n")
    monkeypatch.setattr(gate, "findings", lambda *a: [])
    assert gate.main(["--pull-request", "9", "--properties", str(props)]) == 0
    monkeypatch.setattr(gate, "findings", lambda *a: ["issue x"])
    assert gate.main(["--branch", "main", "--properties", str(props)]) == 1
    assert "1 finding(s)" in capsys.readouterr().out

    def boom(*a):
        raise OSError("down")

    monkeypatch.setattr(gate, "findings", boom)
    assert gate.main(["--branch", "main", "--properties", str(props)]) == 2

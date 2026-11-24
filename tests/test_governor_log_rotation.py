"""governor.log rotation: bounded size, one gzip generation, failures never stop logging."""

from __future__ import annotations

import gzip

from heat_governor.cli._commands.governing import _file_logger


def _read_gz(path):
    with gzip.open(path, "rb") as fh:
        return fh.read().decode("utf-8")


def test_log_never_exceeds_cap_plus_one_line(tmp_path, capsys):
    log_path = tmp_path / "governor.log"
    log = _file_logger(log_path, 100)
    line = "x" * 29  # 30 bytes with newline
    for i in range(50):
        log(line)
        assert log_path.stat().st_size <= 100 + len(line) + 1
    assert (tmp_path / "governor.log.1.gz").exists()
    capsys.readouterr()


def test_rotation_keeps_previous_contents(tmp_path, capsys):
    log_path = tmp_path / "governor.log"
    log = _file_logger(log_path, 50)
    log("a" * 29)
    log("b" * 29)  # size 30 < 50: appended
    log("c" * 29)  # size 60 >= 50: rotate, then append
    assert _read_gz(tmp_path / "governor.log.1.gz") == "a" * 29 + "\n" + "b" * 29 + "\n"
    assert log_path.read_text() == "c" * 29 + "\n"
    assert not (tmp_path / "governor.log.1.gz.tmp").exists()
    capsys.readouterr()


def test_second_rotation_replaces_first(tmp_path, capsys):
    log_path = tmp_path / "governor.log"
    log = _file_logger(log_path, 20)
    log("first-line-xxxxxxxxxxxxxxxx")
    log("second-line-xxxxxxxxxxxxxxx")  # rotates first
    log("third-line-xxxxxxxxxxxxxxxx")  # rotates second
    assert _read_gz(tmp_path / "governor.log.1.gz") == "second-line-xxxxxxxxxxxxxxx\n"
    assert not (tmp_path / "governor.log.2.gz").exists()
    capsys.readouterr()


def test_cap_may_be_a_callable(tmp_path, capsys):
    log_path = tmp_path / "governor.log"
    cap = {"v": 10_000}
    log = _file_logger(log_path, lambda: cap["v"])
    log("a" * 29)
    log("b" * 29)
    assert not (tmp_path / "governor.log.1.gz").exists()
    cap["v"] = 10
    log("c" * 29)
    assert _read_gz(tmp_path / "governor.log.1.gz") == "a" * 29 + "\n" + "b" * 29 + "\n"
    capsys.readouterr()


def test_rotation_failure_goes_to_stderr_and_logging_continues(tmp_path, capsys, monkeypatch):
    import os

    log_path = tmp_path / "governor.log"
    log = _file_logger(log_path, 20)
    log("a" * 29)

    def boom(*_a, **_k):
        raise OSError("disk says no")

    monkeypatch.setattr(os, "replace", boom)
    log("b" * 29)
    err = capsys.readouterr().err
    assert "rotat" in err and "disk says no" in err
    assert log_path.read_text().endswith("b" * 29 + "\n")
    assert not (tmp_path / "governor.log.1.gz.tmp").exists()
    log("c" * 29)
    assert log_path.read_text().endswith("c" * 29 + "\n")

"""SSH transport tests: a fake ssh executable stands in for the network."""

from __future__ import annotations

import json
import shlex
import stat
import sys
import time
from pathlib import Path

import pytest

from heat_governor.cli._errors import CliError
from heat_governor.transport import SshTransport, Unreachable

DOC = {"level": 1, "temp_c": 55.0}

FAKE = """#!{py}
import json, os, sys, time
log = os.environ["FAKE_SSH_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
mode = os.environ.get("FAKE_SSH_MODE", "ok")
if "-O" in sys.argv:
    sys.exit(0)
if mode == "hang":
    time.sleep(60)
elif mode == "fail":
    sys.stderr.write("ssh: connect to host x port 22: Connection refused\\n")
    sys.exit(255)
elif mode == "empty":
    sys.exit(0)
elif mode == "junk":
    print("not json")
elif mode == "flood":
    # a broken or hostile remote: stream output until killed
    chunk = b"x" * 65536
    with open(os.environ["FAKE_SSH_PIDFILE"], "w") as f:
        f.write(str(os.getpid()))
    while True:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
else:
    print(json.dumps({doc}))
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    path = tmp_path / "fakebin" / "ssh"
    path.parent.mkdir()
    path.write_text(FAKE.format(py=sys.executable, doc=repr(DOC)))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return str(path), log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def make(fake, tmp_path, host="spark", timeout_s=2.0):
    return SshTransport(host, timeout_s=timeout_s, state_dir=tmp_path / "state", ssh=fake[0])


def test_argv_shape(fake, tmp_path):
    t = make(fake, tmp_path)
    assert t.fetch() == DOC
    argv = calls(fake[1])[0]
    assert "BatchMode=yes" in argv
    assert any(a.startswith("ConnectTimeout=") for a in argv)
    assert any(a.startswith("ServerAliveInterval=") for a in argv)
    assert "ControlMaster=auto" in argv
    assert any(a.startswith("ControlPersist=") for a in argv)
    cp = [a for a in argv if a.startswith("ControlPath=")]
    assert len(cp) == 1
    sock = cp[0].split("=", 1)[1]
    assert sock.startswith(str(tmp_path / "state" / "ssh"))
    assert len(sock) - len(str(tmp_path)) < 40  # tmp_path itself is long
    assert "spark" not in sock
    i = argv.index("--")
    assert argv[i + 1 :] == ["spark", "curl -s -m 2 127.0.0.1:9476/state"]
    assert (tmp_path / "state" / "ssh").stat().st_mode & 0o777 == 0o700


def test_invalid_host_rejected(fake, tmp_path):
    with pytest.raises(CliError):
        make(fake, tmp_path, host="-oProxyCommand=evil")
    assert not fake[1].exists()


def test_hang_is_bounded(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "hang")
    t = make(fake, tmp_path, timeout_s=0.6)
    start = time.monotonic()
    with pytest.raises(Unreachable) as exc:
        t.fetch()
    assert time.monotonic() - start < 3.0
    assert "timed out" in str(exc.value)


@pytest.mark.parametrize("mode", ["fail", "empty", "junk"])
def test_bad_outputs_are_unreachable(fake, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_SSH_MODE", mode)
    transport_ = make(fake, tmp_path)
    with pytest.raises(Unreachable) as exc:
        transport_.fetch()
    assert str(exc.value)


def test_missing_ssh_binary(tmp_path):
    t = SshTransport("spark", timeout_s=1, state_dir=tmp_path, ssh=str(tmp_path / "nope"))
    with pytest.raises(Unreachable):
        t.fetch()


def test_close_tears_down_and_is_idempotent(fake, tmp_path):
    with make(fake, tmp_path) as t:
        t.fetch()
        sock = Path(t.control_path)
        sock.write_text("")  # stand-in for the master's socket
    argv = calls(fake[1])
    exits = [a for a in argv if "-O" in a]
    assert len(exits) == 1
    assert exits[0][exits[0].index("-O") + 1] == "exit"
    assert f"ControlPath={sock}" in exits[0]
    assert exits[0][-2:] == ["--", "spark"]
    assert not sock.exists()
    t.close()
    assert len([a for a in calls(fake[1]) if "-O" in a]) == 1


# -- records history (d2) ---------------------------------------------------------

EXEC_FAKE = """#!{py}
import json, os, subprocess, sys
with open(os.environ["FAKE_SSH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if "-O" in sys.argv:
    sys.exit(0)
# behave like sshd: the remote command string goes to a shell
sys.exit(subprocess.call(["sh", "-c", sys.argv[-1]]))
"""


@pytest.fixture
def exec_fake(tmp_path, monkeypatch):
    path = tmp_path / "fakebin" / "ssh"
    path.parent.mkdir()
    path.write_text(EXEC_FAKE.format(py=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return str(path), log


def test_history_argv_is_the_fixed_niced_command(exec_fake, tmp_path, monkeypatch):
    from heat_governor.history import HISTORY_SCRIPT

    home = tmp_path / "xdg"
    rd = home / "heat-governor" / "records"
    rd.mkdir(parents=True)
    t0 = 1_794_000_000.0
    day = time.strftime("%Y%m%d", time.gmtime(t0 + 10))
    (rd / f"{day}.jsonl").write_text(
        json.dumps({"t": t0 + 10, "temp_c": 61.0, "level": "warm", "gpu": {}, "system": {}}) + "\n"
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    t = make(exec_fake, tmp_path)
    out = t.history(t0, t0 + 100, 10)
    assert [s["temp_c"] for s in out["samples"]] == [61.0]
    argv = calls(exec_fake[1])[0]
    assert f"ControlPath={t.control_path}" in argv
    assert "BatchMode=yes" in argv
    i = argv.index("--")
    remote = f"nice -n 19 python3 -c {shlex.quote(HISTORY_SCRIPT)} {t0:.3f} {t0 + 100:.3f} 10"
    assert argv[i + 1 :] == ["spark", remote]


@pytest.mark.parametrize(
    "args", [("1; rm -rf /", 2.0, 10), (float("nan"), 2.0, 10), (1.0, 2.0, "10 && x")]
)
def test_history_refuses_non_numeric_before_any_process(exec_fake, tmp_path, args):
    t = make(exec_fake, tmp_path)
    with pytest.raises(Unreachable):
        t.history(*args)
    assert not exec_fake[1].exists()


@pytest.mark.parametrize("mode", ["fail", "junk", "empty"])
def test_history_bad_outputs_are_unreachable(fake, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_SSH_MODE", mode)
    transport_ = make(fake, tmp_path)
    with pytest.raises(Unreachable):
        transport_.history(1.0, 100.0, 10)


def test_history_hang_is_bounded(fake, tmp_path, monkeypatch):
    import heat_governor.transport as transport

    monkeypatch.setenv("FAKE_SSH_MODE", "hang")
    monkeypatch.setattr(transport, "timeout_for", lambda span: 0.5)
    start = time.monotonic()
    transport_ = make(fake, tmp_path)
    with pytest.raises(Unreachable) as exc:
        transport_.history(1.0, 100.0, 10)
    assert time.monotonic() - start < 3.0
    assert "timed out" in str(exc.value)


# -- output caps (qodo #4) --------------------------------------------------------


def _assert_killed(pidfile: Path) -> None:
    import os

    pid = int(pidfile.read_text())
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"flooding child {pid} still alive")


def test_fetch_output_is_capped_and_the_child_killed(fake, tmp_path, monkeypatch):
    pidfile = tmp_path / "flood.pid"
    monkeypatch.setenv("FAKE_SSH_MODE", "flood")
    monkeypatch.setenv("FAKE_SSH_PIDFILE", str(pidfile))
    t = make(fake, tmp_path, timeout_s=8.0)
    start = time.monotonic()
    with pytest.raises(Unreachable) as exc:
        t.fetch()
    assert time.monotonic() - start < 4.0  # stopped at the cap, not at the 8 s deadline
    assert "too large" in str(exc.value)
    _assert_killed(pidfile)


def test_history_output_is_capped_and_the_child_killed(fake, tmp_path, monkeypatch):
    import heat_governor.transport as transport

    pidfile = tmp_path / "flood.pid"
    monkeypatch.setenv("FAKE_SSH_MODE", "flood")
    monkeypatch.setenv("FAKE_SSH_PIDFILE", str(pidfile))
    monkeypatch.setattr(transport, "timeout_for", lambda span: 8.0)
    t = make(fake, tmp_path)
    start = time.monotonic()
    with pytest.raises(Unreachable) as exc:
        t.history(1.0, 100.0, 10)
    assert time.monotonic() - start < 4.0
    assert "too large" in str(exc.value)
    _assert_killed(pidfile)


def test_output_at_the_cap_is_accepted(exec_fake, tmp_path, monkeypatch):
    import heat_governor.transport as transport

    body = json.dumps({"level": "cool", "pad": "y" * 5000})
    monkeypatch.setattr(transport, "MAX_DOC_BYTES", len(body) + 1)  # + print's newline
    t = make(exec_fake, tmp_path)
    monkeypatch.setattr(t, "argv", lambda: [t.ssh, "--", t.host, f"echo '{body}'"])
    assert t.fetch()["pad"] == "y" * 5000
    monkeypatch.setattr(transport, "MAX_DOC_BYTES", len(body))
    with pytest.raises(Unreachable, match="too large"):
        t.fetch()

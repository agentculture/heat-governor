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
elif mode == "echo":
    # stdin-echo: keep what arrived on stdin, answer with its size
    data = sys.stdin.buffer.read()
    with open(os.environ["FAKE_SSH_STDIN"], "wb") as f:
        f.write(data)
    print(json.dumps({{"stdin_len": len(data)}}))
elif mode == "exit3":
    sys.stdout.write("partial out\\n")
    sys.stderr.write("rule rejected: bad name\\n")
    sys.exit(3)
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


# -- boundaries commands with a body on stdin (t13) -------------------------------

BIN_DEFAULT = "~/.local/bin/heat-governor"
CFG = "/home/u/.config/heat-governor/config.toml"


def remote_of(argv: list[str]) -> str:
    return argv[argv.index("--") + 2]


@pytest.mark.parametrize("verb", ["list", "apply", "preview"])
def test_boundaries_remote_argv_is_fixed_and_body_free(fake, tmp_path, monkeypatch, verb):
    monkeypatch.setenv("FAKE_SSH_MODE", "echo")
    monkeypatch.setenv("FAKE_SSH_STDIN", str(tmp_path / "stdin"))
    t = make(fake, tmp_path)
    t.boundaries(verb, b'{"rules": []}', config_path=CFG)
    t.boundaries(verb, b'{"name": "$(rm -rf /); `x` \'; evil"}', config_path=CFG)
    first, second = calls(fake[1])
    assert first == second
    i = first.index("--")
    assert first[i + 1] == "spark"
    expected = f"~/.local/bin/heat-governor boundaries {verb} --config {shlex.quote(CFG)} --json"
    assert first[i + 2] == expected
    assert "evil" not in " ".join(first)


def test_boundaries_body_arrives_on_stdin(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "echo")
    monkeypatch.setenv("FAKE_SSH_STDIN", str(tmp_path / "stdin"))
    body = ('{"rules": [{"name": "caf\u00e9\\n"}]}' + " " * 300_000).encode()
    res = make(fake, tmp_path).boundaries("apply", body, config_path=CFG)
    assert res.code == 0
    assert json.loads(res.stdout) == {"stdin_len": len(body)}
    assert (tmp_path / "stdin").read_bytes() == body


def test_boundaries_nonzero_exit_returns_code_stdout_stderr(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "exit3")
    res = make(fake, tmp_path).boundaries("apply", b"{}", config_path=CFG)
    assert res.code == 3
    assert res.stdout == "partial out\n"
    assert res.stderr == "rule rejected: bad name\n"


@pytest.mark.parametrize("mode", ["hang"])
def test_boundaries_timeout_is_its_own_and_raises_unreachable(fake, tmp_path, monkeypatch, mode):
    import heat_governor.transport as transport

    monkeypatch.setenv("FAKE_SSH_MODE", mode)
    monkeypatch.setattr(transport, "BOUNDARIES_TIMEOUT_S", 0.5)
    t = make(fake, tmp_path, timeout_s=30.0)  # the fetch timeout must not apply
    start = time.monotonic()
    with pytest.raises(Unreachable, match="timed out"):
        t.boundaries("list", b"", config_path=CFG)
    assert time.monotonic() - start < 3.0


def test_boundaries_timeout_constant_is_separate_from_fetch():
    import heat_governor.transport as transport

    assert transport.BOUNDARIES_TIMEOUT_S > 5.0


def test_boundaries_default_and_custom_bin(fake, tmp_path):
    t = make(fake, tmp_path)
    t.boundaries("list", b"", config_path=CFG)
    custom = SshTransport(
        "spark",
        timeout_s=2.0,
        state_dir=tmp_path / "state",
        ssh=fake[0],
        bin_path="/opt/hg/bin/heat-governor",
    )
    custom.boundaries("list", b"", config_path=CFG)
    a, b = (remote_of(c) for c in calls(fake[1]))
    assert a.startswith(BIN_DEFAULT + " boundaries list --config ")
    assert b.startswith("/opt/hg/bin/heat-governor boundaries list --config ")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"verb": "delete"},
        {"verb": "list; id"},
        {"verb": "list", "config_path": "relative/config.toml"},
        {"verb": "list", "config_path": "/etc/x\nrm"},
        {"verb": "list", "config_path": ""},
        {"verb": "list", "body": "x" * (2 << 20)},
    ],
)
def test_boundaries_refuses_bad_input_before_any_process(fake, tmp_path, kwargs):
    args = {"verb": "list", "body": b"", "config_path": CFG, **kwargs}
    with pytest.raises(Unreachable):
        make(fake, tmp_path).boundaries(args["verb"], args["body"], config_path=args["config_path"])
    assert not fake[1].exists()


@pytest.mark.parametrize("bad", ["-oProxyCommand=x", "a b", "x;y", "$(x)", "../x", "rel/bin"])
def test_bad_bin_is_rejected_at_construction(fake, tmp_path, bad):
    with pytest.raises(CliError):
        SshTransport("spark", timeout_s=2.0, state_dir=tmp_path, ssh=fake[0], bin_path=bad)


def test_fetch_and_history_still_use_devnull_stdin(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "echo")
    monkeypatch.setenv("FAKE_SSH_STDIN", str(tmp_path / "stdin"))
    t = make(fake, tmp_path)
    t.fetch()
    assert (tmp_path / "stdin").read_bytes() == b""
    with pytest.raises(Unreachable):  # the echo answer is not a history document
        t.history(1.0, 100.0, 10)
    assert (tmp_path / "stdin").read_bytes() == b""


def test_boundaries_runs_the_real_remote_shell_string(exec_fake, tmp_path):
    """sshd hands the string to a shell: the tilde bin expands and a spaced config path
    stays one argument."""
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    tool = bin_dir / "heat-governor"
    tool.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))\n"
    )
    tool.chmod(0o755)
    import os

    os.environ["HOME"] = str(tmp_path / "home")
    try:
        res = make(exec_fake, tmp_path).boundaries(
            "preview", b"BODY", config_path="/tmp/a dir/config.toml"
        )
    finally:
        os.environ.pop("HOME", None)
    got = json.loads(res.stdout)
    assert got["argv"] == ["boundaries", "preview", "--config", "/tmp/a dir/config.toml", "--json"]
    assert got["stdin"] == "BODY"


# -- LocalTransport.boundaries ----------------------------------------------------


def test_local_boundaries_runs_the_same_command_with_stdin(tmp_path):
    from heat_governor.transport import LocalTransport

    tool = tmp_path / "hg"
    tool.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))\n"
        "sys.stderr.write('warn\\n')\nsys.exit(4)\n"
    )
    tool.chmod(0o755)
    t = LocalTransport(timeout_s=2.0, bin_path=str(tool))
    res = t.boundaries("apply", b"hello", config_path=CFG)
    got = json.loads(res.stdout)
    assert got == {"argv": ["boundaries", "apply", "--config", CFG, "--json"], "stdin": "hello"}
    assert res.code == 4
    assert res.stderr == "warn\n"


def test_local_boundaries_defaults_to_this_python(monkeypatch):
    import heat_governor.transport as transport

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw["input"]

        class P:
            returncode = 0
            stdout = b"{}"
            stderr = b""

        return P()

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    transport.LocalTransport(timeout_s=1.0).boundaries("list", b"x", config_path=CFG)
    assert seen["argv"][:3] == [sys.executable, "-m", "heat_governor"]
    assert seen["argv"][3:] == ["boundaries", "list", "--config", CFG, "--json"]
    assert seen["input"] == b"x"


def test_local_boundaries_timeout_is_unreachable(tmp_path, monkeypatch):
    import heat_governor.transport as transport
    from heat_governor.transport import LocalTransport

    tool = tmp_path / "hg"
    tool.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    tool.chmod(0o755)
    monkeypatch.setattr(transport, "BOUNDARIES_TIMEOUT_S", 0.4)
    with pytest.raises(Unreachable, match="timed out"):
        LocalTransport(timeout_s=1.0, bin_path=str(tool)).boundaries("list", b"", config_path=CFG)


def test_fleet_toml_bin_key(tmp_path):
    from heat_governor.fleet import load_fleet

    f = tmp_path / "fleet.toml"
    f.write_text(
        '[[machines]]\nname = "a"\nhost = "a.example"\nbin = "/opt/hg/heat-governor"\n'
        '[[machines]]\nname = "b"\nhost = "b.example"\n'
    )
    a, b = load_fleet(f).machines
    assert a.bin == "/opt/hg/heat-governor"
    assert b.bin == ""
    f.write_text('[[machines]]\nname = "a"\nhost = "a.example"\nbin = ""\n')
    with pytest.raises(CliError):
        load_fleet(f)


def test_local_boundaries_expands_a_home_relative_bin(monkeypatch):
    import subprocess

    from heat_governor import transport

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, b"{}", b"")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    monkeypatch.setenv("HOME", "/home/someone")
    t = transport.LocalTransport(timeout_s=1, bin_path="~/bin/heat-governor")
    t.boundaries("list", b"", config_path="/etc/hg/config.toml")
    assert seen["argv"][0] == "/home/someone/bin/heat-governor"

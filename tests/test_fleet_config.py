import dataclasses
from pathlib import Path

import pytest

from heat_governor import fleet
from heat_governor.cli._errors import EXIT_USER_ERROR, CliError

GOOD = """
poll_s = 5
timeout_s = 4

[[machines]]
name = "local-box"
transport = "local"

[[machines]]
name = "box-a"
host = "box-a.example.ts.net"
transport = "ssh"

[[machines]]
name = "box-b"
host = "ops@192.0.2.7"
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "fleet.toml"
    p.write_text(text)
    return p


def _machine(host: str) -> str:
    return f'[[machines]]\nname = "m"\nhost = {host!r}\ntransport = "ssh"\n'.replace("'", '"')


def test_loads_machines(tmp_path):
    cfg = fleet.load_fleet(_write(tmp_path, GOOD))
    assert [m.name for m in cfg.machines] == ["local-box", "box-a", "box-b"]
    assert cfg.machines[0].transport == "local" and cfg.machines[0].host == ""
    assert cfg.machines[1].host == "box-a.example.ts.net"
    assert cfg.machines[2].transport == "ssh"  # default
    assert (cfg.poll_s, cfg.timeout_s) == (5.0, 4.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.machines[0].name = "x"  # type: ignore[misc]


def test_default_path_beside_config(tmp_path):
    p = fleet.default_fleet_path({"HEAT_GOVERNOR_CONFIG": str(tmp_path / "sub" / "config.toml")})
    assert p == tmp_path / "sub" / "fleet.toml"
    p = fleet.default_fleet_path({"XDG_CONFIG_HOME": str(tmp_path)})
    assert p == tmp_path / "heat-governor" / "fleet.toml"


def test_missing_file_is_user_error(tmp_path):
    with pytest.raises(CliError) as e:
        fleet.load_fleet(tmp_path / "nope.toml")
    assert e.value.code == EXIT_USER_ERROR and e.value.remediation


@pytest.mark.parametrize(
    "host",
    [
        "-oProxyCommand=touch /tmp/x",
        "-oProxyCommand=x",
        "",
        "a b",
        "a;b",
        "a&b",
        "a|b",
        "a`b`",
        "$(x)",
        "a>b",
        "a\\b",
        "a'b",
        'a"b',
        "a*",
        "a?",
        "a[1]",
        "a{b}",
        "a!",
        "a#b",
        "~a",
        "a\nb",
        "-bad@host",
        "@host",
        "u;x@host",
        "host.",
        "a..b",
    ],
)
def test_bad_hosts_rejected(tmp_path, host):
    text = f'[[machines]]\nname = "m"\nhost = {fleet_toml_str(host)}\ntransport = "ssh"\n'
    with pytest.raises(CliError) as e:
        fleet.load_fleet(_write(tmp_path, text))
    assert e.value.code == EXIT_USER_ERROR
    assert e.value.remediation


def fleet_toml_str(s: str) -> str:
    esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{esc}"'


@pytest.mark.parametrize("host", ["h", "a-b.c.example.ts.net", "192.0.2.1", "2001:db8::1", "u@h"])
def test_good_hosts_accepted(tmp_path, host):
    text = f'[[machines]]\nname = "m"\nhost = "{host}"\ntransport = "ssh"\n'
    assert fleet.load_fleet(_write(tmp_path, text)).machines[0].host == host


@pytest.mark.parametrize(
    "text",
    [
        "",  # no machines
        'bogus = 1\n[[machines]]\nname="m"\ntransport="local"\n',
        '[[machines]]\nname="m"\ntransport="local"\nextra=1\n',
        '[[machines]]\nname="m"\ntransport="telnet"\nhost="h"\n',
        '[[machines]]\nname="m"\ntransport="ssh"\n',  # ssh needs host
        '[[machines]]\nname="m"\ntransport="local"\nhost="h"\n',  # local takes none
        '[[machines]]\ntransport="local"\n',  # name required
        '[[machines]]\nname="bad name"\ntransport="local"\n',
        '[[machines]]\nname="m"\ntransport="local"\n[[machines]]\nname="m"\ntransport="local"\n',
        'poll_s = 0\n[[machines]]\nname="m"\ntransport="local"\n',
        'timeout_s = 9999\n[[machines]]\nname="m"\ntransport="local"\n',
        'poll_s = "x"\n[[machines]]\nname="m"\ntransport="local"\n',
        "this is not toml [",
    ],
)
def test_invalid_files_rejected(tmp_path, text):
    with pytest.raises(CliError) as e:
        fleet.load_fleet(_write(tmp_path, text))
    assert e.value.code == EXIT_USER_ERROR and e.value.remediation


def test_example_doc_loads():
    root = Path(__file__).resolve().parent.parent
    cfg = fleet.load_fleet(root / "docs" / "fleet.example.toml")
    assert cfg.machines


def test_no_mesh_hostnames_in_package():
    root = Path(__file__).resolve().parent.parent / "heat_governor"
    import re

    pat = re.compile(r"\b(spark2?|thor|orin)\b", re.I)
    # allow platform-profile names (not hostnames) outside fleet.py
    text = (root / "fleet.py").read_text()
    assert not pat.search(text)

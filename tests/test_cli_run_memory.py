"""`run` passes [memory]/[ship] to the daemon even when live reload is off (deviation d2)."""

from heat_governor import config as cfgmod
from heat_governor.cli import _build_parser
from heat_governor.cli._commands.governing import _run_options

CONFIG = """
[daemon]
reload = {reload}

[memory]
action = "kill"
floor_pct = 6

[ship]
enabled = true
endpoint = "http://127.0.0.1:9000"
bucket = "heat-governor"
machine = "testbox"
"""


def _opts(tmp_path, reload: bool):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(reload="true" if reload else "false"))
    args = _build_parser().parse_args(
        ["run", "--config", str(path), "--data-dir", str(tmp_path / "data")]
    )
    return _run_options(args, cfgmod.load(path, required=True))


def test_reload_off_still_passes_memory_and_ship(tmp_path):
    opts = _opts(tmp_path, reload=False)
    assert opts.config is None  # live reload really is off
    assert opts.memory is not None and opts.memory.action == "kill"
    assert opts.memory.floor_pct == 6
    assert opts.ship is not None and opts.ship.machine == "testbox"


def test_reload_on_passes_the_same_rules(tmp_path):
    opts = _opts(tmp_path, reload=True)
    assert opts.config is not None
    assert opts.memory is not None and opts.memory.floor_pct == 6
    assert opts.ship is not None and opts.ship.bucket == "heat-governor"


def test_absent_sections_mean_no_guard_and_no_shipper(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[daemon]\nreload = false\n")
    args = _build_parser().parse_args(["run", "--config", str(path)])
    opts = _run_options(args, cfgmod.load(path, required=True))
    assert opts.memory is None and opts.ship is None

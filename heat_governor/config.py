"""Configuration: one TOML file the daemon re-reads while it runs.

Precedence, highest first: command-line flags, the config file, the built-in
platform profile. The file lives at ``$HEAT_GOVERNOR_CONFIG`` or
``~/.config/heat-governor/config.toml``; ``heat-governor config init`` writes a
commented starting point from the active profile.

Validation is strict on purpose: an unknown key is an error, so a typo
(``hot_C``) never silently leaves the default in force. A running daemon that
reads an invalid file keeps its last good configuration and logs why.

Layout::

    platform = "spark"            # optional; default: detected

    [profile]                     # any subset; see `heat-governor config show`
    warm_c = 88.0
    hot_c = 91.5
    crit_c = 93.0
    duty_curve = [[88, 0.0], [90, 0.3], [93, 1.0]]
    cpu_bands = [[85, 12], [88, 8], [90, 4]]
    concurrency = { cool = 1.0, warm = 0.5, hot = 0.0, critical = 0.0 }

    [daemon]
    poll_s = 1.0
    listen = "127.0.0.1:9476"
    apply = true

    [[containers]]                # for containers started without labels
    name = "model-gear-vllm-primary"
    role = "serving"              # batch | serving
    cpu_cap = true

    [[units]]                     # opt-in: CPUQuota on a systemd --user unit
    name = "app.slice"            # .slice | .service | .scope
    warm = 8                      # CPUs at each level (CPUQuota = cpus x 100%);
    hot = 4                       # omit all three for the platform's default
    critical = 2                  # (spark: 8 / 4 / 2, declared, unmeasured)
    dry_run = true                # optional: log only, change nothing

    [[units]]                     # or: a feedback loop that learns the unit's effect
    name = "app.slice"
    mode = "adaptive"             # levels (default) | adaptive; no warm/hot/critical
    min_cpus = 1                  # never cap below this
    engage = "warm"               # warm | hot | critical
    interval_s = 20               # one evaluation per interval (>= 5)
    target_rate_c_min = 0.0       # temperature rate to hold while engaged, C/min
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from heat_governor.platforms import Profile

ENV_CONFIG = "HEAT_GOVERNOR_CONFIG"
LEVELS = ("cool", "warm", "hot", "critical")

PROFILE_FLOATS = (
    "warm_c",
    "hot_c",
    "crit_c",
    "hysteresis_c",
    "dwell_s",
    "lookahead_s",
    "rate_window_s",
    "ramp_s",
    "duty_period_s",
    "duty_decay_per_s",
)
PROFILE_KEYS = set(PROFILE_FLOATS) | {
    "zones",
    "duty_curve",
    "cpu_bands",
    "gpu_clock_bands",
    "gpu_temp",
    "gpu",
    "concurrency",
}
DAEMON_KEYS = {
    "apply": bool,
    "poll_s": (int, float),
    "listen": str,
    "state": str,
    "data_dir": str,
    "use_labels": bool,
    "discover_every_s": (int, float),
    "record": bool,
    "fsync": bool,
    "reload": bool,
    "gpu_clock": bool,
}
CONTAINER_KEYS = {"name", "role", "cpu_cap"}
UNIT_LEVELS = ("warm", "hot", "critical")
UNIT_MODES = ("levels", "adaptive")
ADAPTIVE_KEYS = ("min_cpus", "engage", "interval_s", "target_rate_c_min")
UNIT_KEYS = {"name", "dry_run", "mode", *UNIT_LEVELS, *ADAPTIVE_KEYS}
# a unit name systemctl cannot read as an option: no leading "-", no spaces
UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.@\\-]*\.(slice|service|scope)")
TOP_KEYS = {"platform", "profile", "daemon", "containers", "units"}


class ConfigError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ContainerRule:
    name: str
    role: str
    cpu_cap: bool = False


@dataclass(frozen=True)
class UnitRule:
    """A systemd user unit to cap; ``bands`` are ``(level, cpus)`` pairs, empty =
    the platform's ``unit_bands``."""

    name: str
    bands: tuple[tuple[str, float], ...] = ()
    dry_run: bool = False  # log what would be done, change nothing (even with apply)
    mode: str = "levels"  # levels: fixed CPUs per level; adaptive: the learning loop
    min_cpus: float = 1.0  # adaptive: never cap below this
    engage: str = "warm"  # adaptive: the level at which the loop engages
    interval_s: float = 20.0  # adaptive: one evaluation per interval
    target_rate_c_min: float = 0.0  # adaptive: temperature rate to hold, C per minute


@dataclass
class Config:
    data: dict = field(default_factory=dict)
    path: Path | None = None
    mtime: float | None = None

    @property
    def platform(self) -> str | None:
        return self.data.get("platform")

    @property
    def profile(self) -> dict:
        return self.data.get("profile", {})

    @property
    def daemon(self) -> dict:
        return self.data.get("daemon", {})

    @property
    def containers(self) -> list[ContainerRule]:
        return [
            ContainerRule(c["name"], c["role"], bool(c.get("cpu_cap", False)))
            for c in self.data.get("containers", [])
        ]

    @property
    def units(self) -> list[UnitRule]:
        return [
            UnitRule(
                u["name"],
                tuple((lv, float(u[lv])) for lv in UNIT_LEVELS if lv in u),
                bool(u.get("dry_run", False)),
                u.get("mode", "levels"),
                float(u.get("min_cpus", 1.0)),
                u.get("engage", "warm"),
                float(u.get("interval_s", 20.0)),
                float(u.get("target_rate_c_min", 0.0)),
            )
            for u in self.data.get("units", [])
        ]


def default_config_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG])
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "heat-governor" / "config.toml"


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pairs(name: str, value: object, problems: list[str], *, int_second: bool = False) -> None:
    if not isinstance(value, list) or not value:
        problems.append(f"profile.{name} must be a non-empty list of [temp, value] pairs")
        return
    temps = []
    for item in value:
        if not (isinstance(item, list) and len(item) == 2 and all(_is_num(x) for x in item)):
            problems.append(f"profile.{name}: {item!r} is not a [number, number] pair")
            return
        if int_second and (int(item[1]) != item[1] or item[1] < 1):
            problems.append(f"profile.{name}: CPU count {item[1]!r} must be a whole number >= 1")
        if not int_second and not 0.0 <= item[1] <= 1.0:
            problems.append(f"profile.{name}: {item[1]!r} must be between 0 and 1")
        temps.append(item[0])
    if temps != sorted(temps):
        problems.append(f"profile.{name}: temperatures must ascend")


def _table(data: dict, key: str, problems: list[str], message: str) -> dict:
    """``data[key]`` as a dict, or ``{}`` after noting it isn't a table."""
    value = data.get(key, {})
    if isinstance(value, dict):
        return value
    problems.append(message)
    return {}


def _validate_profile_ranges(prof: dict, problems: list[str]) -> None:
    for k in ("dwell_s", "lookahead_s", "ramp_s", "duty_decay_per_s", "hysteresis_c"):
        if _is_num(prof.get(k)) and prof[k] < 0:
            problems.append(f"profile.{k} must be >= 0")
    for k in ("rate_window_s", "duty_period_s"):
        if _is_num(prof.get(k)) and prof[k] <= 0:
            problems.append(f"profile.{k} must be > 0")


def _validate_profile_scalars(prof: dict, problems: list[str]) -> None:
    for k, v in prof.items():
        if k not in PROFILE_KEYS:
            problems.append(f"unknown key profile.{k!r}")
        elif k in PROFILE_FLOATS and not _is_num(v):
            problems.append(f"profile.{k} must be a number")
    _validate_profile_ranges(prof, problems)
    if "zones" in prof and not (
        isinstance(prof["zones"], list) and all(isinstance(z, str) for z in prof["zones"])
    ):
        problems.append("profile.zones must be a list of zone type names")


def _validate_gpu_clock_bands(bands: object, problems: list[str]) -> None:
    _pairs("gpu_clock_bands", bands, problems, int_second=True)
    for item in bands if isinstance(bands, list) else []:
        if (
            isinstance(item, list)
            and len(item) == 2
            and _is_num(item[1])
            and not 100 <= item[1] <= 5000
        ):
            problems.append(f"profile.gpu_clock_bands: {item[1]} MHz is outside 100-5000")


def _validate_gpu_temp(g: object, problems: list[str]) -> None:
    if g is not False and not (
        isinstance(g, dict)
        and set(g) <= {"warm_c", "hot_c", "crit_c"}
        and all(_is_num(v) for v in g.values())
    ):
        problems.append(
            "profile.gpu_temp must be false or a table of warm_c / hot_c / crit_c numbers"
        )


def _validate_concurrency(conc: object, problems: list[str]) -> None:
    if not isinstance(conc, dict):
        problems.append("profile.concurrency must be a table of level = factor")
        return
    for k, v in conc.items():
        if k not in LEVELS:
            problems.append(f"profile.concurrency: unknown level {k!r}")
        elif not (_is_num(v) and 0.0 <= v <= 1.0):
            problems.append(f"profile.concurrency.{k} must be between 0 and 1")


def _validate_profile(prof: dict, problems: list[str]) -> None:
    _validate_profile_scalars(prof, problems)
    if "duty_curve" in prof:
        _pairs("duty_curve", prof["duty_curve"], problems)
    if "cpu_bands" in prof:
        _pairs("cpu_bands", prof["cpu_bands"], problems, int_second=True)
    if "gpu_clock_bands" in prof:
        _validate_gpu_clock_bands(prof["gpu_clock_bands"], problems)
    if "gpu_temp" in prof:
        _validate_gpu_temp(prof["gpu_temp"], problems)
    if "gpu" in prof and prof["gpu"] not in ("nvidia-smi", "jetson", "none"):
        problems.append("profile.gpu must be nvidia-smi, jetson or none")
    if "concurrency" in prof:
        _validate_concurrency(prof["concurrency"], problems)


def _validate_daemon(dmn: dict, problems: list[str]) -> None:
    for k, v in dmn.items():
        want = DAEMON_KEYS.get(k)
        if want is None:
            problems.append(f"unknown key daemon.{k!r} (known: {', '.join(sorted(DAEMON_KEYS))})")
        elif not isinstance(v, want) or (want is not bool and isinstance(v, bool)):
            problems.append(f"daemon.{k} has the wrong type")
    if _is_num(dmn.get("poll_s")) and not 0.1 <= dmn["poll_s"] <= 10:
        problems.append("daemon.poll_s must be between 0.1 and 10 seconds")
    if isinstance(dmn.get("listen"), str) and ":" not in dmn["listen"]:
        problems.append("daemon.listen must be HOST:PORT")


def _validate_container(i: int, c: dict, seen: set[str], problems: list[str]) -> None:
    for k in c:
        if k not in CONTAINER_KEYS:
            problems.append(f"containers[{i}]: unknown key {k!r}")
    if not isinstance(c.get("name"), str) or not c.get("name"):
        problems.append(f"containers[{i}]: name is required")
    elif c["name"] in seen:
        problems.append(f"containers[{i}]: {c['name']!r} is listed twice")
    else:
        seen.add(c["name"])
    if c.get("role") not in ("batch", "serving"):
        problems.append(f"containers[{i}]: role must be batch or serving")
    if "cpu_cap" in c and not isinstance(c["cpu_cap"], bool):
        problems.append(f"containers[{i}]: cpu_cap must be true or false")


def _validate_containers(conts: object, problems: list[str]) -> None:
    if not isinstance(conts, list):
        problems.append("containers must be an array of tables ([[containers]])")
        return
    seen: set[str] = set()
    for i, c in enumerate(conts):
        if isinstance(c, dict):
            _validate_container(i, c, seen, problems)
        else:
            problems.append(f"containers[{i}] must be a table")


def _validate_unit(i: int, u: dict, seen: set[str], problems: list[str]) -> None:
    for k in u:
        if k not in UNIT_KEYS:
            problems.append(
                f"units[{i}]: unknown key {k!r} (known: {', '.join(sorted(UNIT_KEYS))})"
            )
    name = u.get("name")
    if not isinstance(name, str) or not UNIT_NAME.fullmatch(name):
        problems.append(f"units[{i}]: name must be a unit name ending .slice, .service or .scope")
    if "dry_run" in u and not isinstance(u["dry_run"], bool):
        problems.append(f"units[{i}].dry_run must be true or false")
    if isinstance(name, str) and name in seen:
        problems.append(f"units[{i}]: {name!r} is listed twice")
    elif isinstance(name, str):
        seen.add(name)
    mode = u.get("mode", "levels")
    if mode not in UNIT_MODES:
        problems.append(f"units[{i}].mode must be levels or adaptive")
    elif mode == "adaptive":
        _validate_adaptive(i, u, problems)
    else:
        _validate_levels(i, u, problems)


def _validate_levels(i: int, u: dict, problems: list[str]) -> None:
    for k in ADAPTIVE_KEYS:
        if k in u:
            problems.append(f"units[{i}].{k} is allowed only with mode = adaptive")
    given = []
    for lv in UNIT_LEVELS:
        if lv not in u:
            continue
        if not _is_num(u[lv]):
            problems.append(f"units[{i}].{lv} must be a number of CPUs")
        elif u[lv] <= 0:
            problems.append(f"units[{i}].{lv} must be > 0 CPUs")
        else:
            given.append((lv, u[lv]))
    if any(b[1] > a[1] for a, b in zip(given, given[1:])):
        problems.append(f"units[{i}]: the CPU count must not rise from warm to hot to critical")


def _validate_adaptive(i: int, u: dict, problems: list[str]) -> None:
    for lv in UNIT_LEVELS:
        if lv in u:
            problems.append(
                f"units[{i}].{lv} is not allowed with mode = adaptive (the loop sizes the cap)"
            )
    if "min_cpus" in u and not (_is_num(u["min_cpus"]) and u["min_cpus"] > 0):
        problems.append(f"units[{i}].min_cpus must be a number of CPUs > 0")
    if "engage" in u and u["engage"] not in UNIT_LEVELS:
        problems.append(f"units[{i}].engage must be warm, hot or critical")
    if "interval_s" in u and not (_is_num(u["interval_s"]) and u["interval_s"] >= 5):
        problems.append(f"units[{i}].interval_s must be a number of seconds >= 5")
    if "target_rate_c_min" in u and not _is_num(u["target_rate_c_min"]):
        problems.append(f"units[{i}].target_rate_c_min must be a number (C per minute)")


def _validate_units(units: object, problems: list[str]) -> None:
    if not isinstance(units, list):
        problems.append("units must be an array of tables ([[units]])")
        return
    seen: set[str] = set()
    for i, u in enumerate(units):
        if isinstance(u, dict):
            _validate_unit(i, u, seen, problems)
        else:
            problems.append(f"units[{i}] must be a table")


def validate(data: dict) -> list[str]:
    """Every problem in the document, not just the first."""
    problems: list[str] = []
    for k in data:
        if k not in TOP_KEYS:
            problems.append(f"unknown top-level key {k!r} (known: {', '.join(sorted(TOP_KEYS))})")
    if "platform" in data and not isinstance(data["platform"], str):
        problems.append("platform must be a string")
    _validate_profile(_table(data, "profile", problems, "[profile] must be a table"), problems)
    _validate_daemon(_table(data, "daemon", problems, "[daemon] must be a table"), problems)
    _validate_containers(data.get("containers", []), problems)
    _validate_units(data.get("units", []), problems)
    return problems


def load(path: Path | None, *, required: bool = False) -> Config:
    """Read and validate; a missing optional file is an empty config."""
    if path is None or not path.exists():
        if required:
            raise ConfigError([f"no config file at {path}"])
        return Config(path=path)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError([f"{path}: {exc}"]) from exc
    problems = validate(data)
    if problems:
        raise ConfigError(problems)
    return Config(data=data, path=path, mtime=path.stat().st_mtime)


def changed(cfg: Config) -> bool:
    if cfg.path is None:
        return False
    try:
        m = cfg.path.stat().st_mtime
    except OSError:
        return cfg.mtime is not None  # deleted: back to the built-in profile
    return m != cfg.mtime


def diff(old: dict, new: dict, prefix: str = "") -> list[str]:
    """Human-readable keys whose values differ."""
    out = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if isinstance(a, dict) and isinstance(b, dict):
            out += diff(a, b, f"{prefix}{k}.")
        elif a != b:
            out.append(f"{prefix}{k}: {a!r} -> {b!r}")
    return out


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, float) and v.is_integer():
        return f"{v:.1f}"
    return str(v)


def template(profile: Profile) -> str:
    """A commented config that reproduces ``profile`` exactly; edit what you need."""
    lim = profile.limits
    conc = dict(zip(LEVELS, profile.concurrency))
    lines = [
        "# heat-governor configuration. Re-read by a running daemon within ~2 s.",
        "# Precedence: command-line flags > this file > built-in profile.",
        "# Check it with: heat-governor config check",
        "",
        f'platform = "{profile.name}"',
        "",
        "[profile]",
        f"# provenance of the built-in numbers: {profile.provenance}",
        f"zones = {_toml_value(list(profile.zones))}  # thermal-zone types to watch; [] = all",
        "# levels on the effective temperature (reading + lookahead x rising rate):",
        f"warm_c = {_toml_value(lim.warm_c)}   # start throttling batch work; half concurrency",
        f"hot_c = {_toml_value(lim.hot_c)}    # hold new work",
        f"crit_c = {_toml_value(lim.crit_c)}   # batch fully paused",
        f"hysteresis_c = {_toml_value(lim.hysteresis_c)}  # a level falls only this far below...",
        f"dwell_s = {_toml_value(lim.dwell_s)}       # ...for this long",
        f"lookahead_s = {_toml_value(profile.lookahead_s)}",
        f"rate_window_s = {_toml_value(profile.rate_window_s)}",
        "# share of each duty period a batch container is paused, by effective temperature:",
        f"duty_curve = {_toml_value([list(p) for p in profile.duty_curve])}",
        f"duty_period_s = {_toml_value(profile.duty_period_s)}",
        f"duty_decay_per_s = {_toml_value(profile.duty_decay_per_s)}  # how fast the duty may fall",
        "# CPU cap at or above each temperature (drops at once, rises one band per ramp_s):",
        f"cpu_bands = {_toml_value([list(b) for b in profile.cpu_bands])}",
        f"ramp_s = {_toml_value(profile.ramp_s)}  # also paces GPU clock rises",
        "# GPU clock ceiling (MHz) at or above each temperature; only with daemon.gpu_clock = true",
        "# and the root helper installed (heat-governor gpu-clock helper). Declared, unmeasured:",
        f"gpu_clock_bands = {_toml_value([list(b) for b in profile.gpu_clock_bands])}",
        "# allowed in-flight work for clients = base x factor (hot/critical 0 = hold):",
        f"concurrency = {_toml_value(conc)}",
        f'gpu = "{profile.gpu}"',
        "# the GPU's own temperature as a second input (false = off); whichever input is",
        "# closer to its own limit governs:",
        (
            "gpu_temp = "
            + _toml_value(
                {
                    "warm_c": profile.gpu_temp_limits.warm_c,
                    "hot_c": profile.gpu_temp_limits.hot_c,
                    "crit_c": profile.gpu_temp_limits.crit_c,
                }
            )
            if profile.gpu_temp_limits
            else "gpu_temp = false"
        ),
        "",
        "[daemon]",
        "# apply = false        # true: change containers (pause batch, cap CPUs)",
        "# poll_s = 1.0",
        '# listen = "127.0.0.1:9476"',
        "# use_labels = true    # govern containers labelled heat-governor.role=...",
        "# gpu_clock = false    # true: lower the GPU clock ceiling per gpu_clock_bands",
        "",
        "# Containers started without labels:",
        "# [[containers]]",
        '# name = "model-gear-vllm-primary"',
        '# role = "serving"      # batch: duty-cycled + CPU cap; serving: never paused',
        "# cpu_cap = true        # serving only: allow CPU caps",
        "",
        "# systemd --user units to CPU-cap by level (off unless listed). Every change is",
        "# `systemctl --user set-property --runtime` (a reboot drops it) and is undone on exit.",
        "# CPUs per level; omit warm/hot/critical for the platform default"
        + (
            " ("
            + " / ".join(f"{lv} {c:g}" for lv, c in profile.unit_bands)
            + ", declared, unmeasured):"
            if profile.unit_bands
            else " (none on this platform: give the bands):"
        ),
        "# [[units]]",
        '# name = "app.slice"     # interactive sessions run in app-*.scope under it',
        "# hot = 4",
        "# critical = 2",
        "# Or let a feedback loop size the cap from what it measures: it cuts the unit's CPU",
        "# while the temperature rises faster than the target, learns how much the rate falls",
        "# per CPU removed (kept in <data_dir>/adaptive.json) and releases as it cools:",
        "# [[units]]",
        '# name = "app.slice"',
        '# mode = "adaptive"      # levels (default) | adaptive; no warm/hot/critical here',
        "# min_cpus = 1",
        '# engage = "warm"        # warm | hot | critical',
        "# interval_s = 20        # one evaluation per interval, >= 5",
        "# target_rate_c_min = 0.0  # C per minute to hold while engaged (0 = flat)",
        "",
    ]
    return "\n".join(lines)

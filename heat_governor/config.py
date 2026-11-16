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
    gpu_ceiling_mhz = 2200        # optional: the operator's GPU ceiling -- law

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

    # or, INSTEAD of the levels entry above (a unit may be listed only once),
    # a feedback loop that learns the unit's effect:
    # [[units]]
    # name = "app.slice"
    # mode = "adaptive"           # levels (default) | adaptive; no warm/hot/critical
    # min_cpus = 1                # never cap below this
    # engage = "warm"             # warm | hot | critical
    # interval_s = 20             # one evaluation per interval (>= 5)
    # target_rate_c_min = 0.0     # temperature rate to hold while engaged, C/min

    [pair]                        # opt-in: one model across two boxes (TP=2)
    peer = "10.77.77.2:9476"      # the other box's daemon (its GET /state)
    containers = ["sparkrun_*"]   # names or fnmatch globs; governed as serving
    listen = "10.77.77.1:9476"    # optional: also serve /state here for the peer
    stale_after_s = 10            # peer silent this long -> one band hotter
    poll_s = 1.0                  # how often the peer is read
    cpu_cap = true                # CPU-cap pair containers (by the shared band)
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

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
    "gpu_ceiling_mhz": int,
}
CONTAINER_KEYS = {"name", "role", "cpu_cap"}
UNIT_LEVELS = ("warm", "hot", "critical")
UNIT_MODES = ("levels", "adaptive")
ADAPTIVE_KEYS = ("min_cpus", "engage", "interval_s", "target_rate_c_min")
UNIT_KEYS = {"name", "dry_run", "mode", *UNIT_LEVELS, *ADAPTIVE_KEYS}
# a unit name systemctl cannot read as an option: no leading "-", no spaces
UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.@\\-]*\.(slice|service|scope)")
PAIR_KEYS = {
    "peer": str,
    "containers": list,
    "listen": str,
    "stale_after_s": (int, float),
    "poll_s": (int, float),
    "cpu_cap": bool,
}
PAIR_RESTART_ONLY = ("listen",)  # a bound socket: changed only by a restart
MEMORY_ACTIONS = ("record", "kill")
MEMORY_KEYS = {
    "action": str,
    "floor_pct": (int, float),
    "horizon_s": (int, float),
    "soft_pct": (int, float),
    "window_s": (int, float),
    "min_victim_mib": int,
    "max_kills_per_hour": int,
    "protect": list,
    "poll_s": (int, float),
    "top_n": int,
    "top_every_s": (int, float),
    "floor_mib": int,
    "soft_mib": int,
    "allow": list,
    "act_over_pct": (int, float),
}
# [[memory.allow]]: a per-process (or per-set) memory boundary
ALLOW_KEYS = {
    "name": str,
    "comm": list,
    "cmdline": str,
    "cgroup": str,
    "max_mib": int,
    "scope": str,
}
ALLOW_SCOPES = ("process", "total")
ALLOW_MATCHERS = ("comm", "cmdline", "cgroup")
ALLOW_MIN_MIB = 64
# The page-managed sibling of config.toml: only [[memory.allow]] tables, nothing else.
BOUNDARIES_FILE = "boundaries.toml"
CONFIG_FILE = "config.toml"
SHIP_KEYS = {
    "enabled": bool,
    "endpoint": str,
    "region": str,
    "bucket": str,
    "prefix": str,
    "profile": str,
    "machine": str,
    "blackbox_every_s": (int, float),
    "blackbox_minutes": (int, float),
    "spool_dir": str,
}
# S3 bucket naming: 3-63 chars of lowercase letters, digits, hyphens, dots; alnum at both ends
BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]")
# Tailscale / carrier-grade NAT range: the RFC 6598 shared address space the tailnet uses
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # NOSONAR python:S1313 -- RFC 6598 policy constant
_SECONDS = " seconds"  # the unit suffix of range-check messages
TOP_KEYS = {"platform", "profile", "daemon", "containers", "units", "pair", "memory", "ship"}


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


@dataclass(frozen=True)
class PairRule:
    """One half of a tensor-parallel pair (``[pair]``): the peer daemon to read,
    and the containers (names or fnmatch globs) that are this box's rank."""

    peer: str
    containers: tuple[str, ...]
    listen: str | None = None
    stale_after_s: float = 10.0
    poll_s: float = 1.0
    cpu_cap: bool = True

    def matches(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pat) for pat in self.containers)


@dataclass(frozen=True)
class AllowRule:
    """``[[memory.allow]]``: an acceptable memory boundary for the processes it matches.

    Every matcher given must match (AND): ``comm`` names (comm, argv0 or exe basename),
    ``cmdline`` and ``cgroup`` fnmatch globs. ``scope = "process"`` judges each match on its
    own; ``"total"`` judges the sum over all matches."""

    name: str
    max_mib: int
    comm: tuple[str, ...] = ()
    cmdline: str | None = None
    cgroup: str | None = None
    scope: str = "process"
    # The file the rule came from. Never a TOML key: the loader sets it, not the user.
    source: str = CONFIG_FILE


@dataclass(frozen=True)
class MemoryRule:
    """``[memory]``: the memory guard.

    Absent section = no guard at all; present with the default action = record only.
    """

    action: str = "record"  # record | kill
    floor_pct: float = 5.0  # MemAvailable below this share of RAM is the floor
    horizon_s: float = 10.0  # act when the floor is projected to be hit within this
    soft_pct: float = 20.0  # below this share the guard watches closely
    window_s: float = 10.0  # span of the trend fit
    min_victim_mib: int = 2048  # never kill a process smaller than this
    max_kills_per_hour: int = 3
    protect: tuple[str, ...] = ()  # extra comm names, on top of the built-in list
    poll_s: float = 0.25
    top_n: int = 10  # processes recorded per top snapshot
    top_every_s: float = 10.0
    # Absolute limits (MiB), set together; they override floor_pct/soft_pct. Unset: the
    # platform profile's declared limits, else the percentages (deviation d3).
    floor_mib: int | None = None
    soft_mib: int | None = None
    # Boundaries ([[memory.allow]]); the tightest matching rule applies to a process.
    allow: tuple[AllowRule, ...] = ()
    # Far over: usage above max_mib x (1 + act_over_pct / 100) is acted on without a fire.
    act_over_pct: float = 50.0


@dataclass(frozen=True)
class ShipRule:
    """``[ship]``: ship records and blackbox dumps to S3-compatible storage."""

    enabled: bool = False
    endpoint: str | None = None
    region: str = "us-east-1"
    bucket: str | None = None
    prefix: str = "fleet"
    profile: str | None = None  # AWS credentials profile
    machine: str | None = None  # default: fleet.toml's transport = "local" entry, else shipping off
    blackbox_every_s: float = 60.0
    blackbox_minutes: float = 10.0
    spool_dir: str | None = None  # default: under the daemon's data dir


def _allow_rule(a: dict, source: str = CONFIG_FILE) -> AllowRule:
    return AllowRule(
        name=a["name"],
        max_mib=int(a["max_mib"]),
        comm=tuple(a.get("comm", ())),
        cmdline=a.get("cmdline"),
        cgroup=a.get("cgroup"),
        scope=a.get("scope", "process"),
        source=source,
    )


@dataclass
class Config:
    data: dict = field(default_factory=dict)
    path: Path | None = None
    # (config.toml mtime, boundaries.toml mtime); None for a file that is absent.
    mtime: tuple[float | None, float | None] = (None, None)
    # [[memory.allow]] tables from the sibling boundaries.toml, validated, not merged into data.
    boundaries: tuple[dict, ...] = ()
    # Why the last boundaries.toml edit was rejected (the previous rules stay in force).
    boundaries_error: str | None = None

    @property
    def boundaries_path(self) -> Path | None:
        return self.path.parent / BOUNDARIES_FILE if self.path else None

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

    @property
    def pair(self) -> PairRule | None:
        p = self.data.get("pair")
        if not p:
            return None
        return PairRule(
            p["peer"],
            tuple(p["containers"]),
            p.get("listen"),
            float(p.get("stale_after_s", 10.0)),
            float(p.get("poll_s", 1.0)),
            bool(p.get("cpu_cap", True)),
        )

    @property
    def memory(self) -> MemoryRule:
        m = self.data.get("memory", {})
        d = MemoryRule()
        return MemoryRule(
            m.get("action", d.action),
            float(m.get("floor_pct", d.floor_pct)),
            float(m.get("horizon_s", d.horizon_s)),
            float(m.get("soft_pct", d.soft_pct)),
            float(m.get("window_s", d.window_s)),
            int(m.get("min_victim_mib", d.min_victim_mib)),
            int(m.get("max_kills_per_hour", d.max_kills_per_hour)),
            tuple(m.get("protect", d.protect)),
            float(m.get("poll_s", d.poll_s)),
            int(m.get("top_n", d.top_n)),
            float(m.get("top_every_s", d.top_every_s)),
            m.get("floor_mib", d.floor_mib),
            m.get("soft_mib", d.soft_mib),
            tuple(_allow_rule(a) for a in m.get("allow", d.allow))
            + tuple(_allow_rule(a, BOUNDARIES_FILE) for a in self.boundaries),
            float(m.get("act_over_pct", d.act_over_pct)),
        )

    @property
    def ship(self) -> ShipRule:
        s = self.data.get("ship", {})
        d = ShipRule()
        return ShipRule(
            bool(s.get("enabled", d.enabled)),
            s.get("endpoint", d.endpoint),
            s.get("region", d.region),
            s.get("bucket", d.bucket),
            s.get("prefix", d.prefix),
            s.get("profile", d.profile),
            s.get("machine", d.machine),
            float(s.get("blackbox_every_s", d.blackbox_every_s)),
            float(s.get("blackbox_minutes", d.blackbox_minutes)),
            s.get("spool_dir", d.spool_dir),
        )


def default_config_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG])
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "heat-governor" / CONFIG_FILE


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
    if "gpu" in prof and prof["gpu"] not in ("nvidia-smi", "jetson", "devfreq", "none"):
        problems.append("profile.gpu must be nvidia-smi, jetson, devfreq or none")
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
    if isinstance(dmn.get("listen"), str) and host_port(dmn["listen"]) is None:
        problems.append("daemon.listen must be HOST:PORT (no scheme or path)")
    ceiling = dmn.get("gpu_ceiling_mhz")
    if _is_num(ceiling) and not 100 <= ceiling <= 5000:
        problems.append("daemon.gpu_ceiling_mhz must be > 0, a whole MHz within 100-5000")


def _typed_keys(section: str, tbl: dict, keys: dict, problems: list[str]) -> set[str]:
    """Flag unknown keys and wrong types; return the keys that are fine."""
    ok: set[str] = set()
    for k, v in tbl.items():
        want = keys.get(k)
        if want is None:
            problems.append(f"unknown key {section}.{k!r} (known: {', '.join(sorted(keys))})")
        elif not isinstance(v, want) or (want is not bool and isinstance(v, bool)):
            problems.append(f"{section}.{k} has the wrong type")
        else:
            ok.add(k)
    return ok


def _in_range(
    section: str, tbl: dict, ok: set[str], key: str, lo: float, hi: float, unit: str
) -> list[str]:
    if key in ok and not lo <= tbl[key] <= hi:
        return [f"{section}.{key} must be between {lo:g} and {hi:g}{unit}"]
    return []


def _validate_memory(mem: dict, problems: list[str]) -> None:
    ok = _typed_keys("memory", mem, MEMORY_KEYS, problems)
    if "action" in ok and mem["action"] not in MEMORY_ACTIONS:
        problems.append(f"memory.action must be one of {', '.join(MEMORY_ACTIONS)}")
    ranges = (
        ("floor_pct", 1, 50, "%"),
        ("soft_pct", 1, 100, "%"),
        ("horizon_s", 1, 120, _SECONDS),
        ("window_s", 2, 120, _SECONDS),
        ("poll_s", 0.1, 2, _SECONDS),
        ("top_every_s", 1, 3600, _SECONDS),
        ("max_kills_per_hour", 0, 60, ""),
        ("top_n", 1, 100, ""),
        ("act_over_pct", 1, 1000, "%"),
    )
    for key, lo, hi, unit in ranges:
        problems.extend(_in_range("memory", mem, ok, key, lo, hi, unit))
    if "min_victim_mib" in ok and mem["min_victim_mib"] < 64:
        problems.append("memory.min_victim_mib must be at least 64")
    if "protect" in ok and not all(isinstance(x, str) and x for x in mem["protect"]):
        problems.append("memory.protect must be a list of non-empty process names")
    floor = mem["floor_pct"] if "floor_pct" in ok else MemoryRule.floor_pct
    soft = mem["soft_pct"] if "soft_pct" in ok else MemoryRule.soft_pct
    if ("floor_pct" in ok or "soft_pct" in ok) and soft <= floor:
        problems.append("memory.soft_pct must be greater than memory.floor_pct")
    _validate_memory_mib(mem, ok, problems)
    if "allow" in ok:
        _validate_allow(mem["allow"], problems)


def _validate_allow(items: list, problems: list[str]) -> None:
    """[[memory.allow]]: each a table with a unique name, a matcher and max_mib."""
    seen: set[str] = set()
    for i, item in enumerate(items):
        where = f"memory.allow[{i}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be a table")
            continue
        ok = _typed_keys(where, item, ALLOW_KEYS, problems)
        _validate_allow_name(where, item, ok, seen, problems)
        _validate_allow_fields(where, item, ok, problems)


def _validate_allow_name(where: str, item: dict, ok: set, seen: set, problems: list[str]) -> None:
    if "name" not in item:
        problems.append(f"{where}.name is required")
    elif "name" not in ok:
        return
    elif not item["name"]:
        problems.append(f"{where}.name must be a non-empty string")
    elif item["name"] in seen:
        problems.append(f"{where}.name {item['name']!r} is a duplicate")
    else:
        seen.add(item["name"])


def _validate_allow_fields(where: str, item: dict, ok: set, problems: list[str]) -> None:
    if "max_mib" not in item:
        problems.append(f"{where}.max_mib is required")
    elif "max_mib" in ok and item["max_mib"] < ALLOW_MIN_MIB:
        problems.append(f"{where}.max_mib must be at least {ALLOW_MIN_MIB}")
    if "scope" in ok and item["scope"] not in ALLOW_SCOPES:
        problems.append(f"{where}.scope must be one of {', '.join(ALLOW_SCOPES)}")
    comm = item.get("comm")
    if "comm" in ok and not (comm and all(isinstance(x, str) and x for x in comm)):
        problems.append(f"{where}.comm must be a non-empty list of process names")
    for key in ("cmdline", "cgroup"):
        if key in ok and not item[key]:
            problems.append(f"{where}.{key} must be a non-empty glob")
    if not any(k in item for k in ALLOW_MATCHERS):
        problems.append(f"{where} needs at least one of {', '.join(ALLOW_MATCHERS)}")
    elif _matches_everything(item, ok):
        problems.append(
            f"{where} matches every process: give a matcher that is not a bare wildcard"
        )


# Unlikely strings a glob is tried on: one that matches them all matches every process. A cgroup
# path always starts with "/", so its probes do too ("/*" is a match-all cgroup glob).
_PROBES = ("a", "Zq 7", "x-y_z.9 --k=v", "\u00e9[1]")


def _glob_matches_all(pattern: object, prefix: str = "") -> bool:
    if not isinstance(pattern, str) or not pattern:
        return False
    return all(fnmatch.fnmatchcase(prefix + probe, pattern) for probe in _PROBES)


def _matches_everything(item: dict, ok: set) -> bool:
    """True when the rule would hit all processes: no ``comm`` (names are compared exactly, so
    a ``comm`` list always narrows) and every glob given matches anything."""
    if "comm" in item:
        return False
    globs = [(k, "/" if k == "cgroup" else "") for k in ("cmdline", "cgroup") if k in item]
    return bool(globs) and all(k in ok and _glob_matches_all(item[k], pre) for k, pre in globs)


def allow_problems(items: list) -> list[str]:
    """Problems with a list of ``[[memory.allow]]`` tables (names ``memory.allow[i]``)."""
    problems: list[str] = []
    _validate_allow(items, problems)
    return problems


def boundaries_problems(data: dict) -> list[str]:
    """Problems with a parsed boundaries.toml: only ``[[memory.allow]]`` tables are allowed."""
    problems: list[str] = []
    for key in data:
        if key != "memory":
            problems.append(
                f"unknown top-level key {key!r} (boundaries.toml holds only [[memory.allow]])"
            )
    mem = data.get("memory", {})
    if not isinstance(mem, dict):
        return problems + ["[memory] must be a table"]
    for key in mem:
        if key != "allow":
            problems.append(
                f"unknown key memory.{key} (boundaries.toml holds only [[memory.allow]])"
            )
    allow = mem.get("allow", [])
    if not isinstance(allow, list):
        return problems + ["memory.allow must be an array of tables"]
    return problems + allow_problems(allow)


def duplicate_allow_names(config_items: list, boundary_items: list) -> list[str]:
    """A rule name present in both config.toml and boundaries.toml, naming both files."""
    mine = {i.get("name") for i in config_items if isinstance(i, dict)}
    return [
        f"memory.allow name {i['name']!r} is defined in both {CONFIG_FILE} and {BOUNDARIES_FILE}"
        for i in boundary_items
        if isinstance(i, dict) and i.get("name") in mine
    ]


def _validate_memory_mib(mem: dict, ok: set, problems: list[str]) -> None:
    """floor_mib/soft_mib: absolute limits, set together, soft above floor."""
    have = {k for k in ("floor_mib", "soft_mib") if k in mem}
    if len(have) == 1:
        problems.append("memory.floor_mib and memory.soft_mib must be set together")
    if "floor_mib" in ok and mem["floor_mib"] < 256:
        problems.append("memory.floor_mib must be at least 256")
    if {"floor_mib", "soft_mib"} <= ok and mem["soft_mib"] <= mem["floor_mib"]:
        problems.append("memory.soft_mib must be greater than memory.floor_mib")


def endpoint_problem(url: str) -> str | None:
    """Why ``url`` is not an acceptable ship endpoint, or None. https: any host;
    http: only loopback or 100.64.0.0/10 (a private overlay), never the open internet."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError on a bad port
    except ValueError:
        return "is not a valid URL"
    if parts.scheme not in ("http", "https") or not host:
        return "must be an http:// or https:// URL with a host"
    if parts.scheme == "https" or host == "localhost":
        return None
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and (addr.is_loopback or addr in CGNAT):
        return None
    return "may use http:// only for a loopback or 100.64.0.0/10 host (use https://)"


def _validate_ship(ship: dict, problems: list[str]) -> None:
    ok = _typed_keys("ship", ship, SHIP_KEYS, problems)
    for key in ("region", "machine", "spool_dir", "profile"):
        if key in ok and not ship[key].strip():
            problems.append(f"ship.{key} must not be empty")
    problems.extend(_in_range("ship", ship, ok, "blackbox_every_s", 5, 3600, _SECONDS))
    problems.extend(_in_range("ship", ship, ok, "blackbox_minutes", 1, 120, " minutes"))
    if "endpoint" in ok:
        why = endpoint_problem(ship["endpoint"])
        if why:
            problems.append(f"ship.endpoint {why}")
    if "bucket" in ok and not BUCKET_NAME.fullmatch(ship["bucket"]):
        problems.append(
            "ship.bucket must be 3-63 characters: lowercase letters, digits, hyphens, dots"
        )
    if ship.get("enabled") is True:
        for key in ("endpoint", "bucket"):
            if key not in ship:
                problems.append(f"ship.{key} is required when ship.enabled is true")


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


# compared against, never bound: the hosts that listen on every interface
WILDCARD_HOSTS = ("0.0.0.0", "::", "")  # nosec B104


def host_port(spec: str, *, default_host: str | None = None) -> tuple[str, int] | None:
    """``"host:port"`` as ``(host, port)``; ``None`` when it is not one. The one
    HOST:PORT rule set for every address (``[daemon] listen``, ``[pair] peer`` /
    ``listen``, ``run --listen``): no scheme, no path, a port 1-65535, an IPv6
    host in brackets. An empty host is allowed only where ``default_host`` is given."""
    if not isinstance(spec, str) or "/" in spec or any(c.isspace() for c in spec):
        return None  # "http://h:1" or "h:1/state": an address, not a URL
    host, sep, port = spec.rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    if host.startswith("["):
        if not host.endswith("]") or len(host) < 3:
            return None
        host = host[1:-1]
    elif ":" in host:
        return None  # a bare IPv6 address is ambiguous: bracket it
    if not host:
        if default_host is None:
            return None
        host = default_host
    return host, int(port)


def listen_conflict(a: tuple[str, int], b: tuple[str, int]) -> str | None:
    """How two listen addresses relate: ``"same"`` (one socket serves both),
    ``"clash"`` (same port and one is a wildcard: the second bind fails with
    address in use), or ``None`` (independent)."""
    if a == b:
        return "same"
    if a[1] == b[1] and (a[0] in WILDCARD_HOSTS or b[0] in WILDCARD_HOSTS):
        return "clash"
    return None


def _validate_pair_keys(pair: dict, problems: list[str]) -> None:
    _validate_pair_types(pair, problems)
    _validate_pair_required(pair, problems)


def _validate_pair_types(pair: dict, problems: list[str]) -> None:
    for k, v in pair.items():
        want = PAIR_KEYS.get(k)
        if want is None:
            problems.append(f"unknown key pair.{k!r} (known: {', '.join(sorted(PAIR_KEYS))})")
        elif not isinstance(v, want) or (want is not bool and isinstance(v, bool)):
            problems.append(f"pair.{k} has the wrong type")
    for k in ("peer", "listen"):
        if isinstance(pair.get(k), str) and host_port(pair[k]) is None:
            problems.append(f"pair.{k} must be HOST:PORT (no scheme or path, e.g. 10.0.0.2:9476)")


def _validate_pair_required(pair: dict, problems: list[str]) -> None:
    if "peer" not in pair:
        problems.append("pair.peer is required (HOST:PORT of the peer daemon's state server)")
    conts = pair.get("containers")
    if conts is None:
        problems.append("pair.containers is required (names or globs of this box's rank)")
    elif isinstance(conts, list) and not (conts and all(isinstance(c, str) and c for c in conts)):
        problems.append("pair.containers must be a non-empty list of container names or globs")


def _validate_pair_listen(pair: dict, data: dict, problems: list[str]) -> None:
    dmn = data.get("daemon") if isinstance(data.get("daemon"), dict) else {}
    a = host_port(dmn["listen"]) if isinstance(dmn.get("listen"), str) else None
    b = host_port(pair["listen"]) if isinstance(pair.get("listen"), str) else None
    if a and b and listen_conflict(a, b) == "clash":
        problems.append(
            f"pair.listen {pair['listen']} and daemon.listen {dmn['listen']} share port {b[1]} "
            "and one is a wildcard: the second bind would fail (address in use). Drop "
            "pair.listen when daemon.listen is a wildcard (it already serves the peer), "
            "or use another port"
        )


def _validate_pair(pair: dict, data: dict, problems: list[str]) -> None:
    _validate_pair_keys(pair, problems)
    poll = pair.get("poll_s", 1.0)
    stale = pair.get("stale_after_s", 10.0)
    if _is_num(poll) and not 0.2 <= poll <= 10:
        problems.append("pair.poll_s must be between 0.2 and 10 seconds")
    if _is_num(stale) and _is_num(poll) and not 2 * poll <= stale <= 600:
        problems.append("pair.stale_after_s must be at least 2 x pair.poll_s and at most 600")
    _validate_pair_listen(pair, data, problems)
    pats = pair.get("containers")
    if not (isinstance(pats, list) and all(isinstance(c, str) for c in pats)):
        return
    for i, c in enumerate(data.get("containers") or []):
        name = c.get("name") if isinstance(c, dict) else None
        if (
            isinstance(name, str)
            and c.get("role") == "batch"
            and any(fnmatch.fnmatchcase(name, p) for p in pats)
        ):
            problems.append(
                f"containers[{i}]: {name!r} is a pair container and must not be batch "
                "(pausing one rank hangs the other)"
            )


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
    _validate_memory(_table(data, "memory", problems, "[memory] must be a table"), problems)
    _validate_ship(_table(data, "ship", problems, "[ship] must be a table"), problems)
    _validate_containers(data.get("containers", []), problems)
    _validate_units(data.get("units", []), problems)
    if "pair" in data:
        pair = _table(data, "pair", problems, "[pair] must be a table")
        _validate_pair(pair, data, problems)
    return problems


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError([f"{path}: {exc}"]) from exc


def load_boundaries(path: Path, config_data: dict | None = None) -> tuple[dict, ...]:
    """Read and validate a boundaries.toml; the rules, or ConfigError naming the file."""
    data = _read_toml(path)
    problems = [f"{path.name}: {p}" for p in boundaries_problems(data)]
    items = data.get("memory", {}).get("allow", []) if not problems else []
    mine = (config_data or {}).get("memory", {}).get("allow", [])
    problems += duplicate_allow_names(mine, items)
    if problems:
        raise ConfigError(problems)
    return tuple(items)


def _stat_mtime(path: Path | None) -> float | None:
    try:
        return path.stat().st_mtime if path else None
    except OSError:
        return None


def mtimes(path: Path | None) -> tuple[float | None, float | None]:
    """Current (config.toml, boundaries.toml) modification times beside ``path``."""
    if path is None:
        return (None, None)
    return (_stat_mtime(path), _stat_mtime(path.parent / BOUNDARIES_FILE))


def load(path: Path | None, *, required: bool = False, last_good: Config | None = None) -> Config:
    """Read and validate config.toml and the sibling boundaries.toml (beside ``path``).

    A missing optional file is an empty config. An invalid boundaries.toml raises, unless
    ``last_good`` is given: then its boundaries stay and the message goes in
    ``boundaries_error`` so a valid config.toml edit still applies."""
    if path is None or not path.exists():
        if required:
            raise ConfigError([f"no config file at {path}"])
        return Config(path=path)
    seen = mtimes(path)  # before the reads: a write landing meanwhile is reloaded, not missed
    data = _read_toml(path)
    problems = validate(data)
    if problems:
        raise ConfigError(problems)
    bpath = path.parent / BOUNDARIES_FILE
    boundaries: tuple[dict, ...] = ()
    error = None
    if bpath.exists():
        try:
            boundaries = load_boundaries(bpath, data)
        except ConfigError as exc:
            if last_good is None:
                raise
            # config.toml wins a name: a kept rule it now also defines is dropped, never doubled
            mine = {a.get("name") for a in data.get("memory", {}).get("allow", [])}
            boundaries = tuple(b for b in last_good.boundaries if b.get("name") not in mine)
            error = f"{bpath} rejected, keeping the last good boundaries: {exc}"
    return Config(
        data=data,
        path=path,
        mtime=seen,
        boundaries=boundaries,
        boundaries_error=error,
    )


def changed(cfg: Config) -> bool:
    if cfg.path is None:
        return False
    now = mtimes(cfg.path)
    if now[0] is None:
        return cfg.mtime[0] is not None  # deleted: back to the built-in profile
    return now != tuple(cfg.mtime)


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
        (
            f"gpu_clock_bands = {_toml_value([list(b) for b in profile.gpu_clock_bands])}"
            if profile.gpu_clock_bands
            # an empty list fails validation; profiles without bands (generic, ventunoq) get
            # a commented example instead
            else "# gpu_clock_bands = [[90.0, 2200], [93.0, 1400]]  # none for this platform"
        ),
        "# allowed in-flight work for clients = base x factor (hot/critical 0 = hold):",
        f"concurrency = {_toml_value(conc)}",
        '# how the GPU is read: "nvidia-smi" | "jetson" | "devfreq" | "none"',
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
        "# gpu_ceiling_mhz = 2200  # the operator's GPU ceiling: law, applied even with",
        "#                         # gpu_clock off; the daemon may go below it, never above",
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
        "# One model across two boxes (tensor parallel, e.g. sparkrun TP=2): each box runs a",
        "# daemon; the pair's containers are capped by the HOTTER box's band (one band hotter",
        "# than local when the peer is silent), and are never paused. Mirror it on the peer.",
        "# [pair]",
        '# peer = "10.77.77.2:9476"     # the peer daemon\'s state server (its [pair] listen)',
        '# containers = ["sparkrun_*"]  # names or fnmatch globs of this box\'s rank',
        '# listen = "10.77.77.1:9476"   # also serve GET /state here (restart to change)',
        "# stale_after_s = 10           # peer silent this long -> one band hotter",
        "# poll_s = 1.0",
        "# cpu_cap = true               # cap the pair containers by the shared band",
        "",
        "# Memory guard: sees an out-of-memory stall coming, records what ate the RAM, and",
        "# (action = kill) ends the biggest unprotected process. Off unless you uncomment it.",
        "# [memory]",
        '# action = "record"            # record (default, never kills) | kill',
        "# floor_pct = 5                # MemAvailable floor, % of RAM (1-50)",
        "# horizon_s = 10               # act when the floor is projected within this",
        "# soft_pct = 20                # watch closely below this % (> floor_pct)",
        "# floor_mib = 3072             # absolute limits, set together; they win over the %",
        "# soft_mib = 6144              # (spark/thor/orin/ventunoq profiles declare their own)",
        "# window_s = 10                # span of the trend fit (2-120)",
        "# min_victim_mib = 2048        # never kill a process smaller than this",
        "# max_kills_per_hour = 3       # 0-60",
        '# protect = ["sshd"]           # extra process names, on top of the built-in list',
        "# poll_s = 0.25                # 0.1-2",
        "# top_n = 10                   # processes per top snapshot",
        "# top_every_s = 10",
        "# act_over_pct = 50            # far over a boundary: > max_mib x (1 + this/100), 1-1000",
        "# Acceptable boundaries: a process within its boundary is never blamed or killed; over",
        "# it, it is the first victim when memory runs low; far over it, it is killed (own user,",
        "# same protections, min_victim_mib and budget) or recorded (root, containers, others)",
        "# even while memory is fine. Several rules may match: the smallest max_mib applies.",
        "# [[memory.allow]]",
        '# name = "vllm"                # required, unique',
        '# comm = ["vllm", "python3"]   # exact names (comm, argv0 or exe basename)',
        '# cmdline = "*vllm*serve*"     # fnmatch glob over the command line',
        '# cgroup = "*docker-*.scope"   # fnmatch glob over the cgroup path',
        "# max_mib = 102400             # required, >= 64",
        '# scope = "process"            # process (each match alone, default) | total (sum)',
        "",
        "# Ship records and blackbox dumps to S3-compatible storage (off unless enabled).",
        "# http:// endpoints are accepted only for loopback or 100.64.0.0/10 hosts.",
        "# [ship]",
        "# enabled = false",
        '# endpoint = "https://s3.example.org"',
        '# region = "us-east-1"',
        '# bucket = "my-fleet-bucket"   # 3-63 chars: lowercase, digits, hyphens, dots',
        '# prefix = "fleet"',
        '# profile = "default"          # credentials profile',
        '# machine = "spark-1"          # default: fleet.toml transport = "local" entry; else off',
        "# blackbox_every_s = 60        # 5-3600",
        "# blackbox_minutes = 10        # 1-120",
        '# spool_dir = "/var/lib/heat-governor/spool"  # default: under data_dir',
        "",
    ]
    return "\n".join(lines)

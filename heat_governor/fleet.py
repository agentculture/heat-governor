"""fleet.toml loader: the machines a fleet view polls, strictly validated.

``fleet.toml`` sits beside ``config.toml`` (or at an explicit path). Hosts end up in
``ssh`` argv, so they are validated hard: no leading ``-``, no whitespace, no shell
metacharacters. Every failure is a :class:`CliError` with exit code 1 and a hint.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from heat_governor.cli._errors import EXIT_USER_ERROR, CliError
from heat_governor.config import ENV_CONFIG

TRANSPORTS = ("ssh", "local")
TOP_KEYS = frozenset({"poll_s", "timeout_s", "machines"})
MACHINE_KEYS = frozenset({"name", "host", "transport"})
POLL_BOUNDS = (1.0, 3600.0)
TIMEOUT_BOUNDS = (0.5, 120.0)
DEFAULT_POLL_S = 10.0
DEFAULT_TIMEOUT_S = 5.0

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
_V6_RE = re.compile(r"^[0-9A-Fa-f:]+$")
_HINT = "see docs/fleet.example.toml for the fleet.toml format"


@dataclass(frozen=True)
class Machine:
    name: str
    transport: str = "ssh"
    host: str = ""


@dataclass(frozen=True)
class Fleet:
    machines: tuple[Machine, ...]
    poll_s: float = DEFAULT_POLL_S
    timeout_s: float = DEFAULT_TIMEOUT_S


def _fail(message: str, remediation: str = _HINT) -> CliError:
    return CliError(EXIT_USER_ERROR, message, remediation)


def default_fleet_path(environ: dict[str, str] | None = None) -> Path:
    """fleet.toml beside config.toml (honours HEAT_GOVERNOR_CONFIG and XDG_CONFIG_HOME)."""
    env = os.environ if environ is None else environ
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG]).parent / "fleet.toml"
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "heat-governor" / "fleet.toml"


def _valid_hostname(h: str) -> bool:
    if not h or len(h) > 253:
        return False
    return all(_LABEL_RE.match(label) for label in h.split("."))


def _valid_host_part(h: str) -> bool:
    if ":" in h:  # bracket-less IPv6
        return bool(_V6_RE.match(h)) and h.count(":") >= 2 and ":::" not in h
    return _valid_hostname(h)


def validate_host(host: object) -> str:
    """Return ``host`` if it is safe to place in ssh argv, else raise CliError."""
    if not isinstance(host, str) or not host:
        raise _fail("machine host must be a non-empty string")
    user, sep, rest = host.rpartition("@")
    ok = (_valid_host_part(rest) and (not sep or bool(_USER_RE.match(user)))) and not (
        host.startswith("-")
    )
    if not ok:
        raise _fail(
            f"invalid host {host!r}",
            "use a plain hostname, IPv4/IPv6 address, or user@host; no leading '-', "
            "spaces or shell metacharacters",
        )
    return host


def _bounded(top: dict, key: str, bounds: tuple[float, float], default: float) -> float:
    if key not in top:
        return default
    v = top[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _fail(f"{key} must be a number")
    if not bounds[0] <= v <= bounds[1]:
        raise _fail(f"{key} must be between {bounds[0]} and {bounds[1]} seconds")
    return float(v)


def _machine(raw: object, index: int) -> Machine:
    where = f"machines[{index}]"
    if not isinstance(raw, dict):
        raise _fail(f"{where} must be a table")
    unknown = sorted(set(raw) - MACHINE_KEYS)
    if unknown:
        raise _fail(f"{where}: unknown key(s) {', '.join(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _fail(f"{where}: name is required (letters, digits, '_', '.', '-')")
    transport = raw.get("transport", "ssh")
    if transport not in TRANSPORTS:
        raise _fail(f"{where}: transport must be one of {', '.join(TRANSPORTS)}")
    if transport == "local":
        if raw.get("host"):
            raise _fail(f"{where}: a local machine takes no host")
        return Machine(name=name, transport="local")
    if "host" not in raw:
        raise _fail(f"{where}: an ssh machine needs a host")
    return Machine(name=name, transport="ssh", host=validate_host(raw["host"]))


def parse_fleet(data: dict) -> Fleet:
    unknown = sorted(set(data) - TOP_KEYS)
    if unknown:
        raise _fail(f"unknown top-level key(s): {', '.join(unknown)}")
    raw = data.get("machines")
    if not isinstance(raw, list) or not raw:
        raise _fail("fleet.toml must list at least one [[machines]] entry")
    machines = tuple(_machine(m, i) for i, m in enumerate(raw))
    names = [m.name for m in machines]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise _fail(f"duplicate machine name(s): {', '.join(dupes)}")
    return Fleet(
        machines=machines,
        poll_s=_bounded(data, "poll_s", POLL_BOUNDS, DEFAULT_POLL_S),
        timeout_s=_bounded(data, "timeout_s", TIMEOUT_BOUNDS, DEFAULT_TIMEOUT_S),
    )


def load_fleet(path: Path | str | None = None) -> Fleet:
    p = Path(path) if path is not None else default_fleet_path()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail(f"cannot read fleet file {p}: {exc.strerror or exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _fail(f"{p}: invalid TOML: {exc}") from exc
    return parse_fleet(data)

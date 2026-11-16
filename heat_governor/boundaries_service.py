"""The reader's boundaries service: the one path a write to a machine's memory boundaries takes.

The mesh HTTP server and ``heat-governor boundaries --machine M`` both call
:class:`BoundariesService`. It never edits a file itself: it runs the machine's own
``heat-governor boundaries VERB --config PATH --json`` through the machine's transport
(:meth:`~heat_governor.transport.SshTransport.boundaries`) and returns the plain dict that
command printed, with the same ``outcome`` vocabulary (``ok``, ``saved``, ``conflict``,
``would_kill``, ``invalid``) plus the service-level outcomes below.

Order of work for every verb: validate the request, look the machine up in the reader's
snapshot, then **gate** (machine reporting, version at least :data:`MIN_VERSION`, daemon
reports a config path); a refusal here happens before any transport call. ``apply`` then lists
the file (to learn the old rule for the audit line), writes an ``attempt`` audit line (if that
fails nothing is sent: outcome ``audit_failed``), applies, and appends the final audit line.

The request sent to the box is rebuilt from validated fields (``base_version``, ``operation``,
``rule`` with only the six rule keys, ``allow_kill``); nothing is forwarded raw.

Lost response: if ``apply`` raises ``Unreachable`` (the body may have been delivered) the file
is listed again. A changed version whose rules reflect the request is ``saved`` with
``recovered: true``; an unchanged version is ``not_saved``; a changed version that does not
reflect the request is ``conflict``; an unreadable file is ``unknown``. Repeating a request whose
requested state already holds gets a conflict from the box, which the service reports as
``saved`` with ``idempotent: true`` (nothing is written).

Audit log (``boundaries-audit.jsonl`` in the reader state dir, mode 0600, append-only): one JSON
object per line with ``time``, ``label``, ``machine``, ``operation``, ``old``, ``new`` and
``outcome``. A write that reaches the box has an ``attempt`` line first and the final line
after; a request refused by the gate has the final line only. Rules are reduced to the six rule
keys; no token and no process data is ever written. The old rule is the one the list just before
the apply showed, so a write racing another one may name the other's rule.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from heat_governor.cli._errors import CliError
from heat_governor.state import default_data_dir
from heat_governor.transport import Unreachable

MIN_VERSION = (0, 19, 0)  # the first release with the boundaries command
MIN_VERSION_TEXT = ".".join(str(n) for n in MIN_VERSION)
AUDIT_FILE = "boundaries-audit.jsonl"
RULE_KEYS = ("name", "comm", "cmdline", "cgroup", "max_mib", "scope")
OPERATIONS = ("add", "set", "rm")
BOUNDARIES_SOURCE = "boundaries.toml"

# service-level outcomes (the box's own: ok, saved, conflict, would_kill, invalid)
UNKNOWN_MACHINE = "unknown_machine"
MACHINE_UNREACHABLE = "machine_unreachable"  # not reporting, or the transport failed
VERSION_TOO_OLD = "version_too_old"
NO_CONFIG_PATH = "no_config_path"
BAD_REPLY = "bad_reply"
BOX_ERROR = "box_error"  # the box's command exited 2 (environment error)
AUDIT_FAILED = "audit_failed"
NOT_SAVED = "not_saved"  # lost response, and the file is unchanged
UNKNOWN = "unknown"  # lost response, and the file could not be read afterwards

# a suggested HTTP status per outcome, for the mesh server
HTTP_STATUS = {
    "ok": 200,
    "saved": 200,
    "conflict": 409,
    "would_kill": 409,
    "invalid": 400,
    UNKNOWN_MACHINE: 404,
    MACHINE_UNREACHABLE: 502,
    VERSION_TOO_OLD: 422,
    NO_CONFIG_PATH: 422,
    BAD_REPLY: 502,
    BOX_ERROR: 502,
    AUDIT_FAILED: 500,
    NOT_SAVED: 502,
    UNKNOWN: 504,
}
BOX_OUTCOMES = {"ok", "saved", "conflict", "would_kill", "invalid"}
_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:[.+_a-zA-Z-].*)?$")
_LABEL_MAX = 200
_MSG_MAX = 300

Snapshot = Callable[[], dict]
TransportFor = Callable[[str], Any]


def http_status(result: dict) -> int:
    """The suggested HTTP status for a service result (500 for an outcome not in the table)."""
    return HTTP_STATUS.get(result.get("outcome"), 500)


def parse_version(text: object) -> tuple[int, int, int] | None:
    """``0.19.0``, ``0.19.0.dev3``, ``0.19.0rc1`` or ``0.19.0+x`` -> (0, 19, 0); else None."""
    m = _VERSION_RE.match(text) if isinstance(text, str) else None
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def _clip(text: object, n: int = _MSG_MAX) -> str:
    clean = "".join(c if c.isprintable() else " " for c in str(text))
    return clean[:n]


def pick_rule(rule: object) -> dict:
    """``rule`` reduced to the six rule keys (``comm`` as a list copy)."""
    if not isinstance(rule, dict):
        return {}
    out = {k: rule[k] for k in RULE_KEYS if k in rule}
    if isinstance(out.get("comm"), (list, tuple)):
        out["comm"] = list(out["comm"])
    return out


def _canon(rule: dict) -> dict:
    out = {"name": rule.get("name"), "comm": list(rule.get("comm") or [])}
    out["cmdline"], out["cgroup"] = rule.get("cmdline"), rule.get("cgroup")
    out["max_mib"], out["scope"] = rule.get("max_mib"), rule.get("scope") or "process"
    return out


def _find(rules: object, name: object) -> dict | None:
    for r in rules if isinstance(rules, list) else []:
        if isinstance(r, dict) and r.get("name") == name:
            return r
    return None


def reflects(operation: str, rule: dict, rules: object) -> bool:
    """True when ``rules`` (the boundaries rules on disk) already hold the requested state."""
    current = _find(rules, rule.get("name"))
    if operation == "rm":
        return current is None
    return current is not None and _canon(current) == _canon(rule)


def boundaries_rules(listing: dict) -> list[dict]:
    """The editable boundaries.toml rules of a ``list`` reply, reduced to the rule keys."""
    rules = listing.get("rules")
    return [
        pick_rule(r)
        for r in (rules if isinstance(rules, list) else [])
        if isinstance(r, dict) and r.get("source") == BOUNDARIES_SOURCE
    ]


def validate_request(request: object) -> tuple[dict, list[str]]:
    """``(clean request, problems)``; the clean one has only the validated fields."""
    if not isinstance(request, dict):
        return {}, ["request must be a JSON object"]
    allowed = {"base_version", "operation", "rule", "allow_kill"}
    problems = [f"unknown request key {k!r}" for k in sorted(set(request) - allowed)]
    if not isinstance(request.get("base_version"), str):
        problems.append("base_version must be a string")
    if request.get("operation") not in OPERATIONS:
        problems.append(f"operation must be one of {', '.join(OPERATIONS)}")
    if not isinstance(request.get("allow_kill", False), bool):
        problems.append("allow_kill must be true or false")
    problems += _rule_problems(request.get("rule"), request.get("operation"))
    clean = {
        "base_version": request.get("base_version"),
        "operation": request.get("operation"),
        "rule": pick_rule(request.get("rule")),
    }
    if request.get("allow_kill") is True:
        clean["allow_kill"] = True
    return clean, problems


def _rule_problems(rule: object, operation: object) -> list[str]:
    if not isinstance(rule, dict):
        return ["rule must be an object"]
    problems = [f"unknown rule key {k!r}" for k in sorted(set(rule) - set(RULE_KEYS))]
    if not isinstance(rule.get("name"), str) or not rule.get("name"):
        problems.append("rule.name must be a non-empty string")
    if operation == "rm" and set(rule) - {"name"}:
        problems.append('rm takes only a rule name: {"name": ...}')
    return problems


# -- audit ----------------------------------------------------------------------------------


class AuditLog:
    """Append-only JSON-lines file, mode 0600, opened with ``O_APPEND``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            raise
        return fd

    def append(self, entry: dict) -> None:
        """Write one line; ``OSError`` when the file cannot be created, fixed or written."""
        line = json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n"
        fd = self._open()
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    def check(self) -> None:
        """Open (and so create or fix the mode of) the file without writing."""
        os.close(self._open())


def default_audit_path(state_dir: Path | str | None = None) -> Path:
    return (Path(state_dir) if state_dir is not None else default_data_dir()) / AUDIT_FILE


# -- the service ----------------------------------------------------------------------------


def _msg(outcome: str, message: str, machine: str, **extra: object) -> dict:
    return {"outcome": outcome, "message": message, "machine": machine, **extra}


class BoundariesService:
    """``list`` / ``preview`` / ``apply`` for one machine of a fleet; see the module doc.

    ``snapshot`` returns the reader's snapshot dict (``FleetReader.snapshot``);
    ``transport_for(name)`` returns an object with ``boundaries(verb, body, *, config_path)``.
    """

    def __init__(
        self,
        snapshot: Snapshot,
        transport_for: TransportFor,
        *,
        audit_path: Path | str | None = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._snapshot = snapshot
        self._transport_for = transport_for
        self.audit = AuditLog(audit_path if audit_path is not None else default_audit_path())
        self._wall = wall

    # gate
    def _gate(self, machine: str) -> tuple[dict | None, str]:
        """(a refusal or None, the machine's config path)."""
        entries = [m for m in self._snapshot().get("machines", []) if m.get("name") == machine]
        if not entries:
            return _msg(UNKNOWN_MACHINE, f"no machine named {_clip(machine, 80)!r}", machine), ""
        entry = entries[0]
        thermal = entry.get("thermal")
        if not isinstance(thermal, dict) or entry.get("state") == "unreachable":
            return _msg(MACHINE_UNREACHABLE, f"{machine} is not reporting", machine), ""
        ver = thermal.get("version")
        parsed = parse_version(ver)
        if parsed is None or parsed < MIN_VERSION:
            shown = _clip(ver, 40) if isinstance(ver, str) and ver else "none"
            text = f"{machine} reports version {shown}; boundaries need {MIN_VERSION_TEXT} or newer"
            return _msg(VERSION_TOO_OLD, text, machine, needed_version=MIN_VERSION_TEXT), ""
        path = thermal.get("config")
        if not isinstance(path, str) or not path:
            return _msg(NO_CONFIG_PATH, f"{machine} reports no config path", machine), ""
        return None, path

    # transport
    def _call(self, machine: str, verb: str, body: dict | None, config_path: str) -> dict:
        """The box's reply dict, or a service-level refusal dict."""
        data = b"" if body is None else json.dumps(body).encode()
        try:
            res = self._transport_for(machine).boundaries(verb, data, config_path=config_path)
        except Unreachable as exc:
            return _msg(MACHINE_UNREACHABLE, _clip(exc.message), machine, lost=True)
        except CliError as exc:
            return _msg(MACHINE_UNREACHABLE, _clip(exc.message), machine)
        return self._parse(machine, res)

    @staticmethod
    def _parse(machine: str, res: Any) -> dict:
        try:
            doc = json.loads(res.stdout)
        except ValueError:
            doc = None
        if res.code == 2 and not isinstance(doc, dict):
            lines = res.stderr.strip().splitlines()
            return _msg(BOX_ERROR, _clip(lines[-1] if lines else "exit 2"), machine)
        if not isinstance(doc, dict) or doc.get("outcome") not in BOX_OUTCOMES:
            return _msg(BAD_REPLY, f"unexpected reply from {machine} (exit {res.code})", machine)
        return doc

    # verbs
    def list(self, machine: str) -> dict:
        refused, path = self._gate(machine)
        if refused:
            return refused
        out = self._call(machine, "list", None, path)
        return out

    def preview(self, machine: str, rule: object) -> dict:
        problems = _rule_problems(rule, "preview")
        if problems:
            return {"outcome": "invalid", "problems": problems}
        refused, path = self._gate(machine)
        if refused:
            return refused
        out = self._call(machine, "preview", {"rule": pick_rule(rule)}, path)
        return out

    def apply(self, machine: str, request: object, label: str) -> dict:
        clean, problems = validate_request(request)
        if problems:
            return {"outcome": "invalid", "problems": problems}
        tag = _clip(label, _LABEL_MAX)
        refused, path = self._gate(machine)
        if refused:
            return self._final(tag, machine, clean, None, refused)
        before = self._call(machine, "list", None, path)
        if before.get("outcome") != "ok":
            return self._final(tag, machine, clean, None, self._as_refusal(before, machine))
        old = _find(boundaries_rules(before), clean["rule"].get("name"))
        try:
            self.audit.append(self._line(tag, machine, clean, old, "attempt"))
        except OSError as exc:
            msg = f"cannot write the audit log ({_clip(exc.strerror or exc, 80)}); nothing sent"
            return _msg(AUDIT_FAILED, msg, machine)
        out = self._apply_remote(machine, clean, path)
        return self._final(tag, machine, clean, old, out)

    @staticmethod
    def _as_refusal(before: dict, machine: str) -> dict:
        if before.get("outcome") in HTTP_STATUS and before["outcome"] not in BOX_OUTCOMES:
            return before
        return _msg(BAD_REPLY, f"cannot read the rules of {machine} before the write", machine)

    @staticmethod
    def _boxes_rules(listing: dict) -> list[dict]:
        rules = listing.get("rules")
        return [
            {k: r[k] for k in RULE_KEYS if k in r}
            for r in (rules if isinstance(rules, list) else [])
            if isinstance(r, dict) and r.get("source") == BOUNDARIES_SOURCE
        ]

    # apply, lost response, idempotence
    def _apply_remote(self, machine: str, clean: dict, path: str) -> dict:
        out = self._call(machine, "apply", clean, path)
        if out.get("lost"):
            return self._recover(machine, clean, path, out)
        if out.get("outcome") == "conflict" and reflects(
            clean["operation"], clean["rule"], out.get("rules")
        ):
            return {**out, "outcome": "saved", "idempotent": True}
        return out

    def _recover(self, machine: str, clean: dict, path: str, lost: dict) -> dict:
        after = self._call(machine, "list", None, path)
        if after.get("outcome") != "ok" or not isinstance(after.get("version"), str):
            return _msg(UNKNOWN, "no answer to the write and the file cannot be read", machine)
        rules = boundaries_rules(after)
        version = after["version"]
        if version == clean["base_version"]:
            return _msg(NOT_SAVED, f"{_clip(lost.get('message'))}; the file is unchanged", machine)
        if reflects(clean["operation"], clean["rule"], rules):
            return {"outcome": "saved", "recovered": True, "version": version, "rules": rules}
        return {"outcome": "conflict", "version": version, "rules": rules, "recovered": True}

    # audit lines
    def _line(self, label: str, machine: str, clean: dict, old: dict | None, outcome: str) -> dict:
        rule = clean.get("rule") or {}
        new = None if clean.get("operation") == "rm" else rule
        stamp = datetime.fromtimestamp(self._wall(), tz=timezone.utc).isoformat()
        return {
            "time": stamp,
            "label": label,
            "machine": _clip(machine, 80),
            "operation": clean.get("operation"),
            "old": None if old is None else pick_rule(old),
            "new": new,
            "outcome": outcome,
        }

    def _final(self, label: str, machine: str, clean: dict, old: dict | None, out: dict) -> dict:
        try:
            self.audit.append(self._line(label, machine, clean, old, str(out.get("outcome"))))
        except OSError as exc:
            return {**out, "audit_error": _clip(exc.strerror or exc, 80)}
        return out

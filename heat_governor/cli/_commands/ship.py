"""``heat-governor ship`` -- ``overview``, ``test`` and ``fetch``.

``test`` PUTs and GETs a small probe object to prove the ``[ship]`` endpoint, bucket and
credentials work. ``fetch`` downloads what a machine shipped (events, postmortem, black box)
for one boot and prints a summary. Credentials are never printed.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from heat_governor import fleet as fleet_mod
from heat_governor import ship as shipmod
from heat_governor.cli._commands.governing import _config
from heat_governor.cli._commands.overview import emit_overview
from heat_governor.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from heat_governor.cli._output import emit_result

_JSON_HELP = "Emit structured JSON."
_EVENT_RE = re.compile(r"^(\d{8}T\d{6}\.\d{3}Z)(?:-\d+)?-(.+)\.json$")
_EVENTS = "events/"  # a boot's event objects live under <root><boot>/events/
_MAX_OBJECT = 64 * 1024 * 1024  # a sanity cap on what we decode for the summary


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _client(args: argparse.Namespace):
    """``(S3Client, ShipRule)`` from the config, or a user error saying what is missing."""
    rule = _config(args).ship
    if not rule.endpoint or not rule.bucket:
        raise CliError(
            EXIT_USER_ERROR,
            "[ship] endpoint and bucket are not configured",
            'add [ship] endpoint = "https://..." and bucket = "..." to config.toml '
            "(heat-governor config show)",
        )
    creds = shipmod.resolve_credentials(rule.profile)
    if creds is None:
        raise CliError(
            EXIT_USER_ERROR,
            "no ship credentials found",
            "export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or add a profile to "
            "~/.aws/credentials (set [ship] profile to pick it)",
        )
    try:
        client = shipmod.S3Client(rule.endpoint, rule.region, rule.bucket, creds, timeout=10.0)
    except ValueError as exc:
        raise CliError(EXIT_USER_ERROR, str(exc), "fix [ship] in config.toml") from exc
    return client, rule


def _env_error(err: shipmod.ShipError, what: str) -> CliError:
    status = "no response" if err.status is None else f"HTTP {err.status}"
    return CliError(
        EXIT_ENV_ERROR,
        f"{what} failed: {status} {err.code}",
        "check the endpoint, bucket and credentials ('heat-governor ship test')",
    )


def _local_machine() -> str | None:
    path = fleet_mod.default_fleet_path()
    if not path.exists():
        return None
    for m in fleet_mod.load_fleet(path).machines:
        if m.transport == "local":
            return m.name
    return None


# -- test ---------------------------------------------------------------------------


def cmd_test(args: argparse.Namespace) -> int:
    client, rule = _client(args)
    machine = rule.machine or _local_machine()
    if not machine:
        raise CliError(
            EXIT_USER_ERROR,
            'no machine name: [ship] machine is unset and fleet.toml has no transport="local"',
            'set [ship] machine = "<name>" in config.toml',
        )
    now = time.time()
    key = f"{shipmod.object_root(rule.prefix, machine, '_probe')}/{shipmod.utc_stamp(now)}.json"
    body = json.dumps({"probe": "heat-governor ship test", "t": now}).encode()
    started = time.monotonic()
    try:
        client.put(key, body, "application/json")
        back = client.get(key)
    except shipmod.ShipError as err:
        raise _env_error(err, "ship test") from err
    latency = round((time.monotonic() - started) * 1000.0, 1)
    if back != body:
        raise CliError(
            EXIT_ENV_ERROR,
            "ship test: the object read back differs from what was written",
            "the endpoint is not storing objects faithfully",
        )
    doc = {
        "ok": True,
        "endpoint": f"{client.scheme}://{client.netloc}",
        "bucket": client.bucket,
        "machine": machine,
        "key": key,
        "latency_ms": latency,
    }
    if _json(args):
        emit_result(doc, json_mode=True)
    else:
        emit_result(
            f"ship test ok: PUT+GET {key} on {doc['endpoint']} bucket {client.bucket} "
            f"in {latency:g} ms",
            json_mode=False,
        )
    return 0


# -- fetch --------------------------------------------------------------------------


def _boots(keys: list[str], root: str) -> dict[str, str]:
    """``{boot_id: newest event stamp or ""}`` for the keys under ``root``."""
    boots: dict[str, str] = {}
    for k in keys:
        rest = k[len(root) :]
        boot, _, tail = rest.partition("/")
        if not boot or boot == "_probe" or not tail:
            continue
        m = _EVENT_RE.match(tail[len(_EVENTS) :]) if tail.startswith(_EVENTS) else None
        boots[boot] = max(boots.get(boot, ""), m.group(1) if m else "")
    return boots


def _pick_boot(boots: dict[str, str]) -> str:
    return max(boots, key=lambda b: (boots[b], b))


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:=+@-]", "_", name).lstrip(".") or "_"


def _parse(body: bytes) -> dict:
    try:
        doc = json.loads(body)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _event_row(name: str, doc: dict) -> dict:
    m = _EVENT_RE.match(name)
    t = doc.get("t")
    if isinstance(t, (int, float)):
        utc = _utc(t)
    else:
        utc = m.group(1) if m else None
    return {
        "file": name,
        "kind": doc.get("event") or (m.group(2) if m else name),
        "t": t,
        "utc": utc,
        "pid": doc.get("pid"),
        "comm": doc.get("comm"),
    }


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blackbox_summary(body: bytes) -> dict:
    try:
        text = gzip.GzipFile(fileobj=io.BytesIO(body)).read(_MAX_OBJECT).decode("utf-8", "replace")
    except (OSError, EOFError):
        return {"rows": 0, "from": None, "to": None, "error": "not gzip"}
    ts = []
    rows = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        rows += 1
        t = _parse(line.encode()).get("t")
        if isinstance(t, (int, float)):
            ts.append(float(t))
    return {
        "rows": rows,
        "from": _utc(min(ts)) if ts else None,
        "to": _utc(max(ts)) if ts else None,
    }


def _postmortem_gist(doc: dict) -> str:
    if not doc:
        return "(empty)"
    parts = []
    for k, v in list(doc.items())[:8]:
        s = json.dumps(v, default=str) if not isinstance(v, str) else v
        parts.append(f"{k}={s[:60]}")
    return ", ".join(parts)


def _get(client, key: str, what: str) -> bytes | None:
    try:
        return client.get(key)
    except shipmod.ShipError as err:
        if err.status == 404 or err.code == "NoSuchKey":
            return None
        raise _env_error(err, f"fetching {what}") from err


def cmd_fetch(args: argparse.Namespace) -> int:
    if not args.machine:
        raise CliError(EXIT_USER_ERROR, "--machine is required", "for example --machine spark")
    client, rule = _client(args)
    root = shipmod.object_root(rule.prefix, args.machine, "x").rsplit("/", 1)[0] + "/"
    try:
        keys = client.list(root)
    except shipmod.ShipError as err:
        raise _env_error(err, "listing the bucket") from err
    boots = _boots(keys, root)
    boot = _resolve_boot(args, boots, root)
    base = shipmod.object_root(rule.prefix, args.machine, boot) + "/"
    out = Path(args.out) if args.out else Path("heat-governor-fetch") / args.machine / boot
    (out / "events").mkdir(parents=True, exist_ok=True)
    events = _fetch_events(client, keys, base, out)
    pm_body = _get(client, shipmod.postmortem_key(rule.prefix, args.machine, boot), "postmortem")
    bb_body = _get(client, shipmod.blackbox_key(rule.prefix, args.machine, boot), "black box")
    postmortem = None
    if pm_body is not None:
        (out / "postmortem.json").write_bytes(pm_body)
        postmortem = _parse(pm_body)
    blackbox = None
    if bb_body is not None:
        (out / "blackbox.jsonl.gz").write_bytes(bb_body)
        blackbox = _blackbox_summary(bb_body)
    doc = {
        "machine": args.machine,
        "boot": boot,
        "out": str(out),
        "boots": sorted(boots),
        "events": events,
        "postmortem": postmortem,
        "blackbox": blackbox,
    }
    if _json(args):
        emit_result(doc, json_mode=True)
        return 0
    emit_result(_fetch_text(doc), json_mode=False)
    return 0


def _resolve_boot(args: argparse.Namespace, boots: dict[str, str], root: str) -> str:
    """The boot to fetch: ``--boot``, else the newest; a user error when it has nothing."""
    boot = args.boot or (_pick_boot(boots) if boots else None)
    if boot is None or boot not in boots:
        raise CliError(
            EXIT_USER_ERROR,
            f"nothing shipped for machine {args.machine!r}"
            + (f" boot {boot!r}" if args.boot else "")
            + f" under {root}",
            "check the name and [ship] prefix; known boots: "
            + (", ".join(sorted(boots)) or "none"),
        )
    return boot


def _fetch_events(client, keys: list[str], base: str, out: Path) -> list[dict]:
    """Download the boot's event objects into ``out/events``; one summary row each."""
    events = []
    for key in sorted(k for k in keys if k.startswith(base + _EVENTS)):
        name = _safe_name(key.rsplit("/", 1)[-1])
        body = _get(client, key, "an event")
        if body is None:
            continue
        (out / "events" / name).write_bytes(body)
        events.append(_event_row(name, _parse(body)))
    return events


def _fetch_text(doc: dict) -> str:
    """The human-readable summary of a fetch (``doc`` is the ``--json`` result)."""
    events, postmortem, blackbox = doc["events"], doc["postmortem"], doc["blackbox"]
    lines = [f"machine {doc['machine']}, boot {doc['boot']} -> {doc['out']}"]
    lines.append(f"events: {len(events)}")
    for e in events:
        who = f" pid {e['pid']} {e['comm']}" if e["pid"] is not None else ""
        lines.append(f"  {e['utc']} {e['kind']}{who}")
    lines.append(
        "postmortem: " + (_postmortem_gist(postmortem) if postmortem is not None else "none")
    )
    if blackbox is None:
        lines.append("black box: none")
    else:
        lines.append(f"black box: {blackbox['rows']} rows, {blackbox['from']} to {blackbox['to']}")
    return "\n".join(lines)


# -- overview -----------------------------------------------------------------------


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "heat-governor ship",
        [
            {
                "title": "Verbs",
                "items": [
                    "ship overview -- this description",
                    "ship test [--config PATH] -- PUT and GET a probe object with the [ship] "
                    "settings; prints ok and the latency",
                    "ship fetch --machine M [--boot B] [--out DIR] -- download a boot's events, "
                    "postmortem and black box and summarise them (newest boot by default)",
                ],
            },
            {
                "title": "Notes",
                "items": [
                    "config: the [ship] section (endpoint, bucket, region, prefix, profile, "
                    "machine); credentials from AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or the "
                    "shared credentials file, never printed",
                    "layout: <prefix>/<machine>/<boot_id>/{events/,postmortem.json,"
                    "blackbox.jsonl.gz}",
                    "exit codes: 0 ok, 1 missing config/credentials/machine or nothing found, "
                    "2 the endpoint or bucket failed",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("ship", help="Ship records to S3 (see 'heat-governor ship overview').")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun = p.add_subparsers(dest="ship_command", parser_class=type(p))

    ov = noun.add_parser("overview", help="Describe the ship noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)

    t = noun.add_parser("test", help="PUT and GET a probe object with the [ship] settings.")
    t.add_argument("--config", help="Config file (default: the usual path).")
    t.add_argument("--json", action="store_true", help=_JSON_HELP)
    t.set_defaults(func=cmd_test)

    f = noun.add_parser("fetch", help="Download a machine's shipped boot and summarise it.")
    f.add_argument("--machine", help="Machine name as shipped.")
    f.add_argument("--boot", help="Boot id (default: the newest).")
    f.add_argument("--out", help="Directory (default ./heat-governor-fetch/<machine>/<boot>/).")
    f.add_argument("--config", help="Config file (default: the usual path).")
    f.add_argument("--json", action="store_true", help=_JSON_HELP)
    f.set_defaults(func=cmd_fetch)

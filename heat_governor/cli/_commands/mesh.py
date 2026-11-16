"""``heat-governor mesh`` -- the fleet view: ``overview``, ``status`` and ``serve``.

``serve`` runs a :class:`FleetReader` and the :class:`MeshServer` (same-origin page + JSON
API, loopback by default). ``status`` asks a running ``mesh serve`` for ``/api/fleet`` and,
when none answers, does a one-shot direct read through the same reader code. Both are
read-only toward the machines, except that ``serve --token-file PATH`` enables token-guarded
boundary reads, previews and writes (``/api/boundaries/MACHINE``, through
:class:`~heat_governor.boundaries_service.BoundariesService`). Without ``--token-file`` there is
no write path at all.
"""

from __future__ import annotations

import argparse
import http.client
import json
import signal
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from heat_governor import fleet as fleet_mod
from heat_governor.cli._commands.overview import emit_overview
from heat_governor.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from heat_governor.cli._output import emit_result
from heat_governor.fleet import Fleet, load_fleet
from heat_governor.fleet_reader import FleetReader, TransportFactory
from heat_governor.mesh_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    SERVER_NAME_RE,
    WILDCARD_HOSTS,
    MeshServer,
    TokenFileError,
    load_tokens,
)

DEFAULT_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"  # NOSONAR python:S5332 -- loopback
_JSON_HELP = "Emit structured JSON."
SERVER_TIMEOUT_S = 2.0
DIRECT_SLACK_S = 1.0

# Test seam: when set, the reader's transports come from this factory instead of ssh/local.
# Production leaves it None. Tests assign a fake factory (and never contact real hosts).
transport_factory: TransportFactory | None = None

_RANK = {"unreachable": 0, "stale": 1, "ok": 2}


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _fleet_path(args: argparse.Namespace) -> Path:
    given = getattr(args, "fleet", None)
    return Path(given) if given else fleet_mod.default_fleet_path()


def _load(args: argparse.Namespace) -> Fleet:
    path = _fleet_path(args)
    if not path.exists():
        raise CliError(
            EXIT_USER_ERROR,
            f"no fleet file at {path}",
            "create it (see docs/fleet.example.toml) or pass --fleet PATH",
        )
    return load_fleet(path)


def _make_reader(fleet: Fleet) -> FleetReader:
    return FleetReader(fleet, transport_factory=transport_factory)


def parse_listen(spec: str) -> tuple[str, int]:
    """``HOST:PORT`` (port 0 allowed: an ephemeral port, for tests). ``CliError`` otherwise."""
    host, sep, port = str(spec).rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if (
        not sep
        or not host
        or not port.isdigit()
        or int(port) > 65535
        or "/" in spec
        or any(c.isspace() for c in spec)
        or (":" in host and not str(spec).startswith("["))
    ):
        raise CliError(
            EXIT_USER_ERROR,
            f"bad --listen {spec!r}: expected HOST:PORT",
            f"for example {DEFAULT_HOST}:{DEFAULT_PORT}",
        )
    return host, int(port)


# -- serve --------------------------------------------------------------------------


def read_token_file(path: str | Path) -> dict[str, str]:
    """``load_tokens`` with its refusal as an exit-2 ``CliError`` (the message names the problem
    and never a token)."""
    try:
        return load_tokens(path)
    except TokenFileError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            str(exc),
            "the token file is lines of 'label = token' (token: 32+ characters, no spaces), "
            "owned by you with mode 600",
        ) from exc


def check_server_names(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    for name in names:
        if not SERVER_NAME_RE.match(name):
            raise CliError(
                EXIT_USER_ERROR,
                f"bad --server-name {name!r}: expected a plain host name, no port",
                "for example --server-name elitemini.tailnet-name.ts.net",
            )
    return tuple(names)


def _boundaries_service(reader: FleetReader, audit_path: Path | None):
    from heat_governor.boundaries_service import BoundariesService

    service = BoundariesService(reader.snapshot, reader.transport, audit_path=audit_path)
    try:
        service.audit.check()
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot open the boundaries audit log {service.audit.path}: {exc.strerror or exc}",
            "writes are refused without an audit log; check the reader state dir",
        ) from exc
    return service


def start_mesh(
    fleet: Fleet,
    listen: str | None = None,
    *,
    tokens: dict[str, str] | None = None,
    server_names: tuple[str, ...] = (),
    audit_path: Path | None = None,
) -> tuple[FleetReader, MeshServer]:
    """The testable core of ``serve``: start the reader and the server (loopback default).
    ``tokens`` enables the boundaries routes (their audit log at ``audit_path``, default the
    reader state dir); a wildcard listen address is then refused (exit 1)."""
    host, port = parse_listen(listen) if listen else (DEFAULT_HOST, DEFAULT_PORT)
    if tokens and host in WILDCARD_HOSTS:
        raise CliError(
            EXIT_USER_ERROR,
            f"--token-file needs a specific --listen address, not {host or 'a wildcard'!r}",
            "bind the loopback or the tailnet address",
        )
    reader = _make_reader(fleet)
    try:
        # without tokens the call is exactly the read-only one of 0.17
        writes = (
            {
                "tokens": tokens,
                "boundaries": _boundaries_service(reader, audit_path),
                "server_names": server_names,
            }
            if tokens
            else {}
        )
        server = MeshServer(reader, host=host, port=port, **writes)
    except OSError as exc:
        reader.close()
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot listen on {host}:{port}: {exc.strerror or exc}",
            "pick another port with --listen HOST:PORT",
        ) from exc
    except CliError:
        reader.close()
        raise
    reader.start()
    server.start()
    return reader, server


def stop_mesh(reader: FleetReader, server: MeshServer) -> None:
    server.stop()
    reader.close()


def _wait_for_signal() -> None:  # pragma: no cover - blocks until SIGINT/SIGTERM
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    done.wait()


def cmd_serve(args: argparse.Namespace) -> int:
    fleet = _load(args)
    names = check_server_names(getattr(args, "server_name", None) or ())
    token_file = getattr(args, "token_file", None)
    tokens = read_token_file(token_file) if token_file else None
    reader, server = start_mesh(
        fleet, getattr(args, "listen", None), tokens=tokens, server_names=names
    )
    host, port = server.address[:2]
    url = f"http://{host}:{port}"  # NOSONAR python:S5332 -- mesh serve binds loopback by default
    writes = (
        f"boundary writes enabled ({len(server.tokens)} tokens)"
        if server.writes_enabled
        else "read-only"
    )
    try:
        if _json(args):
            emit_result(
                {
                    "serving": url,
                    "host": host,
                    "port": port,
                    "machines": len(fleet.machines),
                    "writes_enabled": server.writes_enabled,
                },
                json_mode=True,
            )
        else:
            emit_result(
                f"mesh serve: {url}  ({len(fleet.machines)} machines, "
                f"poll {fleet.poll_s:g}s, {writes}); Ctrl-C to stop",
                json_mode=False,
            )
        _wait_for_signal()
    finally:
        stop_mesh(reader, server)
    return 0


# -- status -------------------------------------------------------------------------


def _fetch_server(url: str) -> dict:
    """GET <url>/api/fleet; raises on any failure (the caller falls back)."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError(f"not an http URL: {url!r}")
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=SERVER_TIMEOUT_S)
    try:
        conn.request("GET", "/api/fleet")
        resp = conn.getresponse()
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status}")
        doc = json.loads(resp.read(4_000_000))
    finally:
        conn.close()
    if not isinstance(doc, dict) or not isinstance(doc.get("machines"), list):
        raise ValueError("not a fleet snapshot")
    return doc


def _direct_snapshot(fleet: Fleet) -> dict:
    """One concurrent read of every machine; bounded by about ``timeout_s`` + 1 s."""
    reader = _make_reader(fleet)
    try:
        reader.poll_all(fleet.timeout_s + DIRECT_SLACK_S)  # a hung read closes its own ssh
        return reader.snapshot()
    finally:
        reader.close()


def _now() -> float:
    return time.monotonic()


def _guard_summary(g: object, state: object = "ok") -> dict | None:
    """The memory guard in a few fields; None when the daemon reports no guard.

    ``stale`` is true when the machine is not ``ok``: the fields are then the last ones read, and
    the guard may not be running at all.
    """
    if not isinstance(g, dict):
        return None
    bounds = g.get("boundaries")
    return {
        "action": g.get("action"),
        "armed": g.get("armed"),
        "kills_last_hour": g.get("kills_last_hour"),
        "boundary_count": len(bounds) if isinstance(bounds, list) else 0,
        "stale": state != "ok",
    }


def _row(m: dict) -> dict:
    th = m.get("thermal") or {}
    health = m.get("health") or {}
    mem = th.get("memory") or {}
    return {
        "name": m.get("name"),
        "transport": m.get("transport"),
        "host": m.get("host"),
        "state": m.get("state"),
        "age_s": m.get("age_s"),
        "temp_c": th.get("temp_c"),
        "mem_used_pct": mem.get("used_pct"),
        "mem_total_mib": mem.get("total_mib"),
        "mem_temp_c": mem.get("temp_c"),
        "level": th.get("level"),
        "admit": th.get("admit") if m.get("state") == "ok" else None,
        "version": th.get("version"),
        "mem_guard": _guard_summary(th.get("mem_guard"), m.get("state")),
        # false when the snapshot has a reading but no mem_guard key at all: it came from a
        # `mesh serve` older than 0.18, which drops the field, so "no guard" is not known
        "mem_guard_known": "mem_guard" in th or not th,
        "last_error": health.get("last_error"),
        "failing": bool(health.get("failing")),
    }


def _sort_key(row: dict) -> tuple:
    t = row["temp_c"]
    return (_RANK.get(row["state"], 0), -(t if isinstance(t, (int, float)) else 0.0))


def build_status(snapshot: dict, source: str, url: str | None) -> dict:
    rows = sorted((_row(m) for m in snapshot.get("machines", [])), key=_sort_key)
    return {
        "source": source,
        "url": url,
        "reader": snapshot.get("reader"),
        "machines": rows,
    }


def _fmt(v: object, spec: str = "") -> str:
    return "-" if v is None else format(v, spec)


def _show_error(row: dict) -> bool:
    """Show a box's last error only while it is failing or not ok (a recovered box keeps
    last_error in the data, but printing it would read as a current fault)."""
    return bool(row.get("failing")) or row.get("state") != "ok"


def _mem_text(r: dict) -> str:
    """``mem 91% of 122G`` (GiB, derived total) and, where a DDR zone exists, ``ddr 46C``."""
    pct, total = r.get("mem_used_pct"), r.get("mem_total_mib")
    if pct is None:
        return f"{'mem -':<16} {'':<7}"
    text = f"mem {pct:.0f}%" + ("" if total is None else f" of {total / 1024:.0f}G")
    ddr = "" if r.get("mem_temp_c") is None else f"ddr {r['mem_temp_c']:.0f}C"
    return f"{text:<16} {ddr:<7}"


def _guard_text(r: dict) -> str:
    """``guard kill armed 2k 3b`` (action, armed or not, kills in the last hour, boundaries).

    ``guard -`` when the daemon reports none, ``guard ?`` when the snapshot's reader is too old to
    say, and ``guard (last) ...`` when the machine is not ok and these are the last values read.
    """
    g = r.get("mem_guard")
    if not g:
        return "guard -" if r.get("mem_guard_known", True) else "guard ?"
    armed = "armed" if g.get("armed") else "unarmed"
    last = "(last) " if g.get("stale") else ""
    return (
        f"guard {last}{_fmt(g.get('action'))} {armed} "
        f"{_fmt(g.get('kills_last_hour'))}k {_fmt(g.get('boundary_count'))}b"
    )


def _admit_text(admit: object) -> str:
    if admit is None:
        return "-"
    return "admit" if admit else "hold"


def render_status(st: dict) -> str:
    rows = st["machines"]
    w = max([len(str(r["name"])) for r in rows] + [4])
    guards = [_guard_text(r) for r in rows]
    gw = max([len(g) for g in guards] + [7])
    lines = []
    for r, guard in zip(rows, guards):
        admit = _admit_text(r["admit"])
        lines.append(
            f"{str(r['name']):<{w}}  {str(r['state']):<11} age {_fmt(r['age_s'], '.1f'):>6}s  "
            f"{_fmt(r['temp_c'], '.1f'):>5}C  {_mem_text(r)}  {_fmt(r['level']):<8} {admit:<5} "
            f"{guard:<{gw}} v{_fmt(r['version'])}"
            + (f"  ({r['last_error']})" if r["last_error"] and _show_error(r) else "")
        )
    rd = st.get("reader") or {}
    src = st["source"] + (f" {st['url']}" if st.get("url") else "")
    lines.append(
        f"reader: source {src}; poll {_fmt(rd.get('poll_s'))}s timeout "
        f"{_fmt(rd.get('timeout_s'))}s; {len(rows)} machines"
    )
    return "\n".join(lines)


def read_snapshot(args: argparse.Namespace) -> tuple[dict, str, str | None]:
    """Fleet snapshot from a running ``mesh serve`` (``args.url``), else a direct read.

    Returns ``(snapshot, source, url)``; ``source`` is "mesh serve" or "direct". Shared by
    ``mesh status`` and the MCP tools so they never disagree. Raises ``CliError`` when
    there is neither a server nor a usable fleet file.
    """
    url = (getattr(args, "url", None) or DEFAULT_URL).rstrip("/")
    try:
        return _fetch_server(url), "mesh serve", url
    except Exception:  # noqa: BLE001 - any failure means: read directly
        return _direct_snapshot(_load_for_status(args)), "direct", None


def cmd_status(args: argparse.Namespace) -> int:
    snapshot, source, url = read_snapshot(args)
    status = build_status(snapshot, source, url)
    # reads were attempted but not one box ever answered: output is still data, exit 2
    nothing = source == "direct" and all(r["age_s"] is None for r in status["machines"])
    emit_result(status if _json(args) else render_status(status), json_mode=_json(args))
    return EXIT_ENV_ERROR if nothing else 0


def _load_for_status(args: argparse.Namespace) -> Fleet:
    try:
        return _load(args)
    except CliError as err:
        if err.code == EXIT_USER_ERROR and not _fleet_path(args).exists():
            raise CliError(
                EXIT_ENV_ERROR,
                "nothing to read: no mesh serve answering and no fleet file at "
                f"{_fleet_path(args)}",
                "start 'heat-governor mesh serve' or create fleet.toml "
                "(see docs/fleet.example.toml)",
            ) from err
        raise


# -- overview -----------------------------------------------------------------------


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "heat-governor mesh",
        [
            {
                "title": "Verbs",
                "items": [
                    "mesh overview -- this description",
                    "mesh status [--fleet PATH] [--url URL] -- every machine's state, age, "
                    "temp, level and admission, from a running mesh serve or a one-shot read",
                    "mesh serve [--fleet PATH] [--listen HOST:PORT] [--token-file PATH] "
                    "[--server-name NAME] -- the fleet page and JSON API until Ctrl-C; "
                    "read-only unless --token-file enables token-guarded boundary writes",
                ],
            },
            {
                "title": "Where things are",
                "items": [
                    f"fleet file: {fleet_mod.default_fleet_path()} "
                    "(format: docs/fleet.example.toml)",
                    f"default URL: {DEFAULT_URL} (loopback; only --listen changes it)",
                    "exit codes: 0 read worked (unreachable boxes are data), "
                    "1 bad fleet or flag, 2 nothing could be read",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("mesh", help="The fleet view (see 'heat-governor mesh overview').")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun = p.add_subparsers(dest="mesh_command", parser_class=type(p))

    ov = noun.add_parser("overview", help="Describe the mesh noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)

    st = noun.add_parser("status", help="Every machine's state, age, temp, level, admission.")
    st.add_argument("--fleet", help="fleet.toml path (default: beside config.toml).")
    st.add_argument("--url", help=f"mesh serve to ask first (default {DEFAULT_URL}).")
    st.add_argument("--json", action="store_true", help=_JSON_HELP)
    st.set_defaults(func=cmd_status)

    sv = noun.add_parser(
        "serve",
        help="Serve the fleet page and JSON API (read-only unless --token-file is given).",
    )
    sv.add_argument("--fleet", help="fleet.toml path (default: beside config.toml).")
    sv.add_argument(
        "--listen",
        help=f"HOST:PORT to bind (default {DEFAULT_HOST}:{DEFAULT_PORT}). The read routes have "
        "no authentication: bind a non-loopback address only on a network you trust.",
    )
    sv.add_argument(
        "--token-file",
        help="Enable boundary reads, previews and writes (/api/boundaries/MACHINE) for requests "
        "with 'Authorization: Bearer TOKEN'. The file holds 'label = token' lines (# comments "
        "allowed; tokens 32+ characters), must be mode 600 and owned by you, and is read once: "
        "restart to rotate. No default: without it there is no write path. Needs a specific "
        "--listen address (plain HTTP: loopback or a tailnet address only).",
    )
    sv.add_argument(
        "--server-name",
        action="append",
        default=[],
        help="A host name the page is opened by (e.g. the tailnet name), accepted in the Host "
        "header of boundaries requests besides the listen address; repeatable.",
    )
    sv.add_argument("--json", action="store_true", help=_JSON_HELP)
    sv.set_defaults(func=cmd_serve)

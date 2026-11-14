"""``heat-governor memory`` -- ``overview``, ``status``, ``top`` and ``replay``.

``status`` reads the running daemon's published state (``mem_guard``), ``top`` is a one-shot
list of the biggest processes by anonymous memory, and ``replay`` runs the memory trigger over
a recording (the fixture CSV or a records JSONL) to show when it would have fired.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from heat_governor import client, memprocs, memtrigger
from heat_governor.cli._commands.overview import emit_overview
from heat_governor.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from heat_governor.cli._output import emit_result
from heat_governor.config import MemoryRule

_JSON_HELP = "Emit structured JSON."
_NEEDLE = '"mem:avail_mib"'
_MIB = 1024.0


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


# -- status -------------------------------------------------------------------------


def _guard_text(g: dict) -> str:
    armed = "armed" if g.get("armed") else "disarmed"
    lines = [
        f"memory guard: action {g.get('action')}, {armed}, avail {g.get('avail_mib')} MiB, "
        f"{g.get('kills_last_hour', 0)} kill(s) in the last hour"
    ]
    lim = g.get("limits")
    if isinstance(lim, dict):
        lines.append(
            f"limits: floor {lim.get('floor_mib')} MiB, soft {lim.get('soft_mib')} MiB "
            f"(source: {lim.get('source')})"
        )
    k = g.get("last_kill")
    if isinstance(k, dict):
        when = _fmt_time(k.get("t"))
        lines.append(
            f"last kill: {k.get('comm')} (pid {k.get('pid')}, {k.get('rss_mib')} MiB, "
            f"repeat {k.get('repeat')}) at {when}"
        )
    else:
        lines.append("last kill: none")
    return "\n".join(lines)


def _fmt_time(t: object) -> str:
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def cmd_status(args: argparse.Namespace) -> int:
    adm = client.read(args.source)
    if adm.state is None:
        raise CliError(
            EXIT_ENV_ERROR,
            "no daemon state readable (is 'heat-governor run' up?)",
            "start the daemon, or pass --source PATH|http://host:port/state",
        )
    guard = adm.state.get("mem_guard")
    if not isinstance(guard, dict):
        if _json(args):
            emit_result({"configured": False, "mem_guard": None}, json_mode=True)
        else:
            emit_result("memory guard not configured ([memory] absent)", json_mode=False)
        return 0
    if _json(args):
        emit_result({"configured": True, "mem_guard": guard}, json_mode=True)
    else:
        emit_result(_guard_text(guard), json_mode=False)
    return 0


# -- top ----------------------------------------------------------------------------


def cmd_top(args: argparse.Namespace) -> int:
    if args.n < 1:
        raise CliError(EXIT_USER_ERROR, "--n must be at least 1", "for example --n 10")
    root = Path(args.proc_root)
    if not root.is_dir():
        raise CliError(EXIT_ENV_ERROR, f"cannot read {root}", "run this on Linux with /proc")
    rows = memprocs.top(memprocs.scan(root), None, args.n)
    if _json(args):
        emit_result(rows, json_mode=True)
        return 0
    lines = [f"{'PID':>7} {'ANON MiB':>9} {'RSS MiB':>9}  {'COMM':<16} CMDLINE"]
    for r in rows:
        lines.append(
            f"{r['pid']:>7} {r['rss_anon_mib']:>9.0f} {r['rss_mib']:>9.0f}  "
            f"{r['comm']:<16} {r['cmdline'][:100]}"
        )
    emit_result("\n".join(lines), json_mode=False)
    return 0


# -- replay -------------------------------------------------------------------------


def _local_total_mib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return float(line.split()[1]) / _MIB
    except (OSError, ValueError, IndexError):
        pass
    return None


def _csv_rows(path: Path) -> tuple[list[tuple[float, float]], float | None]:
    total = None
    rows: list[tuple[float, float]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("#"):
                if "MemTotal_mib=" in line and total is None:
                    frag = line.split("MemTotal_mib=", 1)[1].split()[0]
                    try:
                        total = float(frag)
                    except ValueError:
                        pass
                continue
            if not line or line.startswith("t,"):
                continue
            t, _, a = line.partition(",")
            try:
                rows.append((float(t), float(a)))
            except ValueError:
                continue
    return rows, total


def _jsonl_rows(path: Path) -> Iterator[tuple[float, float]]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if _NEEDLE not in line:  # cheap pre-filter: most lines are not memory ticks
                continue
            try:
                rec = json.loads(line)
                yield float(rec["t"]), float(rec["system"]["mem:avail_mib"])
            except (ValueError, KeyError, TypeError):
                continue


def _is_csv(path: Path) -> bool:
    if path.suffix.lower() == ".csv":
        return True
    with open(path, encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
    return first.startswith("#") or first.startswith("t,")


def _fire_row(f: memtrigger.Fire) -> dict:
    dt = datetime.fromtimestamp(f.t, timezone.utc)
    return {
        "t": f.t,
        "utc": dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "local": dt.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " " + _tz(dt),
        "reason": f.reason,
        "avail_mib": f.avail_mib,
        "rate_mib_s": round(f.rate_mib_s, 2),
    }


def _tz(dt: datetime) -> str:
    return dt.astimezone().strftime("%z")


def _params(args: argparse.Namespace) -> dict:
    d = MemoryRule()
    p = {
        "floor_pct": d.floor_pct if args.floor_pct is None else args.floor_pct,
        "horizon_s": d.horizon_s if args.horizon_s is None else args.horizon_s,
        "soft_pct": d.soft_pct if args.soft_pct is None else args.soft_pct,
        "window_s": d.window_s if args.window_s is None else args.window_s,
    }
    if not (0 < p["floor_pct"] < p["soft_pct"] <= 100):
        raise CliError(
            EXIT_USER_ERROR,
            "need 0 < --floor-pct < --soft-pct <= 100",
            f"defaults are {d.floor_pct:g} and {d.soft_pct:g}",
        )
    if p["horizon_s"] <= 0 or p["window_s"] <= 0:
        raise CliError(EXIT_USER_ERROR, "--horizon-s and --window-s must be positive", "")
    floor_mib, soft_mib = getattr(args, "floor_mib", None), getattr(args, "soft_mib", None)
    if (floor_mib is None) != (soft_mib is None):
        raise CliError(
            EXIT_USER_ERROR,
            "--floor-mib and --soft-mib must be given together",
            "absolute limits replace both percentages (e.g. --floor-mib 3072 --soft-mib 6144)",
        )
    if floor_mib is not None:
        if not 0 < floor_mib < soft_mib:
            raise CliError(EXIT_USER_ERROR, "need 0 < --floor-mib < --soft-mib", "")
        p["floor_mib"], p["soft_mib"] = float(floor_mib), float(soft_mib)
    return p


def _limits_text(params: dict) -> str:
    if "floor_mib" in params:
        return f"floor {params['floor_mib']:g} MiB soft {params['soft_mib']:g} MiB"
    return f"floor {params['floor_pct']:g}% soft {params['soft_pct']:g}%"


def _load_series(path: Path, args: argparse.Namespace) -> tuple[list, float, str]:
    try:
        csv = _is_csv(path)
        if csv:
            rows, total = _csv_rows(path)
        else:
            rows, total = list(_jsonl_rows(path)), None
    except OSError as exc:
        raise CliError(
            EXIT_USER_ERROR, f"cannot read {path}: {exc.strerror or exc}", "pass an existing file"
        ) from exc
    if args.total_mib is not None:
        total, source = args.total_mib, "--total-mib"
    elif total is not None:
        source = "file header"
    else:
        total, source = _local_total_mib(), "/proc/meminfo"
    if total is None or total <= 0:
        raise CliError(
            EXIT_USER_ERROR,
            "MemTotal is unknown: records do not carry it",
            "pass --total-mib N (the machine's MemTotal in MiB)",
        )
    if not rows:
        raise CliError(
            EXIT_USER_ERROR,
            f"no 'mem:avail_mib' samples in {path}",
            "use a records .jsonl with system['mem:avail_mib'] or a t,mem:avail_mib CSV",
        )
    return rows, float(total), source


def cmd_replay(args: argparse.Namespace) -> int:
    path = Path(args.path)
    params = _params(args)
    if args.total_mib is not None and args.total_mib <= 0:
        raise CliError(EXIT_USER_ERROR, "--total-mib must be positive", "")
    rows, total, source = _load_series(path, args)
    fires = [_fire_row(f) for f in memtrigger.replay(rows, total, **params)]
    span = [rows[0][0], rows[-1][0]]
    if _json(args):
        emit_result(
            {
                "file": str(path),
                "total_mib": int(total) if total == int(total) else total,
                "total_source": source,
                "params": params,
                "samples": len(rows),
                "span": span,
                "fires": fires,
            },
            json_mode=True,
        )
        return 0
    lines = [
        f"{path}: {len(rows)} samples, MemTotal {total:g} MiB ({source}); "
        f"{_limits_text(params)} horizon {params['horizon_s']:g}s "
        f"window {params['window_s']:g}s"
    ]
    for f in fires:
        lines.append(
            f"fire {f['utc']} (local {f['local']}): {f['reason']}, avail {f['avail_mib']:g} MiB, "
            f"fill {f['rate_mib_s']:g} MiB/s"
        )
    n = len(fires)
    lines.append(f"{n} fire{'s' if n != 1 else ''}")
    emit_result("\n".join(lines), json_mode=False)
    return 0


# -- overview -----------------------------------------------------------------------


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "heat-governor memory",
        [
            {
                "title": "Verbs",
                "items": [
                    "memory overview -- this description",
                    "memory status [--source S] -- the running daemon's memory guard: action, "
                    "armed, MemAvailable, last kill, kills in the last hour",
                    "memory top [--n N] -- the biggest processes by anonymous memory, now",
                    "memory replay <records.jsonl|csv> [--total-mib N] [--floor-pct P] "
                    "[--horizon-s S] [--soft-pct P] [--window-s S] -- when the trigger would "
                    "have fired over a recording",
                ],
            },
            {
                "title": "Notes",
                "items": [
                    "the guard is the [memory] section of config.toml; absent = record only",
                    "records do not carry MemTotal: replay uses the CSV header, --total-mib, "
                    "else this machine's /proc/meminfo, and says which",
                    "exit codes: 0 ok, 1 bad flag or file, 2 no daemon state / no /proc",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("memory", help="The memory guard (see 'heat-governor memory overview').")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun = p.add_subparsers(dest="memory_command", parser_class=type(p))

    ov = noun.add_parser("overview", help="Describe the memory noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)

    st = noun.add_parser("status", help="The running daemon's memory guard state.")
    st.add_argument(
        "--source", help="State file path or http://host:port/state (default: the local file)."
    )
    st.add_argument("--json", action="store_true", help=_JSON_HELP)
    st.set_defaults(func=cmd_status)

    tp = noun.add_parser("top", help="The biggest processes by anonymous memory, once.")
    tp.add_argument("--n", type=int, default=10, help="How many processes (default 10).")
    tp.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    tp.add_argument("--json", action="store_true", help=_JSON_HELP)
    tp.set_defaults(func=cmd_top)

    rp = noun.add_parser("replay", help="When the memory trigger would have fired.")
    rp.add_argument("path", help="records .jsonl (system['mem:avail_mib']) or a t,avail CSV.")
    rp.add_argument("--total-mib", type=float, help="MemTotal in MiB (records do not carry it).")
    rp.add_argument("--floor-pct", type=float, help="Floor as %% of RAM (default 5).")
    rp.add_argument("--horizon-s", type=float, help="Act if the floor is this close (default 10).")
    rp.add_argument("--soft-pct", type=float, help="Watch closely below this %% (default 20).")
    rp.add_argument("--window-s", type=float, help="Trend-fit window in seconds (default 10).")
    rp.add_argument("--floor-mib", type=float, help="Absolute floor in MiB (with --soft-mib).")
    rp.add_argument("--soft-mib", type=float, help="Absolute soft limit in MiB (with --floor-mib).")
    rp.add_argument("--json", action="store_true", help=_JSON_HELP)
    rp.set_defaults(func=cmd_replay)

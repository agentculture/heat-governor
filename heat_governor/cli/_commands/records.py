"""``heat-governor records`` -- ``overview`` and ``prune``.

The daemon writes one ``YYYYMMDD.jsonl`` per UTC day into ``<data_dir>/records``.
``prune`` runs one retention pass (:func:`heat_governor.retention.prune_pass`): it deletes
day files past the window and gzips the older raw ones. ``--dry-run`` changes nothing and
lists exactly what a real pass would do. ``overview`` is read-only.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from heat_governor import retention
from heat_governor.cli._commands.governing import _config
from heat_governor.cli._commands.overview import emit_overview
from heat_governor.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from heat_governor.cli._output import emit_diagnostic, emit_result
from heat_governor.state import default_data_dir

_JSON_HELP = "Emit structured JSON."
_FALLBACK_KEEP_DAYS = 7
_MB = 1024 * 1024


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _data_dir(args: argparse.Namespace, cfg) -> Path:
    chosen = getattr(args, "data_dir", None) or cfg.daemon.get("data_dir")
    return Path(chosen) if chosen else default_data_dir()


def _cfg_keep_days(cfg) -> int:
    try:
        return int(cfg.keep_days)
    except (AttributeError, TypeError, ValueError):
        return _FALLBACK_KEEP_DAYS


def _mb(n: int) -> str:
    return f"{n / _MB:.2f} MB"


# -- prune --------------------------------------------------------------------------


def cmd_prune(args: argparse.Namespace) -> int:
    cfg = _config(args)
    keep = args.keep_days if args.keep_days is not None else _cfg_keep_days(cfg)
    if keep < 1:
        raise CliError(
            EXIT_USER_ERROR,
            f"--keep-days must be at least 1, got {keep}",
            "today and yesterday are always kept; pass a whole number of days >= 1",
        )
    records = _data_dir(args, cfg) / "records"
    try:
        result = retention.prune_pass(
            records, time.time(), keep, dry_run=args.dry_run, allow_gap=args.allow_gap
        )
    except ValueError as exc:
        raise CliError(EXIT_USER_ERROR, str(exc), "pass --keep-days N with 1 <= N <= 3650") from exc
    except NotADirectoryError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"records directory not found or not a directory: {records}",
            "point --data-dir at the daemon's data directory (it holds records/)",
        ) from exc
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR, f"cannot read records directory {records}: {exc}", "check permissions"
        ) from exc
    doc = dict(result, dry_run=bool(args.dry_run), keep_days=keep, records_dir=str(records))
    if _json(args):
        emit_result(doc, json_mode=True)
    else:
        emit_result(_render_prune(doc), json_mode=False)
        for w in doc["warnings"]:
            emit_diagnostic(f"warning: {w}")
        for e in doc["errors"]:
            emit_diagnostic(f"error: {e}")
    # a per-file failure is an environment problem: the result is printed first, then exit 2
    return EXIT_ENV_ERROR if doc["errors"] else 0


def _render_prune(doc: dict) -> str:
    dry = doc["dry_run"]
    lines = [f"would delete {n}" if dry else f"deleted {n}" for n in doc["deleted"]]
    lines += [f"would compress {n}" if dry else f"compressed {n}" for n in doc["compressed"]]
    verb = "would free" if dry else "freed"
    suffix = " (at least; .gz sizes are unknown until written)" if dry else ""
    lines.append(
        f"{len(doc['deleted'])} deleted, {len(doc['compressed'])} compressed, "
        f"{verb} {_mb(doc['bytes_freed'])}{suffix}; keep_days {doc['keep_days']}"
    )
    return "\n".join(lines)


# -- overview -----------------------------------------------------------------------

_DAY_RE = re.compile(r"^\d{8}\.jsonl(\.gz)?\Z", re.ASCII)


def _inventory(records: Path) -> str:
    try:
        names = [p for p in records.iterdir() if _DAY_RE.match(p.name) and p.is_file()]
    except OSError:
        return "no readable records directory yet"
    raw = [p for p in names if not p.name.endswith(".gz")]
    gz = [p for p in names if p.name.endswith(".gz")]
    total = 0
    for p in names:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return f"{len(raw)} raw day files, {len(gz)} gz day files, {_mb(total)} total"


def cmd_overview(args: argparse.Namespace) -> int:
    cfg = _config(args)
    keep = _cfg_keep_days(cfg)
    records = _data_dir(args, cfg) / "records"
    emit_overview(
        "heat-governor records",
        [
            {
                "title": "What retention does",
                "items": [
                    f"keeps today plus {keep} complete earlier UTC days "
                    "([daemon] keep_days, default 7)",
                    "gzips raw day files older than yesterday; deletes days past the window",
                    "the daemon runs it once a day; 'records prune' runs it by hand",
                    "does nothing and warns when the clock looks wrong (newest day over a day "
                    "old, or a file dated in the future)",
                ],
            },
            {"title": "Records directory", "items": [str(records), _inventory(records)]},
            {
                "title": "Verbs",
                "items": [
                    "records overview [--config PATH] [--data-dir DIR] -- this description",
                    "records prune [--keep-days N] [--dry-run] [--allow-gap] [--config PATH] "
                    "[--data-dir DIR] -- run one retention pass; --dry-run lists what it would "
                    "do; --allow-gap also deletes days before a hole longer than keep_days",
                    "exit codes: 0 ok, 1 bad --keep-days or config, 2 records directory "
                    "missing or unreadable, or a file failed",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_overview(args)


def _add_common(p: argparse.ArgumentParser, *, verb: bool = False) -> None:
    """Shared options, accepted before or after the verb. On a verb parser their default is
    SUPPRESS, so a value given at the noun level (``records --config X prune``) is kept
    rather than reset to the verb's default."""
    extra = {"default": argparse.SUPPRESS} if verb else {}
    p.add_argument("--config", help="Config file (default: the usual path).", **extra)
    p.add_argument(
        "--data-dir",
        help="The daemon's data directory (default: daemon.data_dir, else XDG state).",
        **extra,
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP, **extra)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("records", help="Daily records: retention (see 'records overview').")
    _add_common(p)
    p.set_defaults(func=_no_verb)
    noun = p.add_subparsers(dest="records_command", parser_class=type(p))

    ov = noun.add_parser("overview", help="Describe the records noun and the retention window.")
    _add_common(ov, verb=True)
    ov.set_defaults(func=cmd_overview)

    pr = noun.add_parser("prune", help="Delete and compress old day files (--dry-run to preview).")
    _add_common(pr, verb=True)
    pr.add_argument(
        "--keep-days",
        type=int,
        help="Complete earlier days to keep besides today (default: [daemon] keep_days, else 7).",
    )
    pr.add_argument(
        "--dry-run", action="store_true", help="Change nothing; list what would happen."
    )
    pr.add_argument(
        "--allow-gap",
        action="store_true",
        help="Also delete days before a hole longer than keep_days (only once the clock is "
        "known to be right: a forward clock jump leaves the same hole).",
    )
    pr.set_defaults(func=cmd_prune)

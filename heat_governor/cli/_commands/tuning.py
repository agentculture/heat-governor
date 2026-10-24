"""The ``config`` and ``gpu-clock`` noun groups.

``config``: path / show / init / check / overview -- the file a running daemon
re-reads, so the governor can be tuned as we learn without a restart.

``gpu-clock``: overview / status / helper / lock / reset / hold / release -- the
GPU clock ceiling, the one control that needs the root helper. ``lock`` and
``reset`` are dry-run unless ``--apply``. ``hold`` / ``release`` declare the
operator's own ceiling (law for the daemon) in ``<data_dir>/operator.json``;
they write that file only, and the running daemon applies it.
"""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from heat_governor import config as cfgmod
from heat_governor import gpuclock
from heat_governor import operator_ceiling as opc
from heat_governor.cli._commands.governing import (
    _JSON_HELP,
    _add_profile_args,
    _config,
    _profile,
    _profile_dict,
)
from heat_governor.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from heat_governor.cli._output import emit_result


def _path(args: argparse.Namespace) -> Path:
    return Path(args.config) if getattr(args, "config", None) else cfgmod.default_config_path()


# -- config ---------------------------------------------------------------------


def cmd_config_overview(args: argparse.Namespace) -> None:
    path = _path(args)
    doc: dict[str, object] = {"path": str(path), "exists": path.exists()}
    if path.exists():
        try:
            cfg = cfgmod.load(path)
            doc |= {"valid": True, "platform": cfg.platform, "containers": len(cfg.containers)}
            doc["units"] = [u.name for u in cfg.units]
            doc["profile_keys"] = sorted(cfg.profile)
            doc["daemon_keys"] = sorted(cfg.daemon)
        except cfgmod.ConfigError as exc:
            doc |= {"valid": False, "problems": exc.problems}
    if args.json:
        emit_result(doc, json_mode=True)
        return
    if not doc["exists"]:
        text = f"no config at {path}: the built-in profile is in force\n"
        text += "start one with: heat-governor config init --write"
    elif doc.get("valid"):
        text = (
            f"{path}: valid; platform {doc['platform'] or '(detected)'}; overrides "
            f"{', '.join(doc['profile_keys']) or 'none'}; daemon "
            f"{', '.join(doc['daemon_keys']) or 'defaults'}; "
            f"{doc['containers']} container rule(s); "
            f"user units {', '.join(doc['units']) or 'none'}"
        )
    else:
        text = f"{path}: INVALID\n" + "\n".join(f"  - {p}" for p in doc["problems"])
    emit_result(text, json_mode=False)


def cmd_config_path(args: argparse.Namespace) -> None:
    path = _path(args)
    emit_result({"path": str(path)} if args.json else str(path), json_mode=args.json)


def cmd_config_show(args: argparse.Namespace) -> None:
    from heat_governor.cli._commands.governing import _config

    cfg = _config(args)
    prof = _profile(args, cfg)
    doc = {
        "config": str(cfg.path) if cfg.path and cfg.mtime else None,
        "profile": _profile_dict(prof),
        "daemon": cfg.daemon,
        "containers": [c.__dict__ for c in cfg.containers],
        "units": [{"name": u.name, "bands": dict(u.bands)} for u in cfg.units],
    }
    if args.json:
        emit_result(doc, json_mode=True)
        return
    emit_result(cfgmod.template(prof), json_mode=False)


def cmd_config_init(args: argparse.Namespace) -> None:
    prof = _profile(args, cfgmod.Config())  # from the built-in profile, not an existing file
    text = cfgmod.template(prof)
    path = _path(args)
    if not args.write:
        emit_result(
            (
                {"path": str(path), "written": False, "text": text}
                if args.json
                else text + f"\n# (not written; --write saves it to {path})"
            ),
            json_mode=args.json,
        )
        return
    if path.exists() and not args.force:
        raise CliError(
            EXIT_USER_ERROR,
            f"{path} exists",
            "edit it, or pass --force to replace it with the built-in profile",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    emit_result(
        {"path": str(path), "written": True} if args.json else f"wrote {path}",
        json_mode=args.json,
    )


def cmd_config_check(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else _path(args)
    try:
        cfg = cfgmod.load(path, required=True)
        cfgmod_profile = _profile(args, cfg)
    except cfgmod.ConfigError as exc:
        if args.json:
            emit_result(
                {"path": str(path), "valid": False, "problems": exc.problems}, json_mode=True
            )
        else:
            emit_result(
                f"{path}: INVALID\n" + "\n".join(f"  - {p}" for p in exc.problems), json_mode=False
            )
        return EXIT_USER_ERROR
    lim = cfgmod_profile.limits
    summary = f"warm {lim.warm_c} / hot {lim.hot_c} / critical {lim.crit_c} C"
    if args.json:
        emit_result({"path": str(path), "valid": True, "limits": summary}, json_mode=True)
    else:
        emit_result(f"{path}: valid ({cfgmod_profile.name}: {summary})", json_mode=False)
    return 0


# -- gpu-clock -----------------------------------------------------------------


def _clock() -> gpuclock.GpuClock:
    g = gpuclock.GpuClock()
    g.probe()
    return g


_CEILING_KEY = "gpu_ceiling_mhz"
_INVISIBLE = (
    "a raw `nvidia-smi -lgc` or `sudo heat-governor-gpuclock lock` is invisible here "
    "(no lock is exposed): declare a hand ceiling with `gpu-clock hold` or "
    "[daemon] gpu_ceiling_mhz for it to be law"
)
_VISIBLE = (
    "a hand lock below the daemon's own ceiling is seen through the helper's status "
    "and treated as the operator's (source observed) until `gpu-clock release`"
)


def _data_dir(args: argparse.Namespace, cfg) -> Path:
    from heat_governor.state import default_data_dir

    chosen = getattr(args, "data_dir", None) or cfg.daemon.get("data_dir")
    return Path(chosen) if chosen else default_data_dir()


def _operator_doc(args: argparse.Namespace, cfg, prof) -> dict:
    path = opc.hold_path(_data_dir(args, cfg))
    hold = opc.read_hold(path)
    config_mhz = cfg.daemon.get(_CEILING_KEY)
    mhz, source = opc.effective(config_mhz, hold)
    return {
        "mhz": mhz,
        "source": source,
        "config_mhz": config_mhz,
        "hold": None if hold is None else {"mhz": hold.mhz, "source": hold.source},
        "path": str(path),
        "hand_lock": _INVISIBLE if prof.gpu != "jetson" else _VISIBLE,
    }


def cmd_gpu_status(args: argparse.Namespace) -> None:
    cfg = _config(args)
    prof = _profile(args, cfg)
    g = _clock()
    doc = {
        "helper": g.helper,
        "available": g.available,
        "status": g.reason,
        "platform": prof.name,
        "gpu_clock_bands": [list(b) for b in prof.gpu_clock_bands],
        "hardware_ceiling_mhz": g.read_ceiling() if g.available else None,
        "operator_ceiling": _operator_doc(args, cfg, prof),
    }
    if args.json:
        emit_result(doc, json_mode=True)
        return
    bands = ", ".join(f">={t:g}C -> {m} MHz" for t, m in prof.gpu_clock_bands) or "none"
    op = doc["operator_ceiling"]
    law = "none" if op["mhz"] is None else f"{op['mhz']} MHz (source {op['source']})"
    emit_result(
        f"gpu clock control: {'available' if g.available else 'UNAVAILABLE'} -- {g.reason}\n"
        f"bands ({prof.name}): {bands}\n"
        "the daemon uses them only with daemon.gpu_clock = true (or run --gpu-clock)\n"
        f"operator ceiling (law): {law}; the daemon may go below it, never above\n"
        f"hand locks: {op['hand_lock']}",
        json_mode=False,
    )


def cmd_gpu_hold(args: argparse.Namespace) -> None:
    mhz = int(args.mhz)
    if not gpuclock.MIN_MHZ <= mhz <= gpuclock.MAX_MHZ:
        raise CliError(
            EXIT_USER_ERROR,
            f"refusing {mhz} MHz (allowed {gpuclock.MIN_MHZ}-{gpuclock.MAX_MHZ})",
            "pick a ceiling the root helper accepts",
        )
    path = opc.hold_path(_data_dir(args, _config(args)))
    opc.write_hold(path, mhz, opc.HOLD)
    doc = {_CEILING_KEY: mhz, "source": opc.HOLD, "path": str(path)}
    emit_result(
        (
            doc
            if args.json
            else f"operator GPU ceiling {mhz} MHz held ({path}).\n"
            "A running daemon applies it within ~2 s (even with gpu_clock off) and never "
            "writes a ceiling above it; `heat-governor gpu-clock release` gives it back."
        ),
        json_mode=args.json,
    )


def cmd_gpu_release(args: argparse.Namespace) -> None:
    cfg = _config(args)
    path = opc.hold_path(_data_dir(args, cfg))
    old = opc.clear_hold(path)
    still = cfg.daemon.get(_CEILING_KEY)
    doc = {
        "released": None if old is None else {_CEILING_KEY: old.mhz, "source": old.source},
        "config_gpu_ceiling_mhz": still,
        "path": str(path),
    }
    if args.json:
        emit_result(doc, json_mode=True)
        return
    text = (
        f"released the operator GPU ceiling {old.mhz} MHz ({old.source})"
        if old
        else "no held or observed operator GPU ceiling"
    )
    if still:
        text += f"; [daemon] gpu_ceiling_mhz = {still} in the config still holds"
    emit_result(text, json_mode=False)


def cmd_gpu_helper(args: argparse.Namespace) -> None:
    user = getpass.getuser()
    if args.json:
        emit_result(
            {
                "path": gpuclock.HELPER_PATH,
                "script": gpuclock.HELPER_SCRIPT,
                "sudoers": gpuclock.sudoers_line(user),
                "install": gpuclock.install_instructions(user),
            },
            json_mode=True,
        )
        return
    emit_result(
        gpuclock.HELPER_SCRIPT
        + "\n# ---- install (needs your sudo password once) ----\n"
        + gpuclock.install_instructions(user),
        json_mode=False,
    )


def _manual(args: argparse.Namespace, action: str) -> None:
    g = _clock()
    if not g.available:
        raise CliError(EXIT_ENV_ERROR, g.reason, "install it: heat-governor gpu-clock helper")
    what = f"lock the GPU clock ceiling at {args.mhz} MHz" if action == "lock" else "reset"
    if not args.apply:
        emit_result(
            {"dry_run": True, "would": what} if args.json else f"dry run: would {what} (--apply)",
            json_mode=args.json,
        )
        return
    ok, out = g.lock(args.mhz) if action == "lock" else g.reset()
    if not ok:
        raise CliError(
            EXIT_ENV_ERROR, f"{action} failed: {out}", "see heat-governor gpu-clock status"
        )
    emit_result({"ok": True, "output": out} if args.json else f"done: {out}", json_mode=args.json)


def cmd_gpu_lock(args: argparse.Namespace) -> None:
    _manual(args, "lock")


def cmd_gpu_reset(args: argparse.Namespace) -> None:
    _manual(args, "reset")


# -- registration ------------------------------------------------------------------

_DATA_DIR_HELP = "The daemon's data directory (default: daemon.data_dir, else XDG state)."


def register(sub: argparse._SubParsersAction) -> None:
    pc = sub.add_parser("config", help="The config file a running daemon re-reads.")
    csub = pc.add_subparsers(dest="config_command", required=True, parser_class=type(pc))

    def add(name: str, func, help_: str):
        q = csub.add_parser(name, help=help_)
        _add_profile_args(q)
        q.add_argument("--json", action="store_true", help=_JSON_HELP)
        q.set_defaults(func=func)
        return q

    add("overview", cmd_config_overview, "Where the config is and whether it is valid.")
    add("path", cmd_config_path, "Print the config path.")
    add("show", cmd_config_show, "The effective configuration (file over built-in profile).")
    q = add("init", cmd_config_init, "Print (or --write) a commented config from the profile.")
    q.add_argument("--write", action="store_true", help="Write it to the config path.")
    q.add_argument("--force", action="store_true", help="Replace an existing file.")
    q = add("check", cmd_config_check, "Validate a config file (exit 1 when invalid).")
    q.add_argument("file", nargs="?", help="File to check (default: the config path).")

    pg = sub.add_parser("gpu-clock", help="The GPU clock ceiling (needs the root helper).")
    gsub = pg.add_subparsers(dest="gpu_command", required=True, parser_class=type(pg))
    for name, func, help_ in (
        ("overview", cmd_gpu_status, "Is the control available, and the bands in force."),
        ("status", cmd_gpu_status, "Same as overview."),
        ("helper", cmd_gpu_helper, "Print the root helper and how to install it."),
    ):
        q = gsub.add_parser(name, help=help_)
        _add_profile_args(q)
        q.add_argument("--data-dir", help=_DATA_DIR_HELP)
        q.add_argument("--json", action="store_true", help=_JSON_HELP)
        q.set_defaults(func=func)
    q = gsub.add_parser("lock", help="Cap the GPU clock at MHZ (dry-run unless --apply).")
    q.add_argument("mhz", type=int, help="Ceiling in MHz.")
    q.add_argument("--apply", action="store_true", help="Actually lock it.")
    q.add_argument("--json", action="store_true", help=_JSON_HELP)
    q.set_defaults(func=cmd_gpu_lock)
    q = gsub.add_parser("reset", help="Remove the GPU clock ceiling (dry-run unless --apply).")
    q.add_argument("--apply", action="store_true", help="Actually reset it.")
    q.add_argument("--json", action="store_true", help=_JSON_HELP)
    q.set_defaults(func=cmd_gpu_reset)
    q = gsub.add_parser(
        "hold", help="Declare the operator's GPU ceiling (law: the daemon never goes above it)."
    )
    q.add_argument("mhz", type=int, help="Ceiling in MHz (100-5000).")
    _hold_args(q)
    q.set_defaults(func=cmd_gpu_hold)
    q = gsub.add_parser("release", help="Give back a held (or observed) operator GPU ceiling.")
    _hold_args(q)
    q.set_defaults(func=cmd_gpu_release)


def _hold_args(q: argparse.ArgumentParser) -> None:
    q.add_argument("--config", help="Config file (its daemon.data_dir picks the directory).")
    q.add_argument("--data-dir", help=_DATA_DIR_HELP)
    q.add_argument("--json", action="store_true", help=_JSON_HELP)

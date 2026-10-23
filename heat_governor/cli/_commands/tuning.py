"""The ``config`` and ``gpu-clock`` noun groups.

``config``: path / show / init / check / overview -- the file a running daemon
re-reads, so the governor can be tuned as we learn without a restart.

``gpu-clock``: overview / status / helper / lock / reset -- the GPU clock
ceiling, the one control that needs the root helper. ``lock`` and ``reset``
are dry-run unless ``--apply``.
"""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from heat_governor import config as cfgmod
from heat_governor import gpuclock
from heat_governor.cli._commands.governing import _add_profile_args, _profile, _profile_dict
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
            f"{', '.join(doc['daemon_keys']) or 'defaults'}; {doc['containers']} container rule(s)"
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


def cmd_gpu_status(args: argparse.Namespace) -> None:
    prof = _profile(args)
    g = _clock()
    doc = {
        "helper": g.helper,
        "available": g.available,
        "status": g.reason,
        "platform": prof.name,
        "gpu_clock_bands": [list(b) for b in prof.gpu_clock_bands],
    }
    if args.json:
        emit_result(doc, json_mode=True)
        return
    bands = ", ".join(f">={t:g}C -> {m} MHz" for t, m in prof.gpu_clock_bands) or "none"
    emit_result(
        f"gpu clock control: {'available' if g.available else 'UNAVAILABLE'} -- {g.reason}\n"
        f"bands ({prof.name}): {bands}\n"
        "the daemon uses them only with daemon.gpu_clock = true (or run --gpu-clock)",
        json_mode=False,
    )


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


def register(sub: argparse._SubParsersAction) -> None:
    pc = sub.add_parser("config", help="The config file a running daemon re-reads.")
    csub = pc.add_subparsers(dest="config_command", required=True, parser_class=type(pc))

    def add(name: str, func, help_: str):
        q = csub.add_parser(name, help=help_)
        _add_profile_args(q)
        q.add_argument("--json", action="store_true", help="Emit structured JSON.")
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
        q.add_argument("--json", action="store_true", help="Emit structured JSON.")
        q.set_defaults(func=func)
    q = gsub.add_parser("lock", help="Cap the GPU clock at MHZ (dry-run unless --apply).")
    q.add_argument("mhz", type=int, help="Ceiling in MHz.")
    q.add_argument("--apply", action="store_true", help="Actually lock it.")
    q.add_argument("--json", action="store_true", help="Emit structured JSON.")
    q.set_defaults(func=cmd_gpu_lock)
    q = gsub.add_parser("reset", help="Remove the GPU clock ceiling (dry-run unless --apply).")
    q.add_argument("--apply", action="store_true", help="Actually reset it.")
    q.add_argument("--json", action="store_true", help="Emit structured JSON.")
    q.set_defaults(func=cmd_gpu_reset)

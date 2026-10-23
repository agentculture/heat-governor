"""The governing verbs: ``sense``, ``capabilities``, ``run``, ``state``, ``wait``,
``report``, ``replay`` and ``unit``.

Only ``run --apply`` changes anything on the machine (docker pause/unpause and
CPU caps on the containers it governs). Everything else is read-only.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from heat_governor import client, platforms, recorder
from heat_governor.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from heat_governor.cli._output import emit_result
from heat_governor.state import default_data_dir, default_state_path

EXIT_HELD = 3  # `wait` timed out while the host was still too hot


def _config(args: argparse.Namespace):
    """The config file: --config, else the default path when it exists."""
    from heat_governor import config as cfgmod

    explicit = getattr(args, "config", None)
    path = Path(explicit) if explicit else cfgmod.default_config_path()
    try:
        return cfgmod.load(path, required=bool(explicit))
    except cfgmod.ConfigError as exc:
        raise CliError(
            EXIT_USER_ERROR,
            "invalid config: " + "; ".join(exc.problems),
            f"fix {path} (heat-governor config check)",
        ) from exc


def _profile(args: argparse.Namespace, cfg=None) -> platforms.Profile:
    cfg = cfg if cfg is not None else _config(args)
    try:
        return platforms.resolve(getattr(args, "platform", None), config=cfg.data)
    except ValueError as exc:
        raise CliError(EXIT_USER_ERROR, str(exc), "use --platform spark|thor|orin|generic") from exc


def _add_profile_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--platform", choices=[*platforms.BUILTIN, "generic"], help="Override detection."
    )
    p.add_argument(
        "--config",
        help="Config file (default $HEAT_GOVERNOR_CONFIG or ~/.config/heat-governor/config.toml).",
    )


def _profile_dict(prof: platforms.Profile) -> dict[str, object]:
    lim = prof.limits
    return {
        "name": prof.name,
        "zones": list(prof.zones),
        "limits": {
            "warm_c": lim.warm_c,
            "hot_c": lim.hot_c,
            "crit_c": lim.crit_c,
            "hysteresis_c": lim.hysteresis_c,
            "dwell_s": lim.dwell_s,
        },
        "duty_curve": [list(x) for x in prof.duty_curve],
        "cpu_bands": [list(x) for x in prof.cpu_bands],
        "gpu_clock_bands": [list(x) for x in prof.gpu_clock_bands],
        "concurrency": dict(zip(("cool", "warm", "hot", "critical"), prof.concurrency)),
        "lookahead_s": prof.lookahead_s,
        "ramp_s": prof.ramp_s,
        "duty_period_s": prof.duty_period_s,
        "gpu": prof.gpu,
        "provenance": prof.provenance,
        "notes": prof.notes,
    }


# -- sense / capabilities ---------------------------------------------------


def cmd_sense(args: argparse.Namespace) -> None:
    from heat_governor.sense import Sensor

    prof = _profile(args)
    r = Sensor(prof.zones, gpu=prof.gpu).read()
    doc = {"platform": prof.name, **r.as_dict()}
    if args.json:
        emit_result(doc, json_mode=True)
        return
    lines = [f"platform: {prof.name}", f"governing temp: {r.temp_c} C (hottest watched zone)"]
    for z in r.zones:
        mark = "*" if (not prof.zones or z.type in prof.zones) else " "
        lines.append(
            f" {mark} {z.key:<22} {'unreadable' if z.temp_c is None else f'{z.temp_c:.1f} C'}"
        )
    if r.fans:
        lines.append("fans: " + ", ".join(f"{k}={v:g}" for k, v in r.fans.items()))
    if r.gpu:
        lines.append("gpu: " + ", ".join(f"{k}={v}" for k, v in r.gpu.items()))
    lines.append("(* = watched)")
    emit_result("\n".join(lines), json_mode=False)


def cmd_capabilities(args: argparse.Namespace) -> None:
    from heat_governor.sense import capabilities

    prof = _profile(args)
    doc = {"platform": prof.name, "profile": _profile_dict(prof), **capabilities()}
    if args.json:
        emit_result(doc, json_mode=True)
        return
    ctl = doc["control"]
    lim = prof.limits
    text = [
        f"platform: {prof.name} ({prof.provenance})",
        f"limits: warm {lim.warm_c} / hot {lim.hot_c} / critical {lim.crit_c} C",
        f"watched zones: {', '.join(prof.zones) or 'all'}",
        "can read: " + ", ".join(k for k, v in doc["read"].items() if v),
        "can change: "
        + ", ".join(
            k for k in ("docker_pause", "docker_cpus", "admission_signal") if ctl[k] is True
        ),
        f"not driven: nvpmodel ({ctl['nvpmodel']}), fan ({ctl['fan_pwm']}), "
        f"gpu clocks ({ctl['gpu_clock_lock']})",
    ]
    emit_result("\n".join(text), json_mode=False)


# -- run ----------------------------------------------------------------------


def _listen(spec: str | None) -> tuple[str, int] | None:
    if not spec:
        return None
    host, _, port = spec.rpartition(":")
    try:
        return (host or "127.0.0.1", int(port))
    except ValueError as exc:
        raise CliError(EXIT_USER_ERROR, f"bad --listen {spec!r}", "use HOST:PORT") from exc


def cmd_run(args: argparse.Namespace) -> None:
    from heat_governor.daemon import Daemon, Options
    from heat_governor.docker import DEFAULT_SOCKET, Docker

    cfg = _config(args)
    prof = _profile(args, cfg)
    dcfg = cfg.daemon

    def pick(flag: str, key: str, default):
        """Command line > config file > default; remembers what the command line set."""
        value = getattr(args, flag)
        if value is not None:
            cli_set.add(key)
            return value
        return dcfg.get(key, default)

    cli_set: set[str] = set()
    apply = True if args.apply else bool(dcfg.get("apply", False))
    if args.once:
        # one tick cannot also undo what it did on exit, so --once never acts
        apply = False
    if args.apply:
        cli_set.add("apply")
    data_s = pick("data_dir", "data_dir", None)
    data = Path(data_s) if data_s else default_data_dir()
    log_path = Path(args.log) if args.log else data / "governor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(line: str) -> None:
        sys.stderr.write(line + "\n")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    sock = args.docker_socket or DEFAULT_SOCKET
    docker = None if args.no_docker or not Path(sock).exists() else Docker(sock)
    if (args.batch or args.serving or cfg.containers) and docker is None:
        raise CliError(
            EXIT_ENV_ERROR,
            f"no docker socket at {sock}",
            "containers can only be governed with docker access (or drop --batch/--serving)",
        )
    state_s = pick("state", "state", None)
    gpu_clock = pick("gpu_clock", "gpu_clock", False)
    opts = Options(
        apply=apply,
        poll_s=float(pick("poll", "poll_s", 1.0)),
        state_path=Path(state_s) if state_s else default_state_path(),
        data_dir=data,
        listen=_listen(pick("listen", "listen", None)),
        batch=tuple(args.batch or ()),
        serving=tuple(args.serving or ()),
        serving_cpu_cap=args.serving_cpu_cap,
        use_labels=False if args.no_labels else bool(dcfg.get("use_labels", True)),
        discover_every_s=float(dcfg.get("discover_every_s", 5.0)),
        containers=tuple(cfg.containers),
        gpu_clock=bool(gpu_clock),
        platform=getattr(args, "platform", None),
        config=cfg if (cfg.path and dcfg.get("reload", True)) else None,
        cli_set=frozenset(cli_set | ({"use_labels"} if args.no_labels else set())),
        record=not args.no_record and bool(dcfg.get("record", True)),
        fsync=bool(dcfg.get("fsync", True)),
        log=log,
    )
    try:
        d = Daemon(prof, opts, docker=docker)
    except OSError as exc:  # --listen port taken, unwritable data dir
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot start: {exc}",
            "pick a free --listen port (default suggestion 127.0.0.1:9476) or check --data-dir",
        ) from exc
    if args.once:
        if d.gpuclock and opts.gpu_clock:
            d.gpuclock.probe()
        d.discover()
        dec = d.tick()
        doc = {"dry_run": not apply, "state": str(opts.state_path), **dec.as_dict()}
        doc["config"] = str(cfg.path) if cfg.path and cfg.mtime else None
        doc["containers"] = [{"name": c.name, "role": c.role} for c in d.containers.values()]
        emit_result(doc if args.json else _decision_text(doc), json_mode=args.json)
        return
    d.run()  # pragma: no cover - endless loop


def _decision_text(doc: dict) -> str:
    gov = ", ".join(f"{c['name']}({c['role']})" for c in doc.get("containers", [])) or "none"
    return (
        f"level={doc['level']} admit={doc['admit']} factor={doc['concurrency_factor']} "
        f"duty={doc['duty']} cpus={doc['cpus']} gpu_mhz={doc.get('gpu_mhz')} "
        f"temp={doc['temp_c']} eff={doc['effective_c']}\n"
        f"config: {doc.get('config') or 'none (built-in profile)'}\n"
        f"governed containers: {gov}\n"
        + (">> dry run: nothing was changed; --apply to govern" if doc.get("dry_run") else "")
    )


# -- state / wait ---------------------------------------------------------------


def cmd_state(args: argparse.Namespace) -> None:
    adm = client.read(args.source, if_unknown=args.if_unknown)
    doc = {
        "admit": adm.admit,
        "level": adm.level,
        "concurrency_factor": adm.concurrency_factor,
        "reason": adm.reason,
        "temp_c": adm.temp_c,
        "state": adm.state,
    }
    if args.json:
        emit_result(doc, json_mode=True)
        return
    emit_result(
        f"{adm.level}: admit={adm.admit} factor={adm.concurrency_factor} "
        f"temp={adm.temp_c} ({adm.reason or 'ok'})",
        json_mode=False,
    )


def cmd_wait(args: argparse.Namespace) -> int:
    try:
        adm = client.wait(
            args.source, timeout=args.timeout, poll_s=args.poll, if_unknown=args.if_unknown
        )
    except TimeoutError as exc:
        if args.json:
            emit_result({"admit": False, "error": str(exc)}, json_mode=True)
        else:
            sys.stderr.write(f"held: {exc}\n")
        return EXIT_HELD
    if args.json:
        emit_result({"admit": True, "level": adm.level, "temp_c": adm.temp_c}, json_mode=True)
    else:
        emit_result(f"admitted ({adm.level}, {adm.temp_c} C)", json_mode=False)
    return 0


# -- report / replay --------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> None:
    d = Path(args.data_dir) if args.data_dir else default_data_dir()
    rows = recorder.report(d / "records", window_s=args.window)
    if args.json:
        emit_result({"records": str(d / "records"), "reboots": rows}, json_mode=True)
        return
    if not rows:
        emit_result(f"no reboot seen in {d / 'records'}", json_mode=False)
        return
    out = []
    for r in rows:
        out.append(
            f"reboot: last record {r['last_record_utc']}, restart {r['restart_utc']} "
            f"(gap {r['gap_s']} s); max {r['max_temp_c']} C in the last {r['window_s']:g} s; "
            f"last level {r['last']['level']} duty {r['last']['duty']}"
        )
    emit_result("\n".join(out), json_mode=False)


def cmd_replay(args: argparse.Namespace) -> None:
    prof = _profile(args)
    path = Path(args.trace)
    if not path.is_file():
        raise CliError(
            EXIT_USER_ERROR, f"no such trace: {path}", "pass a t1s-*.log or records .jsonl"
        )
    res = recorder.replay(path, prof)
    if args.json:
        emit_result(res, json_mode=True)
        return
    lv = ", ".join(f"{k} {v:.0f}s" for k, v in sorted(res["seconds_by_level"].items()))
    emit_result(
        f"profile {res['profile']}: {res['samples']} samples, {res['starts_in_trace']} starts\n"
        f"time by level: {lv}\nnew work held {res['share_held']:.1%} of the time; "
        f"mean batch duty {res['mean_duty']:.2f}\n"
        f"max temp {res['max_temp_c']} C, max effective {res['max_effective_c']} C",
        json_mode=False,
    )


# -- unit -------------------------------------------------------------------------

_UNIT = """\
[Unit]
Description=heat-governor: thermal governing for {platform}
After=docker.service

[Service]
ExecStart={exe} run {flags}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
"""


def cmd_unit(args: argparse.Namespace) -> None:
    exe = shutil.which("heat-governor") or f"{sys.executable} -m heat_governor"
    flags = " ".join(args.flags or []) or "--apply"
    text = _UNIT.format(platform=args.platform or "this host", exe=exe, flags=flags)
    if args.json:
        emit_result(
            {"unit": text, "path": "~/.config/systemd/user/heat-governor.service"}, json_mode=True
        )
        return
    emit_result(
        text + "\n# install: save as ~/.config/systemd/user/heat-governor.service, then\n"
        "#   systemctl --user daemon-reload && systemctl --user enable --now heat-governor\n"
        "#   loginctl enable-linger $USER   # keep it running without a login session",
        json_mode=False,
    )


# -- registration -------------------------------------------------------------------


def _source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--source",
        help="State file path or http://host:port/state (default: the local state file).",
    )
    p.add_argument(
        "--if-unknown",
        choices=("admit", "hold"),
        default="admit",
        help="What a missing or stale state means (default admit: no governor there).",
    )


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("sense", help="Read the thermal zones, fans and GPU once (read-only).")
    _add_profile_args(p)
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_sense)

    p = sub.add_parser(
        "capabilities", help="What this host lets heat-governor read and change (read-only)."
    )
    _add_profile_args(p)
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser(
        "run",
        help="Run the governor: publish the admission signal, record, and (with --apply) "
        "throttle batch containers / cap CPUs.",
    )
    _add_profile_args(p)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Change containers / GPU clocks (default: the config's daemon.apply, "
        "else report only).",
    )
    p.add_argument("--once", action="store_true", help="One tick, then exit (always a dry run).")
    p.add_argument("--poll", type=float, help="Seconds between ticks (default 1).")
    p.add_argument("--batch", action="append", metavar="NAME", help="Govern NAME as batch.")
    p.add_argument(
        "--serving", action="append", metavar="NAME", help="Govern NAME as serving (never paused)."
    )
    p.add_argument(
        "--serving-cpu-cap", action="store_true", help="Also CPU-cap the --serving containers."
    )
    p.add_argument(
        "--gpu-clock",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Lower the GPU clock ceiling per gpu_clock_bands (needs the root helper).",
    )
    p.add_argument("--no-labels", action="store_true", help="Ignore heat-governor.* labels.")
    p.add_argument("--no-docker", action="store_true", help="Signal and record only.")
    p.add_argument("--docker-socket", help="Docker socket (default /var/run/docker.sock).")
    p.add_argument("--state", help="State file to publish (default: $HEAT_GOVERNOR_STATE or XDG).")
    p.add_argument("--listen", help="Also serve GET /state on HOST:PORT (e.g. 127.0.0.1:9476).")
    p.add_argument(
        "--data-dir", help="Records, ledger and log (default ~/.local/state/heat-governor)."
    )
    p.add_argument("--log", help="Action log file (default <data-dir>/governor.log).")
    p.add_argument("--no-record", action="store_true", help="Do not write per-tick records.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON (--once).")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("state", help="Read the published admission state (read-only).")
    _source_args(p)
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser(
        "wait", help="Block until the host admits new work (exit 3 on --timeout while held)."
    )
    _source_args(p)
    p.add_argument("--timeout", type=float, help="Give up after this many seconds.")
    p.add_argument("--poll", type=float, default=1.0, help="Seconds between checks.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("report", help="Reboots seen in the records, and what preceded them.")
    p.add_argument("--data-dir", help="Default ~/.local/state/heat-governor.")
    p.add_argument("--window", type=float, default=60.0, help="Seconds before each reboot.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser(
        "replay", help="Run the controller over a recorded trace (t1s log or records .jsonl)."
    )
    _add_profile_args(p)
    p.add_argument("trace", help="Trace file.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("unit", help="Print a systemd user unit for the daemon (writes nothing).")
    p.add_argument("--platform", help="Label for the description only.")
    p.add_argument("flags", nargs="*", help="Flags for `run` (default --apply). Put after --.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_unit)

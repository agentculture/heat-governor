"""``heat-governor boundaries`` -- ``overview``, ``list``, ``add``, ``set``, ``rm``, ``apply``,
``preview``: the page-managed ``[[memory.allow]]`` rules in ``boundaries.toml``, on this box.

Every verb acts on the ``boundaries.toml`` beside the path given by ``--config`` (default: the
path the other verbs use). Rules in ``config.toml`` are listed but read-only. The CLI records
no events and writes no audit log; the daemon and the reader service own those.

The local execution is separate from parsing and printing: ``run_list(config_path)``,
``run_apply(request, config_path, ...)`` and ``run_preview(request, config_path, ...)`` take a
request dict and return a result dict; ``exit_code(result)`` maps it to the process exit code.

Machine-readable contract (``--json``; a remote reader calls
``BIN boundaries VERB --config PATH --json`` with the request body on stdin). Every result is
one JSON object on stdout with an ``outcome`` field; text-mode refusals go to stderr.

``apply`` request (stdin)::

    {"base_version": "<version from list, or 'missing' for no file>",
     "operation": "add" | "set" | "rm",
     "rule": {"name": ..., "comm": [...], "cmdline": "...", "cgroup": "...",
              "max_mib": 4096, "scope": "process" | "total"},
     "allow_kill": false}

Only those rule keys are accepted (``rm`` takes ``{"name": ...}``); ``allow_kill`` is optional.
``set`` replaces the whole rule of that name. Results (``rules`` are the boundaries.toml rules
as on disk, without ``source``)::

    {"outcome": "saved", "version": "...", "rules": [...], "guard_configured": bool,
     "action": "record|kill", "forced": bool, "processes": [...]}   exit 0
    {"outcome": "conflict", "version": "<current>", "rules": [<current>]}   exit 1
    {"outcome": "would_kill", "version": "<unchanged>", "far_over_killable": N,
     "processes": [<preview rows>], "guard_configured": bool, "action": "..."}   exit 1
    {"outcome": "invalid", "problems": ["..."], "version": "<current>"}   exit 1

``forced`` is true when ``allow_kill`` let a write through with ``processes`` far over.
``would_kill`` is decided on the real process table against the whole would-be rule set
(config.toml rules plus the new boundaries rules), whatever the configured ``action``; with
no ``[memory]`` section the guard defaults are used and ``guard_configured`` is false.

``preview`` request: ``{"rule": {<rule keys as above>}}``. Result, exit 0::

    {"outcome": "ok", "action": "...", "matching": N, "far_over_killable": N,
     "processes": [...], "guard_configured": bool}

``list`` takes no body. Result, exit 0::

    {"outcome": "ok", "rules": [{<rule keys>, "source": "config.toml"|"boundaries.toml",
     "editable": bool}], "version": "...", "exists": bool, "error": null | "<why the file is
     rejected>", "config_path": "...", "boundaries_path": "..."}

A request that is not valid JSON or has the wrong shape is ``invalid`` too. Environment
failures (unreadable config, no /proc, write failure) are a ``CliError``: exit 2, JSON on stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from heat_governor import boundaries_file
from heat_governor import config as cfgmod
from heat_governor import mempreview, memprocs
from heat_governor.cli._commands.overview import emit_overview
from heat_governor.cli._errors import EXIT_ENV_ERROR, CliError
from heat_governor.cli._output import emit_result

_JSON_HELP = "Emit structured JSON."
_RULE_KEYS = ("name", "comm", "cmdline", "cgroup", "max_mib", "scope")
_REQUEST_KEYS = {"base_version", "operation", "rule", "allow_kill"}
_OPERATIONS = ("add", "set", "rm")
_MAX_BODY = 64 * 1024
_OK_OUTCOMES = ("ok", "saved")


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def exit_code(result: dict) -> int:
    """0 for ``ok`` and ``saved``; 1 for every refusal (``outcome`` says which)."""
    return 0 if result.get("outcome") in _OK_OUTCOMES else 1


# -- local execution (request dict -> result dict) -----------------------------------------


def _invalid(problems: list[str], version: str | None = None) -> dict:
    out: dict = {"outcome": "invalid", "problems": problems}
    if version is not None:
        out["version"] = version
    return out


def _load(config_path: Path) -> cfgmod.Config:
    """config.toml with a rejected boundaries.toml tolerated (it is reported, not fatal)."""
    try:
        return cfgmod.load(config_path, last_good=cfgmod.Config(path=config_path))
    except cfgmod.ConfigError as exc:
        raise CliError(
            EXIT_ENV_ERROR, f"cannot load {config_path}: {exc}", "fix config.toml"
        ) from exc


def _config_rules(cfg: cfgmod.Config) -> list:
    return [a for a in cfg.memory.allow if a.source == cfgmod.CONFIG_FILE]


def _boundaries_path(cfg: cfgmod.Config) -> Path:
    path = cfg.boundaries_path
    if path is None:  # unreachable: _load is always given a path
        raise CliError(EXIT_ENV_ERROR, "no config path", "pass --config PATH")
    return path


def run_list(config_path: Path) -> dict:
    cfg = _load(config_path)
    bpath = _boundaries_path(cfg)
    snap = boundaries_file.read(bpath)
    rules = [
        {**boundaries_file.rule_to_dict(r), "source": cfgmod.CONFIG_FILE, "editable": False}
        for r in _config_rules(cfg)
    ]
    rules += [{**r, "source": cfgmod.BOUNDARIES_FILE, "editable": True} for r in snap.rules]
    return {
        "outcome": "ok",
        "rules": rules,
        "version": snap.version,
        "exists": snap.exists,
        "error": snap.error,
        "config_path": str(config_path),
        "boundaries_path": str(bpath),
    }


def _scan(proc_root: Path) -> dict:
    if not Path(proc_root).is_dir():
        raise CliError(EXIT_ENV_ERROR, f"cannot read {proc_root}", "run this on Linux with /proc")
    return memprocs.scan(Path(proc_root))


def _preview(cfg: cfgmod.Config, rules: list, proc_root: Path, uid, self_pid) -> dict:
    out = mempreview.preview(
        _scan(proc_root), rules, cfg.memory, Path(proc_root), uid=uid, self_pid=self_pid
    )
    out["guard_configured"] = "memory" in cfg.data
    return out


def _check_rule(rule: object, *, name_only: bool = False) -> list[str]:
    if not isinstance(rule, dict):
        return ["rule must be an object"]
    if name_only:
        extra = sorted(k for k in rule if k != "name")
        if extra or not rule.get("name"):
            return ['rm takes only a rule name: {"name": ...}']
        return []
    return cfgmod.allow_problems([rule])


def run_preview(
    request: object, config_path: Path, *, proc_root: Path = Path("/proc"), uid=None, self_pid=None
) -> dict:
    if not isinstance(request, dict) or set(request) - {"rule"}:
        return _invalid(['request must be {"rule": {...}}'])
    problems = _check_rule(request.get("rule"))
    if problems:
        return _invalid(problems)
    cfg = _load(config_path)
    out = _preview(
        cfg, [cfgmod._allow_rule(request["rule"], cfgmod.BOUNDARIES_FILE)], proc_root, uid, self_pid
    )
    return {"outcome": "ok", **out}


def _request_problems(request: object) -> list[str]:
    if not isinstance(request, dict):
        return ["request must be a JSON object"]
    problems = [f"unknown request key {k!r}" for k in sorted(set(request) - _REQUEST_KEYS)]
    if not isinstance(request.get("base_version"), str):
        problems.append("base_version must be a string")
    if request.get("operation") not in _OPERATIONS:
        problems.append(f"operation must be one of {', '.join(_OPERATIONS)}")
    if not isinstance(request.get("allow_kill", False), bool):
        problems.append("allow_kill must be true or false")
    return problems


def _next_rules(op: str, current: list[dict], rule: dict, config_names: set) -> tuple[list, list]:
    """The rules after the operation, or the problems that stop it."""
    name = rule.get("name")
    index = next((i for i, r in enumerate(current) if r.get("name") == name), None)
    if op == "add" and index is not None:
        return [], [f"a rule named {name!r} already exists; use set"]
    if op != "add" and index is None:
        where = " (it is read-only in config.toml)" if name in config_names else ""
        return [], [f"no rule named {name!r} in {cfgmod.BOUNDARIES_FILE}{where}"]
    new = list(current)
    if op == "add":
        new.append(rule)
    elif op == "set":
        new[index] = rule
    else:
        del new[index]
    return new, []


def _plan(request: dict, cfg: cfgmod.Config, current: list[dict]) -> tuple[list, list[str]]:
    op, rule = request["operation"], request.get("rule")
    problems = _check_rule(rule, name_only=op == "rm")
    if problems:
        return [], problems
    config_items = cfg.data.get("memory", {}).get("allow", [])
    names = {i.get("name") for i in config_items if isinstance(i, dict)}
    new, problems = _next_rules(op, current, rule, names)
    if problems:
        return [], problems
    problems = cfgmod.duplicate_allow_names(config_items, new)
    try:
        boundaries_file.validate(new)
    except boundaries_file.BoundariesError as exc:
        problems += exc.problems
    return new, problems


def _conflict(version: str, rules: list[dict]) -> dict:
    return {"outcome": "conflict", "version": version, "rules": rules}


def _far_over_pids(pv: dict) -> set:
    return {p["pid"] for p in pv["processes"] if p["far_over_killable"]}


def _kill_check(request, cfg, snap, new, proc_root, uid, self_pid) -> tuple[dict | None, dict]:
    """(a would_kill result or None, the preview) for the whole would-be rule set.

    Only processes the change *puts* far over count: one that is already far over under the
    rules on disk is the guard's business already and must not block an unrelated edit."""
    base = _config_rules(cfg)
    as_rules = [cfgmod._allow_rule(r, cfgmod.BOUNDARIES_FILE) for r in new]
    pv = _preview(cfg, base + as_rules, proc_root, uid, self_pid)
    before = [cfgmod._allow_rule(r, cfgmod.BOUNDARIES_FILE) for r in snap.rules]
    already = _far_over_pids(_preview(cfg, base + before, proc_root, uid, self_pid))
    victims = [p for p in pv["processes"] if p["far_over_killable"] and p["pid"] not in already]
    pv["newly_far_over"] = victims
    if not victims or request.get("allow_kill", False):
        return None, pv
    return {
        "outcome": "would_kill",
        "version": snap.version,
        "far_over_killable": len(victims),
        "processes": victims,
        "guard_configured": pv["guard_configured"],
        "action": pv["action"],
    }, pv


def _saved(res: boundaries_file.WriteResult, pv: dict) -> dict:
    victims = pv["newly_far_over"]
    return {
        "outcome": "saved",
        "version": res.version,
        "rules": res.rules,
        "guard_configured": pv["guard_configured"],
        "action": pv["action"],
        "forced": bool(victims),
        "processes": victims,
    }


def run_apply(
    request: object, config_path: Path, *, proc_root: Path = Path("/proc"), uid=None, self_pid=None
) -> dict:
    problems = _request_problems(request)
    if problems:
        return _invalid(problems)
    cfg = _load(config_path)
    bpath = _boundaries_path(cfg)
    snap = boundaries_file.read(bpath)
    if snap.error:
        return _invalid([f"{bpath.name} is rejected: {snap.error}"], snap.version)
    if snap.version != request["base_version"]:
        return _conflict(snap.version, snap.rules)
    new, problems = _plan(request, cfg, snap.rules)
    if problems:
        return _invalid(problems, snap.version)
    refused, pv = _kill_check(request, cfg, snap, new, proc_root, uid, self_pid)
    if refused:
        return refused
    try:
        res = boundaries_file.write(bpath, request["base_version"], new)
    except boundaries_file.BoundariesError as exc:
        return _invalid(exc.problems, snap.version)
    except OSError as exc:
        raise CliError(EXIT_ENV_ERROR, f"cannot write {bpath}: {exc.strerror or exc}", "") from exc
    return _conflict(res.version, res.rules) if res.conflict else _saved(res, pv)


# -- printing -------------------------------------------------------------------------------


def _rule_line(r: dict) -> str:
    match = [f"comm={','.join(r['comm'])}"] if r.get("comm") else []
    match += [f"{k}={r[k]}" for k in ("cmdline", "cgroup") if r.get(k) is not None]
    tag = "" if r.get("editable", True) else " [read-only]"
    src = f" ({r['source']})" if r.get("source") else ""
    scope = r.get("scope", "process")
    return f"{r['name']}: {' '.join(match)} max {r['max_mib']} MiB, {scope}{src}{tag}"


def _process_line(p: dict) -> str:
    return (
        f"  pid {p['pid']} {p['comm']} ({p.get('user') or p['uid']}): {p['rss_anon_mib']:.0f} MiB "
        f"anon, {p['state']}, rule {p['rule']}" + ("" if p["killable"] else f" [{p['reason']}]")
    )


def _guard_note(r: dict) -> list[str]:
    if r.get("guard_configured", True):
        return []
    return ["note: no [memory] section: the guard is off; defaults used for this check"]


def _text_invalid(r: dict) -> list[str]:
    return ["invalid:"] + [f"  {p}" for p in r["problems"]]


def _text_conflict(r: dict) -> list[str]:
    head = f"conflict: {cfgmod.BOUNDARIES_FILE} changed (now version {r['version']})"
    return [head] + [f"  {_rule_line(x)}" for x in r["rules"]]


def _text_would_kill(r: dict) -> list[str]:
    head = f"would kill: {r['far_over_killable']} process(es) far over; nothing written"
    lines = [head] + [_process_line(p) for p in r["processes"]] + _guard_note(r)
    return lines + ["hint: pass --allow-kill to write anyway"]


def _text_saved(r: dict) -> list[str]:
    lines = [f"saved {len(r['rules'])} rule(s), version {r['version']}"]
    if r["forced"]:
        lines.append(f"forced: {len(r['processes'])} process(es) far over")
    return lines + _guard_note(r)


def _text_list(r: dict) -> list[str]:
    lines = [_rule_line(x) for x in r["rules"]] or ["no boundaries"]
    lines.append(f"boundaries.toml version {r['version']}")
    if r["error"]:
        lines.append(f"error: {r['error']}")
    return lines


def _text_preview(r: dict) -> list[str]:
    head = f"{r['matching']} process(es) match, {r['far_over_killable']} far over and killable"
    return [head] + [_process_line(p) for p in r["processes"]] + _guard_note(r)


def _text(r: dict) -> str:
    kinds = {
        "invalid": _text_invalid,
        "conflict": _text_conflict,
        "would_kill": _text_would_kill,
        "saved": _text_saved,
    }
    fn = kinds.get(r["outcome"]) or (_text_list if "rules" in r else _text_preview)
    return "\n".join(fn(r))


def _finish(result: dict, args: argparse.Namespace) -> int:
    code = exit_code(result)
    if _json(args):
        emit_result(result, json_mode=True)
    else:
        emit_result(_text(result), json_mode=False, stream=None if code == 0 else sys.stderr)
    return code


def _config_path(args: argparse.Namespace) -> Path:
    return Path(args.config) if getattr(args, "config", None) else cfgmod.default_config_path()


def _read_body() -> tuple[object, str | None]:
    """The JSON request on stdin, or (None, why not)."""
    text = sys.stdin.read(_MAX_BODY + 1)
    if len(text) > _MAX_BODY:
        return None, "request body is too large"
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, f"request is not valid JSON: {exc}"


def _opts(args: argparse.Namespace) -> dict:
    return {
        "proc_root": Path(args.proc_root),
        "uid": getattr(args, "uid", None),
        "self_pid": getattr(args, "self_pid", None),
    }


def _with_body(args: argparse.Namespace, runner) -> int:
    body, why = _read_body()
    if why:
        return _finish(_invalid([why]), args)
    return _finish(runner(body, _config_path(args), **_opts(args)), args)


def cmd_list(args: argparse.Namespace) -> int:
    return _finish(run_list(_config_path(args)), args)


def cmd_apply(args: argparse.Namespace) -> int:
    return _with_body(args, run_apply)


def cmd_preview(args: argparse.Namespace) -> int:
    return _with_body(args, run_preview)


def _flag_rule(args: argparse.Namespace) -> dict:
    rule: dict = {"name": args.name}
    if args.comm:
        rule["comm"] = list(args.comm)
    for key in ("cmdline", "cgroup", "max_mib", "scope"):
        if getattr(args, key) is not None:
            rule[key] = getattr(args, key)
    return rule


def _flag_request(args: argparse.Namespace, op: str, config_path: Path) -> dict:
    snap = boundaries_file.read(_boundaries_path(_load(config_path)))
    rule = {"name": args.name} if op == "rm" else _flag_rule(args)
    if op == "set":  # flags override the current rule; unset flags keep its values
        old = next((r for r in snap.rules if r.get("name") == args.name), {})
        rule = {**old, **rule}
    req: dict = {"base_version": snap.version, "operation": op, "rule": rule}
    if getattr(args, "allow_kill", False):
        req["allow_kill"] = True
    return req


def _flag_verb(op: str):
    def run(args: argparse.Namespace) -> int:
        path = _config_path(args)
        result = run_apply(_flag_request(args, op, path), path, **_opts(args))
        return _finish(result, args)

    run.__name__ = f"cmd_{op}"
    return run


cmd_add, cmd_set, cmd_rm = _flag_verb("add"), _flag_verb("set"), _flag_verb("rm")


# -- overview -------------------------------------------------------------------------------


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "heat-governor boundaries",
        [
            {
                "title": "Verbs",
                "items": [
                    "boundaries overview -- this description",
                    "boundaries list [--config P] -- every [[memory.allow]] rule with its source "
                    "file and the boundaries.toml version (config.toml rules are read-only)",
                    "boundaries add --name N [--comm C]... [--cmdline G] [--cgroup G] "
                    "--max-mib M [--scope process|total] [--allow-kill]",
                    "boundaries set --name N [same flags] -- change flags of an existing rule",
                    "boundaries rm --name N [--allow-kill]",
                    "boundaries apply [--config P] -- one JSON request on stdin: base_version, "
                    "operation add|set|rm, rule, allow_kill",
                    "boundaries preview [--config P] -- a draft rule as JSON on stdin: what it "
                    "would match right now",
                ],
            },
            {
                "title": "Notes",
                "items": [
                    "all verbs act on the boundaries.toml beside --config (default: the config "
                    "path the other verbs use)",
                    "a write that would put a killable own-user process far over its boundary "
                    "is refused (outcome would_kill) unless allow_kill is set",
                    "--json results carry an 'outcome': ok, saved, conflict, would_kill, invalid",
                    "exit codes: 0 ok/saved, 1 refused or bad input, 2 environment error",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_overview(args)


def _common(p: argparse.ArgumentParser, *, proc: bool = True) -> None:
    p.add_argument("--config", help="config.toml path; boundaries.toml is its sibling.")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    if proc:
        p.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
        p.add_argument("--uid", type=int, default=None, help=argparse.SUPPRESS)
        p.add_argument("--self-pid", type=int, default=None, help=argparse.SUPPRESS)


def _rule_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", required=True, help="Rule name.")
    p.add_argument("--comm", action="append", help="Process name; repeatable.")
    p.add_argument("--cmdline", help="Command-line glob.")
    p.add_argument("--cgroup", help="cgroup glob.")
    p.add_argument("--max-mib", type=int, dest="max_mib", help="Boundary in MiB.")
    p.add_argument("--scope", help="process or total.")
    p.add_argument("--allow-kill", action="store_true", help="Write even if it would kill.")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "boundaries", help="Memory boundaries (see 'heat-governor boundaries overview')."
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun = p.add_subparsers(dest="boundaries_command", parser_class=type(p))

    ov = noun.add_parser("overview", help="Describe the boundaries noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)

    verbs = [
        ("list", cmd_list, "Rules from both files with the boundaries.toml version.", False),
        ("apply", cmd_apply, "Apply one JSON request from stdin.", True),
        ("preview", cmd_preview, "Preview a draft rule from stdin.", True),
        ("add", cmd_add, "Add a rule.", True),
        ("set", cmd_set, "Change a rule.", True),
        ("rm", cmd_rm, "Remove a rule.", True),
    ]
    for name, func, help_text, _ in verbs:
        v = noun.add_parser(name, help=help_text)
        _common(v)
        if name in ("add", "set"):
            _rule_flags(v)
        if name == "rm":
            v.add_argument("--name", required=True, help="Rule name.")
            v.add_argument("--allow-kill", action="store_true", help="Write even if it would kill.")
        v.set_defaults(func=func)

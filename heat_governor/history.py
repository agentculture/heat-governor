"""Trend history from a machine's own record files (``<data_dir>/records/YYYYMMDD.jsonl``).

:data:`HISTORY_SCRIPT` is ONE self-contained, stdlib-only Python program. The very same text
runs on a remote box (``nice -n 19 python3 -c <script> <since> <until> <max_points>`` over
the reader's existing ssh connection, see :meth:`heat_governor.transport.SshTransport.history`)
and on this machine (:func:`run_local`), so the tests exercise exactly what the boxes run.
It only reads files; it never writes anywhere.

Contract (arguments are numbers only; :func:`build_args` validates them before any process
starts, so no other text ever reaches a remote shell):

* resolves the records dir as the daemon does by default:
  ``$XDG_STATE_HOME/heat-governor/records``, else ``~/.local/state/heat-governor/records``
  (a daemon configured with another ``data_dir`` is not found: the reader then falls back to
  its own buffer);
* opens only the UTC day files overlapping ``[since, until]`` (a missing day is skipped);
* skips torn or garbage lines and ``event`` lines, and any record outside the window;
* splits the window into ``max_points`` equal buckets; each non-empty bucket gives ``t`` (its
  last sample's time), the maximum ``temp_c``, ``gpu_temp_c`` and ``mem_used_pct``, the most
  severe ``level`` and the lowest ``concurrency_factor``; empty buckets are omitted (the page
  draws gaps);
* prints one compact JSON object
  ``{"schema":1,"samples":[...],"lines_read":N,"parsed":M,"stride":k,"files":[...]}``.

Load control (the boxes are busy serving models): the process runs under ``nice -n 19``;
a line is only JSON-parsed when it carries ``"temp_c"`` and its leading ``{"t":<number>``
falls inside the window (read without parsing the line); for windows longer than
:data:`LONG_RANGE_S` only every k-th tick line is parsed, k chosen from the files' sizes so
that at most about :data:`MAX_PARSED` lines are parsed for the whole window. **Buckets of
such a long window (the 7 d range) are therefore sampled maxima, not exact maxima.** The
script stops itself past its own deadline (9 s, or 18 s for a long window), inside the
caller's hard subprocess timeout (:func:`timeout_for`).

Records carry no concurrency factor; it is derived from the level with the default
``Profile.concurrency`` map (a box with an overridden map shows the default factors here).
GPU temperature is the record's ``gpu["temp_c"]`` (the same field the daemon publishes as
``gpu_temp_c``), else null.
"""

from __future__ import annotations

import json
import math
import subprocess  # nosec B404
import sys
import time

SCHEMA = 1
LONG_RANGE_S = 26 * 3600
MAX_RANGE_S = 8 * 86_400
MAX_PARSED = 50_000
MAX_POINTS = 2000
MAX_OUTPUT_BYTES = 4 << 20
LEVELS = ("cool", "warm", "hot", "critical")
# the default client concurrency factor per level (heat_governor.platforms.Profile.concurrency);
# records do not carry the factor, so it is derived from the recorded level
CONCURRENCY = (1.0, 0.5, 0.0, 0.0)
PYTHON = sys.executable

HISTORY_SCRIPT = """import json, os, sys, time
from datetime import datetime, timedelta, timezone
LEVELS = %s
CF = %r
LONG = %d
MAX_PARSED = %d
def main(argv):
    since, until, n = float(argv[1]), float(argv[2]), int(argv[3])
    if not (0 <= since < until) or n < 1:
        raise SystemExit(2)
    start = time.monotonic()
    span = until - since
    DEADLINE = 18.0 if span > LONG else 9.0
    home = os.path.expanduser("~")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    rdir = os.path.join(base, "heat-governor", "records")
    day = datetime.fromtimestamp(since, timezone.utc).date()
    last = datetime.fromtimestamp(until, timezone.utc).date()
    paths = []
    while day <= last:
        p = os.path.join(rdir, day.strftime("%%Y%%m%%d") + ".jsonl")
        if os.path.isfile(p):
            paths.append(p)
        day += timedelta(days=1)
    stride = 1
    if span > LONG and paths:
        size = sum(os.path.getsize(p) for p in paths)
        with open(paths[0], "rb") as fh:
            head = fh.read(65536)
        avg = len(head) / max(1, head.count(b"\\n"))
        stride = max(1, -(-int(size / max(avg, 1.0)) // MAX_PARSED))
    width = span / n
    buckets = {}
    lines = parsed = ticks = 0
    for p in paths:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                lines += 1
                if lines %% 4096 == 0 and time.monotonic() - start > DEADLINE:
                    raise SystemExit(3)
                if "\\"temp_c\\"" not in line:
                    continue
                if line.startswith("{\\"t\\":"):
                    end = line.find(",", 5)
                    try:
                        t = float(line[5:end])
                    except ValueError:
                        t = None
                    if t is not None and not since <= t <= until:
                        continue
                ticks += 1
                if (ticks - 1) %% stride:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                parsed += 1
                if not isinstance(r, dict) or "event" in r:
                    continue
                t = r.get("t")
                if not isinstance(t, (int, float)) or not since <= t <= until:
                    continue
                k = min(n - 1, int((t - since) / width))
                temp = num(r.get("temp_c"))
                gpu = r.get("gpu")
                gpu = num(gpu.get("temp_c")) if isinstance(gpu, dict) else None
                system = r.get("system")
                mem = num(system.get("mem:used_pct")) if isinstance(system, dict) else None
                lvl = LEVELS.index(r["level"]) if r.get("level") in LEVELS else None
                b = buckets.get(k)
                if b is None:
                    buckets[k] = [t, temp, gpu, lvl, mem]
                    continue
                b[0] = max(b[0], t)
                b[1] = top(b[1], temp)
                b[2] = top(b[2], gpu)
                b[3] = top(b[3], lvl)
                b[4] = top(b[4], mem)
    samples = []
    for k in sorted(buckets):
        t, temp, gpu, lvl, mem = buckets[k]
        samples.append({"t": t, "temp_c": temp, "gpu_temp_c": gpu,
                        "level": None if lvl is None else LEVELS[lvl],
                        "concurrency_factor": None if lvl is None else CF[lvl],
                        "mem_used_pct": mem})
    doc = {"schema": 1, "samples": samples, "lines_read": lines, "parsed": parsed,
           "stride": stride, "files": [os.path.basename(p) for p in paths]}
    sys.stdout.write(json.dumps(doc, separators=(",", ":")))
def num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)
def top(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
main(sys.argv)
""" % (
    json.dumps(LEVELS).replace("[", "(").replace("]", ")"),
    CONCURRENCY,
    LONG_RANGE_S,
    MAX_PARSED,
)


def timeout_for(span_s: float) -> float:
    """The hard subprocess timeout for a window: 20 s for a long (7 d) one, else 10 s."""
    return 20.0 if span_s > LONG_RANGE_S else 10.0


def _real(v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"not a finite number: {v!r}")
    return float(v)


def build_args(since: float, until: float, max_points: int) -> list[str]:
    """The script's three arguments as plain number strings; ``ValueError`` for anything
    that is not a sane window (finite, ``0 <= since < until``, at most 8 days, sensible
    point count). This is the only text that reaches the remote command."""
    s, u = _real(since), _real(until)
    if not 0 <= s < u or u - s > MAX_RANGE_S or u > 4e9:
        raise ValueError(f"bad window {s!r}..{u!r}")
    if isinstance(max_points, bool) or not isinstance(max_points, int):
        raise ValueError(f"max_points must be an int: {max_points!r}")
    if not 1 <= max_points <= MAX_POINTS:
        raise ValueError(f"max_points out of range: {max_points}")
    return [f"{s:.3f}", f"{u:.3f}", str(max_points)]


def _opt_num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def parse_output(raw: bytes | str) -> dict:
    """Parse and sanitise the script's output; ``ValueError`` when it is not a schema-1
    document. Malformed samples are dropped; unknown fields are not passed on."""
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError("history output too large")
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    doc = json.loads(text)  # ValueError on empty or non-JSON
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("not a schema-1 history document")
    if not isinstance(doc.get("samples"), list):
        raise ValueError("history document has no sample list")
    samples = []
    for s in doc["samples"]:
        if not isinstance(s, dict) or _opt_num(s.get("t")) is None:
            continue
        level = s.get("level") if s.get("level") in LEVELS else None
        samples.append(
            {
                "t": float(s["t"]),
                "temp_c": _opt_num(s.get("temp_c")),
                "gpu_temp_c": _opt_num(s.get("gpu_temp_c")),
                "level": level,
                "concurrency_factor": _opt_num(s.get("concurrency_factor")),
                "mem_used_pct": _opt_num(s.get("mem_used_pct")),
            }
        )
    files = doc.get("files")
    return {
        "schema": SCHEMA,
        "samples": samples,
        "lines_read": doc.get("lines_read") if isinstance(doc.get("lines_read"), int) else None,
        "parsed": doc.get("parsed") if isinstance(doc.get("parsed"), int) else None,
        "stride": doc.get("stride") if isinstance(doc.get("stride"), int) else None,
        "files": [f for f in files if isinstance(f, str)] if isinstance(files, list) else [],
    }


def run_local(since: float, until: float, max_points: int, *, python: str | None = None) -> dict:
    """Run :data:`HISTORY_SCRIPT` on this machine (niced, no ssh) against its own records.
    Raises ``ValueError`` (bad arguments or output), ``OSError`` (cannot start) or
    ``subprocess.TimeoutExpired`` / ``RuntimeError`` (deadline, non-zero exit)."""
    args = build_args(since, until, max_points)
    argv = ["nice", "-n", "19", python or PYTHON, "-c", HISTORY_SCRIPT, *args]
    deadline = timeout_for(float(args[1]) - float(args[0]))
    began = time.monotonic()
    proc = subprocess.run(  # nosec B603  # NOSONAR python:S4721 -- fixed argv, numbers only
        argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=deadline, check=False
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(
            f"history exited {proc.returncode} after {time.monotonic() - began:.1f}s: "
            f"{err[-1] if err else 'no output'}"
        )
    return parse_output(proc.stdout)

"""Transports a fleet view uses to read a machine's ``/state`` document.

Public API (both transports share it):

* ``SshTransport(host, *, timeout_s, state_dir=None, ssh="ssh")`` -- reads the remote daemon's
  ``GET /state`` over ssh. ``fetch() -> dict`` returns the parsed document or raises
  :class:`Unreachable` (timeout, ssh failure, empty or non-JSON output) with a reason.
  ``close()`` tears the persistent connection down; idempotent; also a context manager.
* ``LocalTransport(*, timeout_s, port=9476)`` -- same interface for the daemon on this machine,
  read over ``http://127.0.0.1:<port>/state`` (see the class for why not the state file).
* Both also have ``history(since, until, max_points) -> dict``: trend history from the machine's
  record files via :data:`heat_governor.history.HISTORY_SCRIPT` -- over ssh as the fixed command
  ``nice -n 19 python3 -c <script> <since> <until> <max_points>`` (numbers validated before any
  process starts; same options and ControlPath as ``fetch``), or run locally through this
  python. Bounded by :func:`heat_governor.history.timeout_for` (10 s, 20 s over 26 h); raises
  :class:`Unreachable` on any failure.

The ssh command is always an argv list (never a shell). The host is re-validated with
:func:`heat_governor.fleet.validate_host`, passed after ``--``, and the remote command is a fixed
string whose only variable part is the numeric timeout: no user text reaches the remote shell.
Connections are reused through OpenSSH ``ControlMaster=auto`` with a ``ControlPath`` of
``<state_dir>/ssh/<sha1(host)[:10]>.sock`` (the dir is mode 0700; the short hash keeps the unix
socket path under the ~104 character limit).

Timeouts: ``ConnectTimeout``, the remote ``curl -m`` and a hard subprocess deadline of
``timeout_s`` plus a 0.5 s grace all bound one poll; on the deadline the ssh child is killed.
Output is capped while it is read (:data:`MAX_DOC_BYTES` for ``/state``,
:data:`MAX_HISTORY_BYTES` for history): past the cap the child is killed and the read fails
as :class:`Unreachable` ("output too large").
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shlex
import subprocess  # nosec B404
import sys
import time
import urllib.request
from pathlib import Path

from heat_governor.cli._errors import EXIT_ENV_ERROR, CliError
from heat_governor.fleet import validate_host
from heat_governor.history import HISTORY_SCRIPT, build_args, parse_output, run_local, timeout_for
from heat_governor.state import default_data_dir

STATE_PORT = 9476
MAX_DOC_BYTES = 1 << 20  # a state document is a few KiB; refuse anything absurd (as pair.py)
MAX_HISTORY_BYTES = 8 << 20  # a history answer is at most a few hundred KiB
MAX_ERR_BYTES = 64 << 10  # stderr keeps only its tail (the last line is the reason shown)
_CHUNK = 65536
GRACE_S = 0.5
PERSIST_S = 60
ALIVE_S = 5
PYTHON = sys.executable  # the interpreter LocalTransport.history runs the script with


class Unreachable(CliError):
    """The machine could not be read this poll; ``message`` says why."""

    def __init__(self, message: str) -> None:
        super().__init__(
            EXIT_ENV_ERROR, message, "check the host, ssh keys and that the daemon runs"
        )


def _num(x: float) -> str:
    return f"{x:g}"


def parse_state(raw: bytes | str) -> dict:
    """Parse a /state body; raise Unreachable on empty or non-object JSON."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    if not text.strip():
        raise Unreachable("empty response")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise Unreachable(f"response is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise Unreachable("response is not a JSON object")
    return doc


class SshTransport:
    def __init__(
        self,
        host: str,
        *,
        timeout_s: float,
        state_dir: Path | str | None = None,
        ssh: str = "ssh",
    ) -> None:
        self.host = validate_host(host)
        self.timeout_s = float(timeout_s)
        self.ssh = ssh
        base = Path(state_dir) if state_dir is not None else default_data_dir()
        self._dir = base / "ssh"
        digest = hashlib.sha1(self.host.encode(), usedforsecurity=False).hexdigest()[:10]
        self.control_path = str(self._dir / f"{digest}.sock")
        self._used = False
        self._closed = False

    def _base_argv(self) -> list[str]:
        ct = max(1, int(self.timeout_s))
        return [
            self.ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={ct}",
            "-o",
            f"ServerAliveInterval={ALIVE_S}",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPersist={PERSIST_S}",
            "-o",
            f"ControlPath={self.control_path}",
        ]

    def argv(self) -> list[str]:
        remote = f"curl -s -m {_num(self.timeout_s)} 127.0.0.1:{STATE_PORT}/state"
        return [*self._base_argv(), "--", self.host, remote]

    def history_argv(self, since: float, until: float, max_points: int) -> list[str]:
        """The fixed records-history command: the same ssh options and ControlPath as
        :meth:`argv`, ``--``, the validated host, then ``nice -n 19 python3 -c <script>``
        and three numbers (``ValueError`` from :func:`build_args` for anything else)."""
        args = build_args(since, until, max_points)
        remote = " ".join(["nice -n 19 python3 -c", shlex.quote(HISTORY_SCRIPT), *args])
        return [*self._base_argv(), "--", self.host, remote]

    def _run(
        self,
        argv: list[str],
        deadline: float,
        label: float | None = None,
        cap: int | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run ``argv``; return ``(code, stdout, stderr)``. The child is killed, and
        :class:`Unreachable` raised, on the deadline or once stdout exceeds ``cap`` bytes
        (a broken or hostile remote cannot make the reader buffer without bound);
        ``cap`` defaults to :data:`MAX_DOC_BYTES`."""
        cap = MAX_DOC_BYTES if cap is None else cap
        try:
            proc = (
                subprocess.Popen(  # nosec B603  # NOSONAR python:S4721 -- argv list, validated host
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        except OSError as exc:
            raise Unreachable(f"cannot run {self.ssh}: {exc}") from exc
        end = time.monotonic() + deadline
        try:
            out, err = _collect(proc, end, cap)
            proc.wait(timeout=max(0.0, end - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            _kill(proc)
            shown = self.timeout_s if label is None else label
            raise Unreachable(f"timed out after {_num(shown)}s") from exc
        except _TooLarge as exc:
            _kill(proc)
            raise Unreachable(f"output too large (over {cap} bytes)") from exc
        return proc.returncode, out, err

    def _prepare(self) -> None:
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        self._used = True
        self._closed = False

    @staticmethod
    def _check(code: int, err: bytes) -> None:
        if code != 0:
            reason = err.decode("utf-8", "replace").strip().splitlines()
            raise Unreachable(f"ssh exited {code}: {reason[-1] if reason else 'no output'}")

    def fetch(self) -> dict:
        self._prepare()
        code, out, err = self._run(self.argv(), self.timeout_s + GRACE_S)
        self._check(code, err)
        return parse_state(out)

    def history(self, since: float, until: float, max_points: int) -> dict:
        """Records-based history of the remote box (see :mod:`heat_governor.history`).
        Bounded by :func:`timeout_for` (10 s, 20 s for a window over 26 h); raises
        :class:`Unreachable` on bad arguments (before any process starts), ssh failure,
        timeout or bad output."""
        try:
            argv = self.history_argv(since, until, max_points)
        except ValueError as exc:
            raise Unreachable(f"refused history arguments: {exc}") from exc
        limit = timeout_for(float(until) - float(since))
        self._prepare()
        code, out, err = self._run(argv, limit + GRACE_S, label=limit, cap=MAX_HISTORY_BYTES)
        self._check(code, err)
        try:
            return parse_output(out)
        except ValueError as exc:
            raise Unreachable(f"bad history output: {exc}") from exc

    def close(self) -> None:
        if self._closed or not self._used:
            return
        self._closed = True
        argv = [*self._base_argv(), "-O", "exit", "--", self.host]
        try:
            self._run(argv, 3.0)
        except Unreachable:
            pass  # best effort: the socket is removed below either way
        try:
            Path(self.control_path).unlink()
        except OSError:
            pass

    def __enter__(self) -> SshTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _TooLarge(Exception):
    """stdout passed its cap."""


def _collect(proc: subprocess.Popen, end: float, cap: int) -> tuple[bytes, bytes]:
    """Read the child's stdout and stderr until both close: ``TimeoutExpired`` past the
    monotonic ``end``, :class:`_TooLarge` once stdout exceeds ``cap``. stderr keeps only
    its last :data:`MAX_ERR_BYTES`."""
    out, err = bytearray(), bytearray()
    with selectors.DefaultSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ, out)  # type: ignore[arg-type]
        sel.register(proc.stderr, selectors.EVENT_READ, err)  # type: ignore[arg-type]
        while sel.get_map():
            left = end - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(proc.args, left)
            for key, _ in sel.select(left):
                _drain(sel, key, out, cap)
    return bytes(out), bytes(err)


def _drain(
    sel: selectors.BaseSelector, key: selectors.SelectorKey, out: bytearray, cap: int
) -> None:
    """Read one ready chunk into its buffer: unregister the pipe at EOF, raise
    :class:`_TooLarge` once stdout passes ``cap``, keep only stderr's tail."""
    chunk = os.read(key.fd, _CHUNK)
    if not chunk:
        sel.unregister(key.fileobj)
        return
    buf: bytearray = key.data
    buf += chunk
    if buf is out:
        if len(buf) > cap:
            raise _TooLarge
    elif len(buf) > MAX_ERR_BYTES:
        del buf[:-MAX_ERR_BYTES]


def _kill(proc: subprocess.Popen) -> None:
    proc.kill()
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass  # a lingering master holding the pipes must not block the caller


class LocalTransport:
    """Read the daemon on this machine over ``http://127.0.0.1:<port>/state``.

    Why HTTP and not the state file (``state.default_state_path``): the file path is per
    user (``$XDG_RUNTIME_DIR``), so a reader running as another user or service would look
    in the wrong place; the loopback endpoint is the same one :class:`SshTransport` curls
    on every remote box, so the fleet has one convention (the daemon runs with
    ``--listen 127.0.0.1:9476``) and the local box reads exactly the document its remote
    peers would. A daemon started without ``--listen`` shows as unreachable with the
    reason, rather than silently read from a stale file.
    """

    def __init__(self, *, timeout_s: float, port: int = STATE_PORT) -> None:
        self.timeout_s = float(timeout_s)
        self.port = port

    def fetch(self) -> dict:
        url = f"http://127.0.0.1:{self.port}/state"  # NOSONAR python:S5332 -- loopback
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:  # nosec B310
                raw = resp.read(MAX_DOC_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise Unreachable(f"local daemon unreachable: {exc}") from exc
        if len(raw) > MAX_DOC_BYTES:
            raise Unreachable("state document too large")
        return parse_state(raw)

    def history(self, since: float, until: float, max_points: int) -> dict:
        """Records-based history of this machine: the same script as the remote boxes,
        run niced through the local python (no ssh). Raises :class:`Unreachable`."""
        try:
            return run_local(since, until, max_points, python=PYTHON)
        except subprocess.TimeoutExpired as exc:
            raise Unreachable(f"local history timed out after {_num(exc.timeout)}s") from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise Unreachable(f"local history failed: {exc}") from exc

    def close(self) -> None:
        return None

    def __enter__(self) -> LocalTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

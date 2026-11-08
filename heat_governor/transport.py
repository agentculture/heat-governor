"""Transports a fleet view uses to read a machine's ``/state`` document.

Public API (both transports share it):

* ``SshTransport(host, *, timeout_s, state_dir=None, ssh="ssh")`` -- reads the remote daemon's
  ``GET /state`` over ssh. ``fetch() -> dict`` returns the parsed document or raises
  :class:`Unreachable` (timeout, ssh failure, empty or non-JSON output) with a reason.
  ``close()`` tears the persistent connection down; idempotent; also a context manager.
* ``LocalTransport(*, timeout_s, port=9476)`` -- same interface for the daemon on this machine.

The ssh command is always an argv list (never a shell). The host is re-validated with
:func:`heat_governor.fleet.validate_host`, passed after ``--``, and the remote command is a fixed
string whose only variable part is the numeric timeout: no user text reaches the remote shell.
Connections are reused through OpenSSH ``ControlMaster=auto`` with a ``ControlPath`` of
``<state_dir>/ssh/<sha1(host)[:10]>.sock`` (the dir is mode 0700; the short hash keeps the unix
socket path under the ~104 character limit).

Timeouts: ``ConnectTimeout``, the remote ``curl -m`` and a hard subprocess deadline of
``timeout_s`` plus a 0.5 s grace all bound one poll; on the deadline the ssh child is killed.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # nosec B404
import urllib.request
from pathlib import Path

from heat_governor.cli._errors import EXIT_ENV_ERROR, CliError
from heat_governor.fleet import validate_host
from heat_governor.state import default_data_dir

STATE_PORT = 9476
GRACE_S = 0.5
PERSIST_S = 60
ALIVE_S = 5


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

    def _run(self, argv: list[str], deadline: float) -> tuple[int, bytes, bytes]:
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
        try:
            out, err = proc.communicate(timeout=deadline)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            try:
                proc.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass  # a lingering master holding the pipes must not block the caller
            raise Unreachable(f"timed out after {_num(self.timeout_s)}s") from exc
        return proc.returncode, out, err

    def fetch(self) -> dict:
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        self._used = True
        self._closed = False
        code, out, err = self._run(self.argv(), self.timeout_s + GRACE_S)
        if code != 0:
            reason = err.decode("utf-8", "replace").strip().splitlines()
            raise Unreachable(f"ssh exited {code}: {reason[-1] if reason else 'no output'}")
        return parse_state(out)

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


class LocalTransport:
    """Read the daemon on this machine over 127.0.0.1."""

    def __init__(self, *, timeout_s: float, port: int = STATE_PORT) -> None:
        self.timeout_s = float(timeout_s)
        self.port = port

    def fetch(self) -> dict:
        url = f"http://127.0.0.1:{self.port}/state"  # NOSONAR python:S5332 -- loopback
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:  # nosec B310
                return parse_state(resp.read())
        except (OSError, ValueError) as exc:
            raise Unreachable(f"local daemon unreachable: {exc}") from exc

    def close(self) -> None:
        return None

    def __enter__(self) -> LocalTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

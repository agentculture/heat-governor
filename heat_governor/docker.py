"""A tiny Docker Engine API client over the unix socket (stdlib only).

Why not the ``docker`` CLI: each CLI call costs tens of milliseconds of process
start-up, which bounds how finely a duty cycle can pause and resume. The API
call is a single request on a local socket. No root is needed: membership of
the ``docker`` group is enough, the same access ``docker pause`` needs.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.parse
from dataclasses import dataclass

DEFAULT_SOCKET = "/var/run/docker.sock"
LABEL_ROLE = "heat-governor.role"  # "batch" | "serving"
LABEL_CPU_CAP = "heat-governor.cpu-cap"  # "true" lets the governor cap a serving container
ROLES = ("batch", "serving")


class DockerError(RuntimeError):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    state: str  # running | paused | ...
    role: str | None  # from the label, or set explicitly by the operator
    cpu_cap: bool
    nano_cpus: int


class Docker:
    def __init__(self, socket_path: str = DEFAULT_SOCKET, timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        conn = _UnixConnection(self.socket_path, self.timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if data is not None else {}
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError(f"docker socket {self.socket_path}: {exc!r}") from exc
        finally:
            conn.close()
        try:
            payload: object = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode(errors="replace")
        return resp.status, payload

    def _ok(self, method: str, path: str, body: dict | None = None) -> object:
        status, payload = self._req(method, path, body)
        # 304 = already in that state (e.g. pause on a paused container is 409 though)
        if status >= 400:
            msg = payload.get("message") if isinstance(payload, dict) else payload
            raise DockerError(f"{method} {path}: HTTP {status}: {msg}")
        return payload

    def inspect(self, ref: str) -> Container:
        info = self._ok("GET", f"/containers/{urllib.parse.quote(ref)}/json")
        assert isinstance(info, dict)
        labels = (info.get("Config") or {}).get("Labels") or {}
        return Container(
            id=info["Id"],
            name=str(info.get("Name", "")).lstrip("/"),
            state=(info.get("State") or {}).get("Status", "unknown"),
            role=labels.get(LABEL_ROLE) if labels.get(LABEL_ROLE) in ROLES else None,
            cpu_cap=str(labels.get(LABEL_CPU_CAP, "")).lower() == "true",
            nano_cpus=int((info.get("HostConfig") or {}).get("NanoCpus") or 0),
        )

    def labelled(self) -> list[Container]:
        """Running or paused containers that carry the role label."""
        q = urllib.parse.quote(json.dumps({"label": [LABEL_ROLE]}))
        rows = self._ok("GET", f"/containers/json?filters={q}")
        out = []
        for row in rows if isinstance(rows, list) else []:
            try:
                out.append(self.inspect(row["Id"]))
            except DockerError:
                continue  # gone between list and inspect
        return out

    def pause(self, ref: str) -> None:
        self._ok("POST", f"/containers/{urllib.parse.quote(ref)}/pause")

    def unpause(self, ref: str) -> None:
        self._ok("POST", f"/containers/{urllib.parse.quote(ref)}/unpause")

    def set_nano_cpus(self, ref: str, nano: int) -> None:
        self._ok("POST", f"/containers/{urllib.parse.quote(ref)}/update", {"NanoCpus": int(nano)})

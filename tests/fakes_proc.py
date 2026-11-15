"""Fake /proc tree for memprocs tests."""

from __future__ import annotations

from pathlib import Path


def add_proc(
    root: Path,
    pid: int,
    *,
    ppid: int = 1,
    uid: int = 1000,
    comm: str = "proc",
    cmdline: list[str] | None = None,
    anon_kib: int = 0,
    file_kib: int = 0,
    start: int = 100,
    cgroup: str = "/user.slice/session-1.scope",
) -> Path:
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text(
        f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nPPid:\t{ppid}\n"
        f"VmRSS:\t{anon_kib + file_kib} kB\nRssAnon:\t{anon_kib} kB\nRssFile:\t{file_kib} kB\n"
    )
    tail = " ".join(["0"] * 16 + [str(start)] + ["0"] * 5)  # fields 4..; start is field 22
    # fields after ")": state, ppid, then 17 more up to starttime at index 19
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 5
    del tail
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
    (d / "cmdline").write_text("\0".join(cmdline or [comm]) + "\0")
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    return d

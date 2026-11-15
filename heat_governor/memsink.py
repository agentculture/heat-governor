"""Hand memory-guard events to disk and S3 without ever blocking the guard thread.

The guard emits ``memory_kill`` / ``oom_kill`` / ``memory_budget_exhausted`` rows from its own
thread. Writing them (a recorder ``fsync`` under a lock shared with the tick, then a spool
``fsync``) can stall for seconds exactly when memory pressure slows writeback, and a stalled
guard misses polls. :class:`MemorySink` puts the write on its own thread behind a bounded
queue: :meth:`submit` never blocks, a full queue drops (and counts) instead of waiting, and
:meth:`stop` drains what is queued. Before :meth:`start` (``run --once``, tests) events are
handled inline.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable

_STOP = object()


def _noop(_msg: str) -> None:
    return None


class MemorySink:
    def __init__(
        self,
        handle: Callable[[dict], None],
        *,
        maxsize: int = 1024,
        log: Callable[[str], None] = _noop,
    ) -> None:
        self._handle = handle
        self._log = log
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self.dropped = 0
        self._warned = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._thread = threading.Thread(target=self._run, name="heat-governor-memsink", daemon=True)
        self._thread.start()

    def submit(self, ev: dict) -> None:
        """Queue ``ev``; never blocks. Inline when the sink is not running."""
        if not self.running:
            self._safe(ev)
            return
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            self.dropped += 1
            if not self._warned:
                self._warned = True
                self._log("memory sink: queue full, events dropped (counted in memsink.dropped)")

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued so far has been handled (tests, shutdown)."""
        if not self.running:
            return True
        done = threading.Event()
        try:
            self._q.put(done, timeout=timeout)
        except queue.Full:
            return False
        return done.wait(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        """Drain what is queued, then end the thread."""
        thread = self._thread
        if thread is None:
            return
        try:
            self._q.put(_STOP, timeout=timeout)
        except queue.Full:
            pass  # the thread is stuck; it is a daemon thread and dies with the process
        thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            self._safe(item)

    def _safe(self, ev: dict) -> None:
        try:
            self._handle(ev)
        except Exception as exc:  # noqa: BLE001 - one bad write must not end the sink
            try:
                self._log(f"memory sink: {exc!r}")
            except Exception:  # noqa: BLE001 - nowhere left to report it # nosec B110
                pass

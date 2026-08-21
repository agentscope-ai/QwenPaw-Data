"""Non-blocking rotating file logging for request-path metadata."""

from __future__ import annotations

import logging
import queue
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final


_STOP: Final = object()


class AsyncRotatingFileHandler(logging.Handler):
    """Format on the caller and perform filesystem writes on one worker thread.

    The queue is deliberately bounded.  Access logging must never apply
    backpressure to a business response; when saturated it drops metadata lines
    and exposes the count through :attr:`dropped`.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        backup_count: int,
        queue_size: int = 4096,
    ) -> None:
        super().__init__()
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.queue_size = queue_size
        self.dropped = 0
        self._queue: queue.Queue[str | object] = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self.writer_error: str | None = None

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._accepting = True
            self.writer_error = None
            self._thread = threading.Thread(
                target=self._run,
                name="qwenpaw-data-access-log",
                daemon=True,
            )
            self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            with self._lock:
                if not self._accepting:
                    return
                self._queue.put_nowait(message)
        except queue.Full:
            with self._lock:
                self.dropped += 1
        except Exception:
            self.handleError(record)

    def _discard_queued(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            else:
                self._queue.task_done()

    def _run(self) -> None:
        target: RotatingFileHandler | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = RotatingFileHandler(
                str(self.path),
                maxBytes=self.max_bytes,
                backupCount=self.backup_count,
                encoding="utf-8",
            )
            target.setFormatter(logging.Formatter("%(message)s"))
            while True:
                message = self._queue.get()
                try:
                    if message is _STOP:
                        return
                    record = logging.LogRecord(
                        name="api.access.writer",
                        level=logging.INFO,
                        pathname="",
                        lineno=0,
                        msg=str(message),
                        args=(),
                        exc_info=None,
                    )
                    target.emit(record)
                finally:
                    self._queue.task_done()
        except Exception as exc:  # noqa: BLE001 - logging must not crash the app
            self.writer_error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._accepting = False
            # If opening or rotating the target failed, release producers that
            # may be waiting to enqueue the shutdown sentinel.
            self._discard_queued()
            if target is not None:
                target.flush()
                target.close()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._accepting = False
        # Blocking here is safe because the application calls stop through a
        # worker during lifespan shutdown.  It also guarantees queued records
        # are flushed before the process exits.
        if thread.is_alive():
            self._queue.put(_STOP)
        thread.join()
        self._discard_queued()
        with self._lock:
            self._thread = None

    def close(self) -> None:
        self.stop()
        super().close()


__all__ = ["AsyncRotatingFileHandler"]

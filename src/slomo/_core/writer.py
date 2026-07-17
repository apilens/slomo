"""Buffered background writer.

The hot path is one ``SimpleQueue.put``. A daemon thread batches events and
appends them as JSONL lines; ``fsync`` happens only on exception/exit paths.
The writer must never block or break the host application.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from queue import Empty, SimpleQueue

from slomo._core.events import Event, Severity

_DROPPABLE = (Severity.DEBUG, Severity.INFO)


class _Flush:
    __slots__ = ("done", "fsync")

    def __init__(self, fsync: bool) -> None:
        self.done = threading.Event()
        self.fsync = fsync


class _Stop(_Flush):
    pass


_QueueItem = Event | _Flush


class BackgroundWriter:
    def __init__(
        self,
        sink,  # SessionWriter
        *,
        flush_interval_s: float = 0.5,
        queue_max: int = 10_000,
        batch_max: int = 500,
        make_drop_event: Callable[[int], Event] | None = None,
    ) -> None:
        self._sink = sink
        self._queue: SimpleQueue[_QueueItem] = SimpleQueue()
        self._interval = flush_interval_s
        self._queue_max = queue_max
        self._batch_max = batch_max
        self._make_drop_event = make_drop_event
        self._dropped = 0
        self._drop_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="slomo-writer", daemon=True)
        self._thread.start()

    def submit(self, event: Event) -> None:
        if self._closed:
            return
        if self._queue.qsize() > self._queue_max and event.severity in _DROPPABLE:
            with self._drop_lock:
                self._dropped += 1
            return
        self._queue.put(event)

    def flush(self, *, fsync: bool = False, timeout: float = 5.0) -> None:
        if self._closed or not self._thread.is_alive():
            return
        req = _Flush(fsync)
        self._queue.put(req)
        req.done.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        req = _Stop(fsync=True)
        self._queue.put(req)
        req.done.wait(timeout)

    # ---------- writer thread ----------

    def _run(self) -> None:
        pending_flush: _Flush | None = None
        stop = False
        while not stop:
            batch: list[Event] = []
            try:
                item = self._queue.get(timeout=self._interval)
            except Empty:
                self._write_batch(batch, fsync=False)
                continue
            while True:
                if isinstance(item, _Flush):
                    pending_flush = item
                    stop = isinstance(item, _Stop)
                    break
                batch.append(item)
                if len(batch) >= self._batch_max:
                    break
                try:
                    item = self._queue.get_nowait()
                except Empty:
                    break
            self._write_batch(batch, fsync=pending_flush.fsync if pending_flush else False)
            if pending_flush is not None:
                pending_flush.done.set()
                pending_flush = None
        try:
            self._sink.close()
        except Exception:
            pass

    def _write_batch(self, batch: list[Event], *, fsync: bool) -> None:
        with self._drop_lock:
            dropped, self._dropped = self._dropped, 0
        if dropped and self._make_drop_event is not None:
            try:
                batch.insert(0, self._make_drop_event(dropped))
            except Exception:
                pass
        if not batch and not fsync:
            return
        try:
            for event in batch:
                self._sink.write_event(event)
            self._sink.flush(fsync=fsync)
        except Exception:
            pass  # storage failure must never propagate into the app

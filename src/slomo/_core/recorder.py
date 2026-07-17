"""The process-wide Recorder. Everything funnels through ``emit()``.

Import cost matters here: this module (and everything it imports) must stay
stdlib-only so ``enable()`` meets the <5ms budget without pulling the CLI
stack into the host application.
"""

from __future__ import annotations

import atexit
import os
import threading
from pathlib import Path
from typing import Any

from slomo._core import context
from slomo._core.clock import HybridClock
from slomo._core.config import Config, load_config
from slomo._core.events import Event, EventType, Severity
from slomo._core.frames import exception_payload
from slomo._core.ids import uuid7
from slomo._core.redact import Redactor
from slomo._core.serialize import to_jsonable
from slomo._core.session import SessionMeta
from slomo._core.writer import BackgroundWriter

_EXC_ID_ATTR = "_slomo_exception_id"


class Recorder:
    _instance: Recorder | None = None
    _instance_lock = threading.Lock()

    def __init__(self, clock: HybridClock | None = None, id_factory=uuid7) -> None:
        self._clock = clock or HybridClock()
        self._id_factory = id_factory
        self._lock = threading.Lock()
        self._tl = threading.local()
        self._active = False
        self._session: SessionMeta | None = None
        self._writer: BackgroundWriter | None = None
        self._backend = None
        self._config: Config | None = None
        self._redactor: Redactor | None = None
        self._hooks: list[Any] = []
        self._event_count = 0
        self._error_count = 0
        self._count_lock = threading.Lock()
        self._fork_registered = False
        self._crashed = False

    # ---------- singleton ----------

    @classmethod
    def get(cls) -> Recorder:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def _reset_for_tests(cls) -> None:
        with cls._instance_lock:
            if cls._instance is not None and cls._instance.active:
                cls._instance.shutdown()
            cls._instance = None

    # ---------- lifecycle ----------

    @property
    def active(self) -> bool:
        return self._active

    @property
    def config(self) -> Config | None:
        return self._config

    @property
    def session(self) -> SessionMeta | None:
        return self._session

    @property
    def redactor(self) -> Redactor:
        if self._redactor is None:
            self._redactor = Redactor()
        return self._redactor

    def enable(
        self,
        *,
        root: Path | str | None = None,
        labels: dict[str, str] | None = None,
        hooks: bool = True,
    ) -> None:
        with self._lock:
            if self._active:
                return
            from slomo.storage.jsonl import JsonlBackend
            from slomo.storage.paths import ensure_root

            root_path = ensure_root(Path(root) if root else None)
            config = load_config(root_path)
            if not config.enabled:
                return
            self._config = config
            self._redactor = Redactor(
                config.redact_keys,
                config.redact_patterns,
                include_defaults=config.redact_defaults,
            )
            self._backend = JsonlBackend(root_path)
            self._session = SessionMeta.create(self._clock.now_ns(), labels)
            sink = self._backend.create_session(self._session)
            self._writer = BackgroundWriter(
                sink,
                flush_interval_s=config.flush_interval_s,
                queue_max=config.queue_max,
                make_drop_event=self._make_drop_event,
            )
            self._event_count = 0
            self._error_count = 0
            self._crashed = False
            self._active = True

        atexit.register(self._atexit)
        if not self._fork_registered and hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._after_fork_in_child)
            self._fork_registered = True

        if hooks:
            from slomo.hooks import install_all

            self._hooks = install_all(self, self._config)

        self.emit(
            EventType.SESSION_STARTED,
            {
                "argv": self._session.argv,
                "cwd": self._session.cwd,
                "python": self._session.python,
                "platform": self._session.platform,
                "labels": self._session.labels,
            },
        )

    def disable(self) -> None:
        self.shutdown(status="finished")

    def shutdown(self, status: str = "finished") -> None:
        with self._lock:
            if not self._active:
                return
            self._active = False
        if self._crashed and status == "finished":
            status = "crashed"
        atexit.unregister(self._atexit)
        for hook in reversed(self._hooks):
            try:
                hook.uninstall()
            except Exception:
                pass
        self._hooks = []

        writer, session, backend = self._writer, self._session, self._backend
        if writer is not None and session is not None:
            finished_at = self._clock.now_ns()
            writer.submit(
                Event(
                    id=self._id_factory(),
                    session_id=session.id,
                    timestamp=finished_at,
                    type=EventType.SESSION_FINISHED,
                    severity=Severity.INFO,
                    trace_id=context.current_trace_id(),
                    payload={"status": status, "event_count": self._event_count},
                )
            )
            writer.close()
            try:
                backend.finalize_session(
                    session.id,
                    {
                        "finished_at": finished_at,
                        "status": status,
                        "event_count": self._event_count + 1,
                        "error_count": self._error_count,
                    },
                )
            except Exception:
                pass
        self._writer = None
        self._session = None
        self._backend = None

    def _atexit(self) -> None:
        self.shutdown(status="finished")

    def _after_fork_in_child(self) -> None:
        """The inherited writer thread did not survive the fork; start fresh."""
        if not self._active:
            return
        parent_session = self._session.id if self._session else ""
        self._active = False
        self._writer = None  # inherited thread is gone; do not touch its file
        self._hooks = []
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()
        try:
            self.enable(
                root=self._config.root if self._config else None,
                labels={"forked_from": parent_session},
            )
        except Exception:
            pass

    # ---------- emission ----------

    def emit(
        self,
        type: EventType,
        payload: dict[str, Any],
        *,
        severity: Severity = Severity.INFO,
        span_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> None:
        if not self._active or self._writer is None or self._session is None:
            return
        if getattr(self._tl, "guard", False):
            return
        ctx_span = context.current_span_id()
        if span_id is None:
            span_id = ctx_span
        elif parent_span_id is None and ctx_span is not None and ctx_span != span_id:
            parent_span_id = ctx_span  # hook-created spans nest under the active function
        event = Event(
            id=self._id_factory(),
            session_id=self._session.id,
            timestamp=self._clock.now_ns(),
            type=type,
            severity=severity,
            trace_id=context.current_trace_id(),
            span_id=span_id,
            parent_span_id=parent_span_id,
            payload=payload,
        )
        with self._count_lock:
            self._event_count += 1
            if severity in (Severity.ERROR, Severity.CRITICAL):
                self._error_count += 1
        self._writer.submit(event)

    def prepare_payload(self, raw: Any) -> Any:
        """Serialize-then-redact; the one true path for user data."""
        cfg = self._config
        jsonable = to_jsonable(
            raw,
            max_depth=cfg.max_depth if cfg else 4,
            max_items=cfg.max_collection_items if cfg else 25,
            max_str=cfg.max_value_repr if cfg else 2048,
        )
        return self.redactor.redact(jsonable)

    def record_exception(
        self,
        exc: BaseException,
        *,
        unhandled: bool = False,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        function: str | None = None,
    ) -> None:
        if not self._active:
            return
        first_sighting = not hasattr(exc, _EXC_ID_ATTR)
        if first_sighting:
            try:
                setattr(exc, _EXC_ID_ATTR, self._id_factory())
            except Exception:
                first_sighting = True  # can't tag; accept possible duplicates
        exception_id = getattr(exc, _EXC_ID_ATTR, None) or self._id_factory()

        payload = exception_payload(exc, unhandled=unhandled)
        payload["exception_id"] = exception_id
        payload["message"] = self.redactor.redact(payload["message"])
        if function:
            payload["function"] = function

        if unhandled:
            self.emit(
                EventType.ERROR,
                payload,
                severity=Severity.CRITICAL,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )
        else:
            self.emit(
                EventType.FUNCTION_EXCEPTION,
                payload,
                severity=Severity.ERROR,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )

        if first_sighting and self._config and self._config.hooks.snapshots:
            from slomo.hooks.snapshots import capture_exception_locals

            snapshot = capture_exception_locals(exc, self)
            if snapshot:
                self.emit(
                    EventType.VARIABLE_SNAPSHOT,
                    {"exception_id": exception_id, "frames": snapshot, "source": "exception"},
                    severity=Severity.ERROR,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                )

    def flush(self, *, fsync: bool = False) -> None:
        if self._writer is not None:
            self._writer.flush(fsync=fsync)

    def mark_crashed(self) -> None:
        """Called by the unhandled-exception hook: persist state before dying."""
        self._crashed = True
        if self._writer is not None:
            self._writer.flush(fsync=True)
        if self._backend is not None and self._session is not None:
            try:
                self._backend.finalize_session(
                    self._session.id,
                    {
                        "status": "crashed",
                        "finished_at": self._clock.now_ns(),
                        "event_count": self._event_count,
                        "error_count": self._error_count,
                    },
                )
            except Exception:
                pass

    # ---------- helpers ----------

    def guard(self):
        """Thread-local reentrancy guard for hook callbacks."""
        return _Guard(self._tl)

    def _make_drop_event(self, dropped: int) -> Event:
        assert self._session is not None
        return Event(
            id=self._id_factory(),
            session_id=self._session.id,
            timestamp=self._clock.now_ns(),
            type=EventType.CUSTOM,
            severity=Severity.WARNING,
            trace_id="",
            payload={"name": "slomo.events_dropped", "dropped": dropped},
        )


class _Guard:
    __slots__ = ("_tl",)

    def __init__(self, tl: threading.local) -> None:
        self._tl = tl

    def __enter__(self):
        self._tl.guard = True
        return self

    def __exit__(self, *exc) -> None:
        self._tl.guard = False


def get_recorder() -> Recorder:
    return Recorder.get()

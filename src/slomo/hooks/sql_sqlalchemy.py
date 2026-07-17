"""SQLAlchemy instrumentation via its public event API (no monkeypatching).

While a statement executes, the reentrancy guard is held so a sqlite3-level
hook underneath does not double-record the same query.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from slomo._core.events import EventType, Severity
from slomo._core.ids import new_span_id

if TYPE_CHECKING:
    from slomo._core.config import Config
    from slomo._core.recorder import Recorder

_MAX_SQL = 4096


class SqlalchemyHook:
    name = "sql.sqlalchemy"

    def __init__(self) -> None:
        self._installed = False
        self._recorder: Recorder | None = None
        self._capture_params = False

    def available(self) -> bool:
        return "sqlalchemy" in sys.modules

    def install(self, recorder: Recorder, config: Config) -> None:
        if self._installed:
            return
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        self._recorder = recorder
        self._capture_params = config.hooks.sql_capture_params
        event.listen(Engine, "before_cursor_execute", self._before)
        event.listen(Engine, "after_cursor_execute", self._after)
        try:
            event.listen(Engine, "handle_error", self._error)
            self._error_listener = True
        except Exception:
            self._error_listener = False
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        event.remove(Engine, "before_cursor_execute", self._before)
        event.remove(Engine, "after_cursor_execute", self._after)
        if self._error_listener:
            try:
                event.remove(Engine, "handle_error", self._error)
            except Exception:
                pass
        self._installed = False

    # ---------- listeners ----------

    def _before(self, conn, cursor, statement, parameters, context, executemany) -> None:
        rec = self._recorder
        if rec is None or not rec.active or context is None:
            return
        try:
            span_id = new_span_id()
            query_id = new_span_id()
            payload = {
                "engine": "sqlalchemy",
                "dialect": getattr(conn.dialect, "name", "unknown"),
                "sql": str(statement)[:_MAX_SQL],
                "query_id": query_id,
                "executemany": bool(executemany),
            }
            if self._capture_params and parameters:
                payload["params"] = rec.prepare_payload(parameters)
            rec.emit(EventType.SQL_QUERY, payload, span_id=span_id)
            context._slomo = (
                span_id,
                query_id,
                time.perf_counter_ns(),
                rec.guard().__enter__(),
            )
        except Exception:
            pass

    def _after(self, conn, cursor, statement, parameters, context, executemany) -> None:
        rec = self._recorder
        if rec is None or context is None:
            return
        try:
            state = getattr(context, "_slomo", None)
            if state is None:
                return
            context._slomo = None
            span_id, query_id, t0, guard = state
            guard.__exit__(None, None, None)
            rowcount = getattr(cursor, "rowcount", None)
            rec.emit(
                EventType.SQL_RESULT,
                {
                    "query_id": query_id,
                    "duration_ns": time.perf_counter_ns() - t0,
                    "rowcount": rowcount if isinstance(rowcount, int) else None,
                },
                span_id=span_id,
            )
        except Exception:
            pass

    def _error(self, exception_context) -> None:
        rec = self._recorder
        if rec is None:
            return
        try:
            context = getattr(exception_context, "execution_context", None)
            state = getattr(context, "_slomo", None) if context is not None else None
            if state is None:
                return
            context._slomo = None
            span_id, query_id, t0, guard = state
            guard.__exit__(None, None, None)
            exc = exception_context.original_exception
            rec.emit(
                EventType.SQL_RESULT,
                {
                    "query_id": query_id,
                    "duration_ns": time.perf_counter_ns() - t0,
                    "error": type(exc).__qualname__ if exc else "unknown",
                    "message": rec.redactor.redact(str(exc)) if exc else "",
                },
                severity=Severity.ERROR,
                span_id=span_id,
            )
        except Exception:
            pass

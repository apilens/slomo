"""sqlite3 instrumentation via connection/cursor subclasses (keeps
isinstance checks intact). Only patches when the host app already imported
sqlite3; callers supplying their own ``factory`` are left alone."""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any

from slomo._core.events import EventType, Severity
from slomo._core.ids import new_span_id
from slomo.hooks.base import PATCH_SENTINEL

if TYPE_CHECKING:
    from slomo._core.config import Config
    from slomo._core.recorder import Recorder

_MAX_SQL = 4096


def record_query(
    recorder: Recorder,
    engine: str,
    sql: Any,
    params: Any,
    capture_params: bool,
    thunk,
):
    """Emit sql.query, run thunk under the reentrancy guard, emit sql.result."""
    span_id = new_span_id()
    query_id = new_span_id()
    payload: dict[str, Any] = {"engine": engine, "sql": str(sql)[:_MAX_SQL], "query_id": query_id}
    if capture_params and params is not None:
        payload["params"] = recorder.prepare_payload(params)
    recorder.emit(EventType.SQL_QUERY, payload, span_id=span_id)
    t0 = time.perf_counter_ns()
    try:
        with recorder.guard():
            result = thunk()
    except Exception as exc:
        recorder.emit(
            EventType.SQL_RESULT,
            {
                "query_id": query_id,
                "duration_ns": time.perf_counter_ns() - t0,
                "error": type(exc).__qualname__,
                "message": recorder.redactor.redact(str(exc)),
            },
            severity=Severity.ERROR,
            span_id=span_id,
        )
        raise
    rowcount = getattr(result, "rowcount", None)
    recorder.emit(
        EventType.SQL_RESULT,
        {
            "query_id": query_id,
            "duration_ns": time.perf_counter_ns() - t0,
            "rowcount": rowcount if isinstance(rowcount, int) else None,
        },
        span_id=span_id,
    )
    return result


class Sqlite3Hook:
    name = "sqlite3"

    def __init__(self) -> None:
        self._orig_connect = None
        self._module = None

    def available(self) -> bool:
        return "sqlite3" in sys.modules

    def install(self, recorder: Recorder, config: Config) -> None:
        import sqlite3

        if getattr(sqlite3.connect, PATCH_SENTINEL, False):
            return
        self._module = sqlite3
        self._orig_connect = sqlite3.connect
        orig_connect = sqlite3.connect
        capture_params = config.hooks.sql_capture_params

        class TrackedCursor(sqlite3.Cursor):
            def execute(self, sql, *args, **kwargs):
                return record_query(
                    recorder,
                    "sqlite3",
                    sql,
                    args[0] if args else None,
                    capture_params,
                    lambda: super(TrackedCursor, self).execute(sql, *args, **kwargs),
                )

            def executemany(self, sql, *args, **kwargs):
                return record_query(
                    recorder,
                    "sqlite3",
                    sql,
                    None,
                    False,
                    lambda: super(TrackedCursor, self).executemany(sql, *args, **kwargs),
                )

            def executescript(self, script, *args, **kwargs):
                return record_query(
                    recorder,
                    "sqlite3",
                    script,
                    None,
                    False,
                    lambda: super(TrackedCursor, self).executescript(script, *args, **kwargs),
                )

        class TrackedConnection(sqlite3.Connection):
            # Connection.execute* are C-implemented and bypass the Python
            # cursor() override, so they are wrapped here too; the guard in
            # record_query suppresses any nested double-recording.
            def cursor(self, factory=None):
                return super().cursor(factory or TrackedCursor)

            def execute(self, sql, *args, **kwargs):
                return record_query(
                    recorder,
                    "sqlite3",
                    sql,
                    args[0] if args else None,
                    capture_params,
                    lambda: super(TrackedConnection, self).execute(sql, *args, **kwargs),
                )

            def executemany(self, sql, *args, **kwargs):
                return record_query(
                    recorder,
                    "sqlite3",
                    sql,
                    None,
                    False,
                    lambda: super(TrackedConnection, self).executemany(sql, *args, **kwargs),
                )

            def executescript(self, script, *args, **kwargs):
                return record_query(
                    recorder,
                    "sqlite3",
                    script,
                    None,
                    False,
                    lambda: super(TrackedConnection, self).executescript(script, *args, **kwargs),
                )

        def tracked_connect(*args, **kwargs):
            if kwargs.get("factory") is not None:
                return orig_connect(*args, **kwargs)
            kwargs["factory"] = TrackedConnection
            return orig_connect(*args, **kwargs)

        setattr(tracked_connect, PATCH_SENTINEL, True)
        sqlite3.connect = tracked_connect
        sqlite3.dbapi2.connect = tracked_connect

    def uninstall(self) -> None:
        if self._module is not None and self._orig_connect is not None:
            self._module.connect = self._orig_connect
            self._module.dbapi2.connect = self._orig_connect
            self._module = None
            self._orig_connect = None

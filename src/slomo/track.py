"""The @track decorator: opt-in function span recording.

Handles sync functions, async functions, generators, and async generators.
When the recorder is disabled the wrapper short-circuits to the original
function with near-zero overhead.
"""

from __future__ import annotations

import functools
import inspect
import time
from typing import Any

from slomo._core.context import current_span_id, span_context
from slomo._core.events import EventType, Severity
from slomo._core.ids import new_span_id
from slomo._core.recorder import get_recorder
from slomo._core.tracked import register as _register_tracked


def track(
    func=None,
    *,
    capture_args: bool = True,
    capture_result: bool = True,
    name: str | None = None,
):
    def decorator(fn):
        _register_tracked(getattr(fn, "__code__", None))  # auto-trace defers to @track
        label = name or fn.__qualname__
        module = fn.__module__
        try:
            source_file = inspect.getsourcefile(fn) or ""
            source_line = inspect.getsourcelines(fn)[1]
        except (OSError, TypeError):
            source_file, source_line = "", 0

        def _enter(rec, args, kwargs) -> tuple[str, str | None]:
            span_id = new_span_id()
            parent = current_span_id()
            payload: dict[str, Any] = {
                "function": label,
                "module": module,
                "file": source_file,
                "line": source_line,
            }
            if capture_args and (args or kwargs):
                payload["args"] = rec.prepare_payload(list(args))
                payload["kwargs"] = rec.prepare_payload(dict(kwargs))
            rec.emit(EventType.FUNCTION_ENTER, payload, span_id=span_id, parent_span_id=parent)
            return span_id, parent

        def _exit(rec, span_id, parent, t0, result) -> None:
            payload: dict[str, Any] = {
                "function": label,
                "module": module,
                "duration_ns": time.perf_counter_ns() - t0,
            }
            if capture_result and result is not None:
                payload["result"] = rec.prepare_payload(result)
            rec.emit(EventType.FUNCTION_EXIT, payload, span_id=span_id, parent_span_id=parent)

        def _fail(rec, span_id, parent, t0, exc) -> None:
            rec.record_exception(exc, span_id=span_id, parent_span_id=parent, function=label)
            rec.emit(
                EventType.FUNCTION_EXIT,
                {
                    "function": label,
                    "module": module,
                    "duration_ns": time.perf_counter_ns() - t0,
                    "outcome": "exception",
                    "exc_type": type(exc).__qualname__,
                },
                severity=Severity.ERROR,
                span_id=span_id,
                parent_span_id=parent,
            )

        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def asyncgen_wrapper(*args, **kwargs):
                rec = get_recorder()
                if not rec.active:
                    async for item in fn(*args, **kwargs):
                        yield item
                    return
                span_id, parent = _enter(rec, args, kwargs)
                t0 = time.perf_counter_ns()
                try:
                    with span_context(span_id):
                        async for item in fn(*args, **kwargs):
                            yield item
                except BaseException as exc:
                    _fail(rec, span_id, parent, t0, exc)
                    raise
                _exit(rec, span_id, parent, t0, None)

            return asyncgen_wrapper

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                rec = get_recorder()
                if not rec.active:
                    return await fn(*args, **kwargs)
                span_id, parent = _enter(rec, args, kwargs)
                t0 = time.perf_counter_ns()
                try:
                    with span_context(span_id):
                        result = await fn(*args, **kwargs)
                except BaseException as exc:
                    _fail(rec, span_id, parent, t0, exc)
                    raise
                _exit(rec, span_id, parent, t0, result)
                return result

            return async_wrapper

        if inspect.isgeneratorfunction(fn):

            @functools.wraps(fn)
            def gen_wrapper(*args, **kwargs):
                rec = get_recorder()
                if not rec.active:
                    yield from fn(*args, **kwargs)
                    return
                span_id, parent = _enter(rec, args, kwargs)
                t0 = time.perf_counter_ns()
                try:
                    with span_context(span_id):
                        yield from fn(*args, **kwargs)
                except BaseException as exc:
                    _fail(rec, span_id, parent, t0, exc)
                    raise
                _exit(rec, span_id, parent, t0, None)

            return gen_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            rec = get_recorder()
            if not rec.active:
                return fn(*args, **kwargs)
            span_id, parent = _enter(rec, args, kwargs)
            t0 = time.perf_counter_ns()
            try:
                with span_context(span_id):
                    result = fn(*args, **kwargs)
            except BaseException as exc:
                _fail(rec, span_id, parent, t0, exc)
                raise
            _exit(rec, span_id, parent, t0, result)
            return result

        return sync_wrapper

    if func is not None:
        return decorator(func)
    return decorator

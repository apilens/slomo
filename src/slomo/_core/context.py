"""Trace/span propagation via contextvars (asyncio-safe, per-thread)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from slomo._core.ids import new_trace_id

_trace_id: ContextVar[str | None] = ContextVar("slomo_trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("slomo_span_id", default=None)


def current_trace_id() -> str:
    tid = _trace_id.get()
    if tid is None:
        tid = new_trace_id()
        _trace_id.set(tid)
    return tid


def current_span_id() -> str | None:
    return _span_id.get()


@contextmanager
def span_context(span_id: str) -> Iterator[None]:
    token = _span_id.set(span_id)
    try:
        yield
    finally:
        _span_id.reset(token)


def push_span(span_id: str | None):
    """Non-contextmanager span push for callback-driven callers (auto-trace)."""
    return _span_id.set(span_id)


def pop_span(token) -> bool:
    """Reset a push_span token; False if it belongs to another context."""
    try:
        _span_id.reset(token)
        return True
    except ValueError:
        return False

"""httpx instrumentation: patches ``Client.send`` and ``AsyncClient.send``."""

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

_MAX_BODY = 4096


def _request_payload(recorder: Recorder, request: Any, request_id: str) -> dict[str, Any]:
    body = None
    try:
        raw = request.content
        if raw:
            body = raw.decode("utf-8", errors="replace")[:_MAX_BODY]
    except Exception:
        pass
    return {
        "client": "httpx",
        "request_id": request_id,
        "method": str(request.method),
        "url": recorder.redactor.redact(str(request.url)),
        "headers": recorder.redactor.redact_headers(dict(request.headers)),
        "body": recorder.redactor.redact(body) if body else None,
    }


def _response_payload(
    recorder: Recorder, response: Any, request_id: str, t0: int
) -> dict[str, Any]:
    body = None
    try:
        if not response.is_stream_consumed and response.is_closed:
            pass
        raw = response.content  # already read for non-streaming responses
        if raw:
            body = raw.decode("utf-8", errors="replace")[:_MAX_BODY]
    except Exception:
        pass
    return {
        "client": "httpx",
        "request_id": request_id,
        "status": response.status_code,
        "reason": response.reason_phrase,
        "duration_ns": time.perf_counter_ns() - t0,
        "headers": recorder.redactor.redact_headers(dict(response.headers)),
        "body": recorder.redactor.redact(body) if body else None,
    }


def _error_payload(recorder: Recorder, exc: Exception, request_id: str, t0: int) -> dict[str, Any]:
    return {
        "client": "httpx",
        "request_id": request_id,
        "duration_ns": time.perf_counter_ns() - t0,
        "error": type(exc).__qualname__,
        "message": recorder.redactor.redact(str(exc)),
    }


class HttpxHook:
    name = "http.httpx"

    def __init__(self) -> None:
        self._orig_send = None
        self._orig_async_send = None

    def available(self) -> bool:
        return "httpx" in sys.modules

    def install(self, recorder: Recorder, config: Config) -> None:
        import httpx

        if getattr(httpx.Client.send, PATCH_SENTINEL, False):
            return
        self._orig_send = orig_send = httpx.Client.send
        self._orig_async_send = orig_async_send = httpx.AsyncClient.send

        def tracked_send(client, request, **kwargs):
            if not recorder.active:
                return orig_send(client, request, **kwargs)
            span_id = new_span_id()
            request_id = new_span_id()
            try:
                recorder.emit(
                    EventType.HTTP_REQUEST,
                    _request_payload(recorder, request, request_id),
                    span_id=span_id,
                )
            except Exception:
                pass
            t0 = time.perf_counter_ns()
            try:
                with recorder.guard():
                    response = orig_send(client, request, **kwargs)
            except Exception as exc:
                recorder.emit(
                    EventType.HTTP_RESPONSE,
                    _error_payload(recorder, exc, request_id, t0),
                    severity=Severity.ERROR,
                    span_id=span_id,
                )
                raise
            try:
                recorder.emit(
                    EventType.HTTP_RESPONSE,
                    _response_payload(recorder, response, request_id, t0),
                    severity=Severity.WARNING if response.status_code >= 400 else Severity.INFO,
                    span_id=span_id,
                )
            except Exception:
                pass
            return response

        async def tracked_async_send(client, request, **kwargs):
            if not recorder.active:
                return await orig_async_send(client, request, **kwargs)
            span_id = new_span_id()
            request_id = new_span_id()
            try:
                recorder.emit(
                    EventType.HTTP_REQUEST,
                    _request_payload(recorder, request, request_id),
                    span_id=span_id,
                )
            except Exception:
                pass
            t0 = time.perf_counter_ns()
            try:
                with recorder.guard():
                    response = await orig_async_send(client, request, **kwargs)
            except Exception as exc:
                recorder.emit(
                    EventType.HTTP_RESPONSE,
                    _error_payload(recorder, exc, request_id, t0),
                    severity=Severity.ERROR,
                    span_id=span_id,
                )
                raise
            try:
                recorder.emit(
                    EventType.HTTP_RESPONSE,
                    _response_payload(recorder, response, request_id, t0),
                    severity=Severity.WARNING if response.status_code >= 400 else Severity.INFO,
                    span_id=span_id,
                )
            except Exception:
                pass
            return response

        setattr(tracked_send, PATCH_SENTINEL, True)
        setattr(tracked_async_send, PATCH_SENTINEL, True)
        httpx.Client.send = tracked_send
        httpx.AsyncClient.send = tracked_async_send

    def uninstall(self) -> None:
        if self._orig_send is not None:
            import httpx

            httpx.Client.send = self._orig_send
            httpx.AsyncClient.send = self._orig_async_send
            self._orig_send = None
            self._orig_async_send = None

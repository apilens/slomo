"""requests instrumentation: patches ``Session.send`` — the single stable
choke point every requests call goes through."""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from slomo._core.events import EventType, Severity
from slomo._core.ids import new_span_id
from slomo.hooks.base import PATCH_SENTINEL

if TYPE_CHECKING:
    from slomo._core.config import Config
    from slomo._core.recorder import Recorder

_MAX_BODY = 4096
_TEXTUAL = ("json", "text", "xml", "html", "urlencoded")


def _body_preview(content: bytes | str | None, content_type: str) -> str | None:
    if not content or not any(t in content_type for t in _TEXTUAL):
        return None
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8", errors="replace")
        except Exception:
            return None
    return content[:_MAX_BODY]


class RequestsHook:
    name = "http.requests"

    def __init__(self) -> None:
        self._orig_send = None

    def available(self) -> bool:
        return "requests" in sys.modules

    def install(self, recorder: Recorder, config: Config) -> None:
        import requests

        if getattr(requests.sessions.Session.send, PATCH_SENTINEL, False):
            return
        self._orig_send = orig_send = requests.sessions.Session.send

        def tracked_send(session, request, **kwargs):
            if not recorder.active:
                return orig_send(session, request, **kwargs)
            span_id = new_span_id()
            request_id = new_span_id()
            try:
                headers = recorder.redactor.redact_headers(request.headers or {})
                body = _body_preview(request.body, str(request.headers.get("Content-Type", "")))
                recorder.emit(
                    EventType.HTTP_REQUEST,
                    {
                        "client": "requests",
                        "request_id": request_id,
                        "method": request.method,
                        "url": recorder.redactor.redact(request.url or ""),
                        "headers": headers,
                        "body": recorder.redactor.redact(body) if body else None,
                        "body_size": len(request.body) if request.body else 0,
                    },
                    span_id=span_id,
                )
            except Exception:
                pass
            t0 = time.perf_counter_ns()
            try:
                with recorder.guard():
                    response = orig_send(session, request, **kwargs)
            except Exception as exc:
                recorder.emit(
                    EventType.HTTP_RESPONSE,
                    {
                        "client": "requests",
                        "request_id": request_id,
                        "duration_ns": time.perf_counter_ns() - t0,
                        "error": type(exc).__qualname__,
                        "message": recorder.redactor.redact(str(exc)),
                    },
                    severity=Severity.ERROR,
                    span_id=span_id,
                )
                raise
            try:
                ct = str(response.headers.get("Content-Type", ""))
                recorder.emit(
                    EventType.HTTP_RESPONSE,
                    {
                        "client": "requests",
                        "request_id": request_id,
                        "status": response.status_code,
                        "reason": response.reason,
                        "duration_ns": time.perf_counter_ns() - t0,
                        "headers": recorder.redactor.redact_headers(response.headers or {}),
                        "body": recorder.redactor.redact(_body_preview(response.content, ct) or "")
                        or None,
                        "body_size": len(response.content or b""),
                    },
                    severity=Severity.WARNING if response.status_code >= 400 else Severity.INFO,
                    span_id=span_id,
                )
            except Exception:
                pass
            return response

        setattr(tracked_send, PATCH_SENTINEL, True)
        requests.sessions.Session.send = tracked_send

    def uninstall(self) -> None:
        if self._orig_send is not None:
            import requests

            requests.sessions.Session.send = self._orig_send
            self._orig_send = None

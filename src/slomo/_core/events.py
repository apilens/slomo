"""The event model. Every recorded activity is one Event."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any


class EventType(enum.StrEnum):
    SESSION_STARTED = "session.started"
    SESSION_FINISHED = "session.finished"
    FUNCTION_ENTER = "function.enter"
    FUNCTION_EXIT = "function.exit"
    FUNCTION_EXCEPTION = "function.exception"
    HTTP_REQUEST = "http.request"
    HTTP_RESPONSE = "http.response"
    SQL_QUERY = "sql.query"
    SQL_RESULT = "sql.result"
    VARIABLE_SNAPSHOT = "variable.snapshot"
    LOG = "log"
    WARNING = "warning"
    ERROR = "error"
    CUSTOM = "custom"


class Severity(enum.StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


_SEVERITY_ORDER = {s: i for i, s in enumerate(Severity)}


def severity_at_least(sev: Severity, floor: Severity) -> bool:
    return _SEVERITY_ORDER[sev] >= _SEVERITY_ORDER[floor]


@dataclass(slots=True, frozen=True)
class Event:
    id: str
    session_id: str
    timestamp: int  # epoch nanoseconds
    type: EventType
    severity: Severity
    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json_line(self) -> str:
        return (
            json.dumps(
                {
                    "id": self.id,
                    "session_id": self.session_id,
                    "timestamp": self.timestamp,
                    "type": str(self.type),
                    "severity": str(self.severity),
                    "trace_id": self.trace_id,
                    "span_id": self.span_id,
                    "parent_span_id": self.parent_span_id,
                    "payload": self.payload,
                },
                separators=(",", ":"),
                default=str,
            )
            + "\n"
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        try:
            etype = EventType(d.get("type", "custom"))
        except ValueError:
            etype = EventType.CUSTOM
        try:
            sev = Severity(d.get("severity", "info"))
        except ValueError:
            sev = Severity.INFO
        return cls(
            id=d.get("id", ""),
            session_id=d.get("session_id", ""),
            timestamp=int(d.get("timestamp", 0)),
            type=etype,
            severity=sev,
            trace_id=d.get("trace_id", ""),
            span_id=d.get("span_id"),
            parent_span_id=d.get("parent_span_id"),
            payload=d.get("payload") or {},
        )

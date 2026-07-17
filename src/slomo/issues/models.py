"""Issue and Incident models. A crash is an incident; similar incidents
group into one Issue."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

IssueStatus = Literal["open", "resolved"]
Stability = Literal["one-time", "intermittent", "recurring", "resolved"]


@dataclass(slots=True)
class Incident:
    event_id: str
    issue_id: str
    session_id: str
    timestamp: int
    exc_type: str
    message: str
    frames: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: str = ""
    unhandled: bool = False
    exception_id: str = ""
    trace_id: str = ""
    span_id: str | None = None


@dataclass(slots=True)
class Issue:
    id: str  # "SM-<fingerprint[:8]>"
    fingerprint: str
    title: str
    category: str
    severity: str
    status: IssueStatus = "open"
    stability: Stability = "one-time"
    occurrences: int = 0
    first_seen: int = 0
    last_seen: int = 0
    affected_sessions: int = 0
    confidence: float = 0.0
    exc_type: str = ""
    top_frame: dict[str, Any] = field(default_factory=dict)
    resolved_at: int | None = None


def issue_id_for(fp: str) -> str:
    return f"SM-{fp[:8]}"

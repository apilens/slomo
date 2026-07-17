"""`slomo doctor` — heuristic root-cause diagnosis for an issue.

Honest about being heuristic: every field is derived from recorded data,
and the confidence score is surfaced rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from slomo._core.events import Event, EventType
from slomo.issues.fingerprint import top_project_frame
from slomo.issues.index import IssueIndex
from slomo.issues.models import Incident, Issue
from slomo.issues.similarity import similar_issues
from slomo.query.reader import EventReader

_CONTEXT_WINDOW_NS = 2_000_000_000  # correlated events within 2s before the crash

_FIX_TEMPLATES = {
    "Null Reference": "Guard against None before the failing access at {where} — "
    "trace where the value is produced and handle the missing case.",
    "Network": "Wrap the call at {where} with retry/backoff and a connection-failure fallback.",
    "Database": "Check the query and connection handling at {where}; verify schema and constraints.",
    "Timeout": "Increase or tune the timeout at {where}, or make the operation async/retryable.",
    "Filesystem": "Verify the path exists and permissions are correct before the operation at {where}.",
    "Validation": "Validate inputs earlier — reject bad data before it reaches {where}.",
    "Configuration": "A config/env key is missing — provide a default or fail fast at startup.",
    "Dependency": "A required module/version is missing — pin and install the dependency.",
    "Memory": "Reduce working-set size or stream the data processed at {where}.",
    "Authentication": "Check credential freshness/refresh logic before the call at {where}.",
    "Authorization": "Verify the principal's permissions for the operation at {where}.",
    "Programming Error": "Review the logic at {where}; add a test reproducing this input.",
}


@dataclass
class Diagnosis:
    issue: Issue
    likely_root_cause: str = ""
    first_bad_function: str = ""
    first_bad_variable: str = ""
    suggested_fix: str = ""
    correlated_events: list[Event] = field(default_factory=list)
    related_issues: list[tuple[Issue, float]] = field(default_factory=list)
    unhandled_count: int = 0
    sample_incident: Incident | None = None
    variables: dict[str, Any] = field(default_factory=dict)


def diagnose(issue: Issue, index: IssueIndex, reader: EventReader) -> Diagnosis:
    d = Diagnosis(issue=issue)
    incidents = index.incidents_for_issue(issue.id, limit=20)
    if not incidents:
        return d
    latest = incidents[0]
    d.sample_incident = latest
    d.unhandled_count = sum(1 for i in incidents if i.unhandled)

    top = top_project_frame(latest.frames)
    where = (
        f"{_short(top.get('file', '?'))}:{top.get('line', '?')} in {top.get('function', '?')}()"
        if top
        else "?"
    )
    d.likely_root_cause = _root_cause_sentence(issue, latest, top)
    fix = _FIX_TEMPLATES.get(issue.category, "Inspect the recorded replay: slomo replay " + issue.id)
    d.suggested_fix = fix.format(where=where)

    # Walk the incident's session for context: earliest tracked span in the
    # failing trace, correlated http/sql just before the crash, and the
    # variable snapshot linked to the exception.
    try:
        events = [
            e
            for e in reader.backend.iter_events(latest.session_id)
            if e.trace_id == latest.trace_id
        ]
    except Exception:
        events = []
    crash_ts = latest.timestamp
    for e in events:
        if e.type == EventType.FUNCTION_ENTER and not d.first_bad_function:
            d.first_bad_function = str(e.payload.get("function", ""))
        if (
            e.type
            in (
                EventType.HTTP_REQUEST,
                EventType.HTTP_RESPONSE,
                EventType.SQL_QUERY,
                EventType.SQL_RESULT,
            )
            and 0 <= crash_ts - e.timestamp <= _CONTEXT_WINDOW_NS
        ):
            d.correlated_events.append(e)
        if (
            e.type == EventType.VARIABLE_SNAPSHOT
            and e.payload.get("exception_id") == latest.exception_id
        ):
            frames = e.payload.get("frames") or []
            if frames and isinstance(frames[-1], dict):
                d.variables = frames[-1].get("locals") or {}

    if d.variables:
        none_vars = [k for k, v in d.variables.items() if v is None]
        if none_vars and issue.category == "Null Reference":
            d.first_bad_variable = none_vars[0]
            d.likely_root_cause += f" Variable '{none_vars[0]}' was None at the crash site."
        elif d.variables:
            d.first_bad_variable = next(iter(d.variables), "")

    d.related_issues = similar_issues(issue, index)
    d.correlated_events = d.correlated_events[-6:]
    return d


def _root_cause_sentence(issue: Issue, incident: Incident, top: dict[str, Any] | None) -> str:
    where = (
        f"{top.get('function', '?')}() at {_short(top.get('file', '?'))}:{top.get('line', '?')}"
        if top
        else "an unknown location"
    )
    return f"{incident.exc_type} raised in {where}: {incident.message[:140]}"


def _short(filename: str) -> str:
    parts = str(filename).replace("\\", "/").split("/")
    return "/".join(parts[-2:])

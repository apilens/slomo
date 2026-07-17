"""IssueEngine: incrementally scans session timelines, turns exception
events into incidents, and groups incidents into issues by fingerprint.

Runs lazily in the CLI process — never in the recorder.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from slomo._core.events import Event, EventType
from slomo.issues.classify import classify
from slomo.issues.fingerprint import fingerprint, top_project_frame
from slomo.issues.index import IssueIndex
from slomo.issues.models import Incident, Issue, issue_id_for
from slomo.storage.jsonl import JsonlBackend

_FTS_TYPES = {
    EventType.FUNCTION_EXCEPTION,
    EventType.ERROR,
    EventType.WARNING,
    EventType.LOG,
    EventType.HTTP_REQUEST,
    EventType.HTTP_RESPONSE,
    EventType.SQL_QUERY,
    EventType.CUSTOM,
    EventType.VARIABLE_SNAPSHOT,
}


@dataclass
class RefreshStats:
    sessions_scanned: int = 0
    events_scanned: int = 0
    incidents_added: int = 0
    issues_touched: int = 0


class IssueEngine:
    def __init__(self, backend: JsonlBackend, index: IssueIndex) -> None:
        self.backend = backend
        self.index = index

    # ---------- refresh ----------

    def refresh(self) -> RefreshStats:
        stats = RefreshStats()
        touched: set[str] = set()
        now = time.time_ns()
        for meta in self.backend.list_sessions():
            offset = self.index.session_offset(meta.id)
            size = self.backend.timeline_size(meta.id)
            if size <= offset:
                continue
            stats.sessions_scanned += 1
            last_offset = offset
            for event, end_offset in self.backend.iter_events_with_offset(
                meta.id, from_offset=offset
            ):
                stats.events_scanned += 1
                fp = self._process_event(event, stats)
                if fp:
                    touched.add(fp)
                last_offset = end_offset
            self.index.set_session_offset(meta.id, last_offset, now)
        for fp in touched:
            self._recompute_issue(fp)
        stats.issues_touched = len(touched)
        self.index.commit()
        return stats

    def rebuild(self) -> RefreshStats:
        self.index.rebuild()
        return self.refresh()

    def _process_event(self, event: Event, stats: RefreshStats) -> str | None:
        payload = event.payload
        fp: str | None = None
        if event.type in (EventType.FUNCTION_EXCEPTION, EventType.ERROR) and payload.get(
            "exc_type"
        ):
            fp = self._ingest_incident(event, stats)
        if event.type in _FTS_TYPES:
            text = _fts_text(event)
            if text:
                try:
                    self.index.add_fts(
                        event.id, event.session_id, str(event.type), event.timestamp, text
                    )
                except Exception:
                    pass
        return fp

    def _ingest_incident(self, event: Event, stats: RefreshStats) -> str | None:
        payload = event.payload
        exc_type = str(payload.get("exc_type", ""))
        message = str(payload.get("message", ""))
        frames = payload.get("frames") or []
        exception_id = str(payload.get("exception_id", ""))
        unhandled = bool(payload.get("unhandled", event.type == EventType.ERROR))

        fp = fingerprint(exc_type, frames, message)

        if exception_id and self.index.has_exception_id(event.session_id, exception_id):
            # Same exception seen deeper in the stack already; just upgrade
            # the incident if this sighting is the unhandled crash.
            if unhandled:
                self.index.mark_unhandled(event.session_id, exception_id)
            return fp

        added = self.index.add_incident(
            Incident(
                event_id=event.id,
                issue_id=issue_id_for(fp),
                session_id=event.session_id,
                timestamp=event.timestamp,
                exc_type=exc_type,
                message=message,
                frames=frames,
                fingerprint=fp,
                unhandled=unhandled,
                exception_id=exception_id,
                trace_id=event.trace_id,
                span_id=event.span_id,
            )
        )
        if added:
            stats.incidents_added += 1
        return fp

    def _recompute_issue(self, fp: str) -> None:
        issue_id = issue_id_for(fp)
        stats = self.index.issue_stats(issue_id)
        occurrences = int(stats.get("occurrences") or 0)
        if occurrences == 0:
            return
        incidents = self.index.incidents_for_issue(issue_id, limit=1)
        latest = incidents[0] if incidents else None
        existing = self.index.get_issue_by_fingerprint(fp)

        exc_type = latest.exc_type if latest else ""
        message = latest.message if latest else ""
        frames = latest.frames if latest else []
        category, severity, confidence = classify(exc_type, "", message, frames)

        status = existing.status if existing else "open"
        resolved_at = existing.resolved_at if existing else None
        if existing and existing.status == "resolved" and resolved_at:
            if int(stats.get("last_seen") or 0) > resolved_at:
                status = "open"  # auto-reopen on regression
                resolved_at = None

        if status == "resolved":
            stability = "resolved"
        elif occurrences == 1:
            stability = "one-time"
        elif (
            int(stats.get("affected_sessions") or 0) >= 3
            and self.index.distinct_days(issue_id) >= 2
        ):
            stability = "recurring"
        else:
            stability = "intermittent"

        title = f"{exc_type}: {message}"[:160] if exc_type else "Unknown error"
        top = top_project_frame(frames) or {}

        self.index.upsert_issue(
            Issue(
                id=issue_id,
                fingerprint=fp,
                title=title,
                category=str(category),
                severity=str(severity),
                status=status,
                stability=stability,
                occurrences=occurrences,
                first_seen=int(stats.get("first_seen") or 0),
                last_seen=int(stats.get("last_seen") or 0),
                affected_sessions=int(stats.get("affected_sessions") or 0),
                confidence=confidence,
                exc_type=exc_type,
                top_frame=top,
                resolved_at=resolved_at,
            )
        )

    # ---------- queries ----------

    def get_issue(self, ref: str) -> Issue | None:
        ref = ref.strip()
        candidates = [ref]
        if not ref.upper().startswith("SM-"):
            candidates.append(f"SM-{ref}")
        for c in candidates:
            issue = self.index.resolve_issue_ref(c)
            if issue:
                return issue
        return None

    def resolve(self, ref: str) -> Issue | None:
        issue = self.get_issue(ref)
        if issue:
            self.index.set_issue_status(issue.id, "resolved", time.time_ns())
            return self.index.get_issue(issue.id)
        return None

    def reopen(self, ref: str) -> Issue | None:
        issue = self.get_issue(ref)
        if issue:
            self.index.set_issue_status(issue.id, "open", None)
            self._recompute_issue(issue.fingerprint)
            self.index.commit()
            return self.index.get_issue(issue.id)
        return None


def _fts_text(event: Event) -> str:
    p = event.payload
    parts: list[str] = []
    for key in ("message", "exc_type", "function", "module", "logger", "name", "label"):
        v = p.get(key)
        if v:
            parts.append(str(v))
    if event.type in (EventType.SQL_QUERY,):
        parts.append(str(p.get("sql", "")))
    if event.type in (EventType.HTTP_REQUEST, EventType.HTTP_RESPONSE):
        parts.extend(str(p.get(k, "")) for k in ("method", "url", "status") if p.get(k))
    for f in (p.get("frames") or [])[:10]:
        if isinstance(f, dict):
            parts.append(f"{f.get('file', '')} {f.get('function', '')}")
    return " ".join(parts)[:4000]

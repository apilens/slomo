"""Search: free text (FTS5) plus field filters like ``module=checkout``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from slomo._core.events import Event
from slomo.issues.index import IssueIndex
from slomo.query.reader import EventReader

_FIELD_ALIASES = {
    "module": "module",
    "function": "function",
    "fn": "function",
    "logger": "logger",
    "user": "user",
    "host": "host",
    "type": "type",
    "session": "session",
    "url": "url",
    "method": "method",
    "status": "status",
    "sku": "sku",
}


@dataclass
class Query:
    text: str = ""
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class SearchHit:
    event_id: str
    session_id: str
    type: str
    timestamp: int
    snippet: str


def parse_query(terms: list[str]) -> Query:
    q = Query()
    free: list[str] = []
    for term in terms:
        if "=" in term:
            key, _, value = term.partition("=")
            key = key.strip().lower()
            if value:
                q.fields[_FIELD_ALIASES.get(key, key)] = value.strip()
                continue
        free.append(term)
    q.text = " ".join(free).strip()
    return q


def _event_matches_fields(event: Event, fields: dict[str, str]) -> bool:
    for key, wanted in fields.items():
        if key == "type":
            if wanted.lower() not in str(event.type).lower():
                return False
            continue
        if key == "session":
            if not event.session_id.startswith(wanted):
                return False
            continue
        actual = _find_value(event.payload, key)
        if actual is None or wanted.lower() not in str(actual).lower():
            return False
    return True


def _find_value(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    for v in payload.values():
        if isinstance(v, dict):
            found = _find_value(v, key)
            if found is not None:
                return found
    return None


def _event_matches_text(event: Event, text: str) -> bool:
    haystack = str(event.payload).lower()
    return all(term.lower() in haystack for term in text.split())


def search(
    query: Query,
    index: IssueIndex,
    reader: EventReader,
    *,
    limit: int = 50,
) -> list[SearchHit]:
    if query.text and not query.fields:
        return [
            SearchHit(
                event_id=r["event_id"],
                session_id=r["session_id"],
                type=r["type"],
                timestamp=int(r["timestamp"]),
                snippet=r["snippet"],
            )
            for r in index.search_fts(query.text, limit=limit)
        ]

    # Field filters (optionally with text): stream the JSONL timelines.
    hits: list[SearchHit] = []
    session_filter = query.fields.get("session")
    for meta in reversed(reader.backend.list_sessions()):
        if session_filter and not meta.id.startswith(session_filter):
            continue
        for event in reader.backend.iter_events(meta.id):
            if not _event_matches_fields(event, query.fields):
                continue
            if query.text and not _event_matches_text(event, query.text):
                continue
            preview = {k: v for k, v in event.payload.items() if not isinstance(v, (dict, list))}
            hits.append(
                SearchHit(
                    event_id=event.id,
                    session_id=event.session_id,
                    type=str(event.type),
                    timestamp=event.timestamp,
                    snippet=str(preview)[:160],
                )
            )
            if len(hits) >= limit:
                return hits
    return hits

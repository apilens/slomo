"""Read-side access to recorded events: filtered streams and span trees."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from slomo._core.events import Event, EventType, Severity, severity_at_least
from slomo.storage.jsonl import JsonlBackend


@dataclass
class SpanNode:
    span_id: str | None
    events: list[Event] = field(default_factory=list)
    children: list[SpanNode] = field(default_factory=list)

    @property
    def label(self) -> str:
        for e in self.events:
            fn = e.payload.get("function")
            if fn:
                return str(fn)
        return self.span_id or "root"


class EventReader:
    def __init__(self, backend: JsonlBackend) -> None:
        self.backend = backend

    def events(
        self,
        session_id: str,
        *,
        types: set[EventType] | None = None,
        severity_min: Severity | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> Iterator[Event]:
        for event in self.backend.iter_events(session_id):
            if types is not None and event.type not in types:
                continue
            if severity_min is not None and not severity_at_least(event.severity, severity_min):
                continue
            if trace_id is not None and event.trace_id != trace_id:
                continue
            if span_id is not None and event.span_id != span_id:
                continue
            if since is not None and event.timestamp < since:
                continue
            if until is not None and event.timestamp > until:
                continue
            yield event

    def all_events(self, session_id: str) -> list[Event]:
        return list(self.backend.iter_events(session_id))

    def spans_tree(self, session_id: str) -> SpanNode:
        """Group function.* events into a parent/child span tree."""
        root = SpanNode(span_id=None)
        nodes: dict[str, SpanNode] = {}
        order: list[SpanNode] = []
        parents: dict[str, str | None] = {}
        for event in self.backend.iter_events(session_id):
            if event.span_id is None:
                root.events.append(event)
                continue
            node = nodes.get(event.span_id)
            if node is None:
                node = SpanNode(span_id=event.span_id)
                nodes[event.span_id] = node
                order.append(node)
                parents[event.span_id] = event.parent_span_id
            node.events.append(event)
            if event.parent_span_id is not None:
                parents[event.span_id] = event.parent_span_id
        for node in order:
            parent_id = parents.get(node.span_id or "")
            parent = nodes.get(parent_id) if parent_id else None
            (parent or root).children.append(node)
        return root

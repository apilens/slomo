"""Pure replay navigation logic — no I/O, fully unit-testable."""

from __future__ import annotations

from typing import Any

from slomo._core.events import Event, EventType

_ERROR_TYPES = (EventType.FUNCTION_EXCEPTION, EventType.ERROR)


class ReplayState:
    def __init__(self, events: list[Event], cursor: int = 0) -> None:
        if not events:
            raise ValueError("nothing to replay: no events")
        self.events = events
        self.cursor = max(0, min(cursor, len(events) - 1))

    def __len__(self) -> int:
        return len(self.events)

    def current(self) -> Event:
        return self.events[self.cursor]

    def next(self, n: int = 1) -> Event:
        self.cursor = min(self.cursor + n, len(self.events) - 1)
        return self.current()

    def prev(self, n: int = 1) -> Event:
        self.cursor = max(self.cursor - n, 0)
        return self.current()

    def jump(self, index: int) -> Event:
        self.cursor = max(0, min(index, len(self.events) - 1))
        return self.current()

    def search(self, text: str, direction: int = 1) -> int | None:
        """Find the next event containing text (in type or payload); move to it."""
        needle = text.lower()
        indices = (
            range(self.cursor + 1, len(self.events))
            if direction > 0
            else range(self.cursor - 1, -1, -1)
        )
        for i in indices:
            e = self.events[i]
            if needle in str(e.type).lower() or needle in str(e.payload).lower():
                self.cursor = i
                return i
        return None

    def next_error(self) -> int | None:
        for i in range(self.cursor + 1, len(self.events)):
            if self.events[i].type in _ERROR_TYPES:
                self.cursor = i
                return i
        return None

    def first_error_index(self) -> int | None:
        for i, e in enumerate(self.events):
            if e.type in _ERROR_TYPES:
                return i
        return None

    def context_window(self, before: int = 3, after: int = 3) -> list[tuple[int, Event]]:
        lo = max(0, self.cursor - before)
        hi = min(len(self.events), self.cursor + after + 1)
        return [(i, self.events[i]) for i in range(lo, hi)]

    def inspect(self) -> dict[str, Any]:
        e = self.current()
        return {
            "id": e.id,
            "type": str(e.type),
            "severity": str(e.severity),
            "timestamp": e.timestamp,
            "trace_id": e.trace_id,
            "span_id": e.span_id,
            "parent_span_id": e.parent_span_id,
            "payload": e.payload,
        }

"""ReplayState navigation, ReplayTUI command loop, and search."""

from __future__ import annotations

import pytest
from conftest import make_event
from rich.console import Console

from slomo._core.events import EventType, Severity
from slomo.replay.player import ReplayState


def _timeline():
    return [
        make_event(type=EventType.SESSION_STARTED),
        make_event(type=EventType.FUNCTION_ENTER, payload={"function": "checkout"}),
        make_event(type=EventType.SQL_QUERY, payload={"sql": "SELECT * FROM inventory"}),
        make_event(
            type=EventType.FUNCTION_EXCEPTION,
            severity=Severity.ERROR,
            payload={"exc_type": "KeyError", "message": "boom", "exception_id": "e1"},
        ),
        make_event(
            type=EventType.VARIABLE_SNAPSHOT,
            payload={"exception_id": "e1", "frames": [{"locals": {"x": None}}]},
        ),
        make_event(type=EventType.SESSION_FINISHED),
    ]


class TestReplayState:
    def test_navigation_clamps(self):
        s = ReplayState(_timeline())
        assert s.cursor == 0
        s.prev()
        assert s.cursor == 0
        s.next(100)
        assert s.cursor == len(s) - 1
        s.jump(2)
        assert s.current().type == EventType.SQL_QUERY

    def test_search_both_directions(self):
        s = ReplayState(_timeline())
        assert s.search("inventory") == 2
        assert s.search("checkout", direction=-1) == 1
        assert s.search("nope-not-there") is None

    def test_next_error_and_first_error(self):
        s = ReplayState(_timeline())
        assert s.first_error_index() == 3
        assert s.next_error() == 3
        assert s.next_error() is None

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            ReplayState([])

    def test_context_window(self):
        s = ReplayState(_timeline())
        s.jump(3)
        window = s.context_window(before=1, after=1)
        assert [i for i, _ in window] == [2, 3, 4]


class TestReplayTUI:
    def test_scripted_session(self):
        from slomo.replay.tui import ReplayTUI

        commands = iter(["n", "t", "i", "v", "w", "/finished", "q"])
        console = Console(record=True, force_terminal=False, width=100)
        tui = ReplayTUI(
            ReplayState(_timeline()), console=console, input_fn=lambda _: next(commands)
        )
        tui.run()
        out = console.export_text()
        assert "KeyError" in out  # `t` landed on the error, `i` dumped it
        assert '"x": null' in out or "'x': None" in out  # `v` found the linked snapshot


class TestSearch:
    def test_parse_query(self):
        from slomo.query.search import parse_query

        q = parse_query(["timeout", "module=checkout", "user=42"])
        assert q.text == "timeout"
        assert q.fields == {"module": "checkout", "user": "42"}

    def test_field_search_streams_jsonl(self, storage_root, backend):
        from slomo._core.recorder import Recorder
        from slomo.issues.index import IssueIndex
        from slomo.query.reader import EventReader
        from slomo.query.search import parse_query, search
        from slomo.storage import paths as sp

        Recorder._reset_for_tests()
        rec = Recorder.get()
        rec.enable(root=storage_root, hooks=False)
        rec.emit(EventType.CUSTOM, {"name": "order.placed", "user": "42"})
        rec.emit(EventType.CUSTOM, {"name": "order.placed", "user": "77"})
        rec.shutdown()
        Recorder._reset_for_tests()

        index = IssueIndex(sp.issues_dir(storage_root) / "index.sqlite")
        hits = search(parse_query(["user=42"]), index, EventReader(backend))
        assert len(hits) == 1
        assert "42" in hits[0].snippet

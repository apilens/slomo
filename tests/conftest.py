from __future__ import annotations

import sys
import threading

import pytest

from slomo._core.recorder import Recorder
from slomo.storage.jsonl import JsonlBackend
from slomo.storage.paths import initialize_root


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    """Isolated .slomo root; both recorder and CLI resolve it via env."""
    root = tmp_path / ".slomo"
    initialize_root(root)
    monkeypatch.setenv("SLOMO_HOME", str(root))
    return root


@pytest.fixture
def backend(storage_root):
    return JsonlBackend(storage_root)


@pytest.fixture
def recorder(storage_root):
    """A live recorder that is fully torn down (hooks restored) after the test."""
    Recorder._reset_for_tests()
    rec = Recorder.get()
    prev_excepthook = sys.excepthook
    prev_threading_hook = threading.excepthook
    rec.enable(root=storage_root)
    yield rec
    Recorder._reset_for_tests()
    assert sys.excepthook is prev_excepthook, "sys.excepthook not restored"
    assert threading.excepthook is prev_threading_hook, "threading.excepthook not restored"


@pytest.fixture(autouse=True)
def _always_reset_recorder():
    yield
    Recorder._reset_for_tests()


def make_event(**overrides):
    from slomo._core.events import Event, EventType, Severity
    from slomo._core.ids import uuid7

    defaults = dict(
        id=uuid7(),
        session_id="test-session",
        timestamp=1_000_000_000_000_000_000,
        type=EventType.CUSTOM,
        severity=Severity.INFO,
        trace_id="trace-1",
        span_id=None,
        parent_span_id=None,
        payload={},
    )
    defaults.update(overrides)
    return Event(**defaults)

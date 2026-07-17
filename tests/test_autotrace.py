"""Auto-trace: zero-decorator capture of project code via sys.monitoring."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys

import pytest

from slomo._core.events import EventType

PROJECT_MODULE = """
from slomo import track


def add(a, b):
    return a + b


def outer(x):
    return add(x, 1) + add(x, 2)


def boom(password):
    return password[99]


def countdown(n):
    while n:
        yield n
        n -= 1


@track
def doubled(x):
    return x * 2


async def fetch(key):
    await asyncio.sleep(0)
    raise ConnectionError(f"no backend for {key}")


import asyncio
"""


def _events(backend):
    sessions = backend.list_sessions()
    assert sessions, "no session recorded"
    return list(backend.iter_events(sessions[-1].id))


def _spans(events, function, type_):
    return [e for e in events if e.type == type_ and e.payload.get("function") == function]


@pytest.fixture
def project_mod(storage_root, recorder):
    """A module that lives under the project root (the dir holding .slomo)."""
    path = storage_root.parent / "shopmod.py"
    path.write_text(PROJECT_MODULE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("shopmod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("shopmod", None)


class TestAutoTrace:
    def test_plain_function_recorded_with_args_and_result(self, project_mod, recorder, backend):
        assert project_mod.add(2, 3) == 5
        recorder.flush(fsync=True)
        events = _events(backend)
        enters = _spans(events, "add", EventType.FUNCTION_ENTER)
        assert len(enters) == 1
        assert enters[0].payload["source"] == "auto"
        assert enters[0].payload["kwargs"] == {"a": 2, "b": 3}
        exits = _spans(events, "add", EventType.FUNCTION_EXIT)
        assert len(exits) == 1
        assert exits[0].payload["result"] == 5
        assert exits[0].span_id == enters[0].span_id

    def test_nested_calls_link_parent_spans(self, project_mod, recorder, backend):
        project_mod.outer(10)
        recorder.flush(fsync=True)
        events = _events(backend)
        outer_enter = _spans(events, "outer", EventType.FUNCTION_ENTER)[0]
        add_enters = _spans(events, "add", EventType.FUNCTION_ENTER)
        assert len(add_enters) == 2
        assert all(e.parent_span_id == outer_enter.span_id for e in add_enters)

    def test_exception_recorded_and_args_redacted(self, project_mod, recorder, backend):
        with pytest.raises(IndexError):
            project_mod.boom("hunter2-secret")
        recorder.flush(fsync=True)
        events = _events(backend)
        excs = [
            e
            for e in events
            if e.type == EventType.FUNCTION_EXCEPTION and e.payload.get("function") == "boom"
        ]
        assert excs, "escaping exception not recorded"
        exits = _spans(events, "boom", EventType.FUNCTION_EXIT)
        assert exits[0].payload["outcome"] == "exception"
        assert exits[0].payload["exc_type"] == "IndexError"
        enter = _spans(events, "boom", EventType.FUNCTION_ENTER)[0]
        assert enter.payload["kwargs"]["password"] == "[REDACTED]"

    def test_tracked_function_not_recorded_twice(self, project_mod, recorder, backend):
        assert project_mod.doubled(4) == 8
        recorder.flush(fsync=True)
        events = _events(backend)
        enters = _spans(events, "doubled", EventType.FUNCTION_ENTER)
        assert len(enters) == 1
        assert "source" not in enters[0].payload  # recorded by @track, not auto

    def test_generator_records_one_span(self, project_mod, recorder, backend):
        assert list(project_mod.countdown(3)) == [3, 2, 1]
        recorder.flush(fsync=True)
        events = _events(backend)
        assert len(_spans(events, "countdown", EventType.FUNCTION_ENTER)) == 1
        assert len(_spans(events, "countdown", EventType.FUNCTION_EXIT)) == 1

    def test_abandoned_generator_closes_span_without_error(self, project_mod, recorder, backend):
        gen = project_mod.countdown(5)
        next(gen)
        del gen
        import gc

        gc.collect()
        recorder.flush(fsync=True)
        events = _events(backend)
        exits = _spans(events, "countdown", EventType.FUNCTION_EXIT)
        assert len(exits) == 1
        assert "outcome" not in exits[0].payload  # GeneratorExit is a normal close

    def test_async_exception_recorded(self, project_mod, recorder, backend):
        with pytest.raises(ConnectionError):
            asyncio.run(project_mod.fetch("users"))
        recorder.flush(fsync=True)
        events = _events(backend)
        excs = [
            e
            for e in events
            if e.type == EventType.FUNCTION_EXCEPTION and e.payload.get("function") == "fetch"
        ]
        assert excs

    def test_code_outside_project_root_not_traced(self, project_mod, recorder, backend):
        def repo_local_helper():  # defined in this test file, outside tmp project root
            return json.dumps({"x": 1})

        repo_local_helper()
        recorder.flush(fsync=True)
        events = _events(backend)
        names = {e.payload.get("function", "") for e in events}
        assert not any("repo_local_helper" in n for n in names)
        assert not any(n.startswith("json.") or n == "dumps" for n in names)

    def test_disable_frees_monitoring_tool(self, project_mod, recorder, backend):
        recorder.disable()
        for tool_id in (3, 4):
            assert sys.monitoring.get_tool(tool_id) != "slomo"

    def test_config_env_var_disables_autotrace(self, storage_root, monkeypatch):
        from slomo._core.config import load_config

        monkeypatch.setenv("SLOMO_AUTOTRACE", "0")
        cfg = load_config(storage_root)
        assert cfg.hooks.autotrace is False

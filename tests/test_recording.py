"""Recorder lifecycle, writer, storage, @track, hooks."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading

import pytest

from slomo._core.events import EventType
from slomo._core.recorder import Recorder
from slomo.storage.jsonl import JsonlBackend
from slomo.track import track


def _events(backend: JsonlBackend):
    sessions = backend.list_sessions()
    assert sessions, "no session recorded"
    return list(backend.iter_events(sessions[-1].id))


class TestRecorderLifecycle:
    def test_enable_creates_session(self, recorder, backend):
        recorder.flush(fsync=True)
        events = _events(backend)
        assert events[0].type == EventType.SESSION_STARTED

    def test_shutdown_finalizes(self, recorder, backend):
        session_id = recorder.session.id
        recorder.shutdown()
        meta = backend.read_session_meta(session_id)
        assert meta.status == "finished"
        assert meta.event_count > 0
        types = [e.type for e in backend.iter_events(session_id)]
        assert types[-1] == EventType.SESSION_FINISHED

    def test_enable_idempotent(self, recorder, backend):
        recorder.enable(root=backend.root)
        recorder.enable(root=backend.root)
        assert len(backend.list_sessions()) == 1

    def test_disabled_recorder_emits_nothing(self, storage_root, backend):
        Recorder._reset_for_tests()
        rec = Recorder.get()
        rec.emit(EventType.CUSTOM, {"name": "x"})
        assert backend.list_sessions() == []


class TestWriter:
    def test_concurrent_emit_all_lines_parse(self, recorder, backend):
        def worker(n):
            for i in range(1000):
                recorder.emit(EventType.CUSTOM, {"name": f"w{n}", "i": i})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recorder.flush(fsync=True)
        events = _events(backend)
        customs = [e for e in events if e.type == EventType.CUSTOM]
        assert len(customs) == 8000
        assert len({e.id for e in customs}) == 8000

    def test_truncated_tail_tolerated(self, recorder, backend):
        for i in range(10):
            recorder.emit(EventType.CUSTOM, {"i": i})
        recorder.flush(fsync=True)
        session_id = recorder.session.id
        path = backend.timeline_path(session_id)
        with path.open("ab") as f:
            f.write(b'{"id": "partial-line-no-newline')
        events = list(backend.iter_events(session_id))
        assert sum(1 for e in events if e.type == EventType.CUSTOM) == 10


class TestTrack:
    def test_sync(self, recorder, backend):
        @track
        def add(a, b):
            return a + b

        assert add(2, 3) == 5
        recorder.flush(fsync=True)
        events = _events(backend)
        enter = next(e for e in events if e.type == EventType.FUNCTION_ENTER)
        exit_ = next(e for e in events if e.type == EventType.FUNCTION_EXIT)
        assert enter.payload["function"].endswith("add")
        assert enter.payload["args"] == [2, 3]
        assert exit_.payload["result"] == 5
        assert exit_.payload["duration_ns"] > 0
        assert enter.span_id == exit_.span_id

    def test_exception_records_and_reraises(self, recorder, backend):
        @track
        def boom():
            raise ValueError("id=12345 broke")

        with pytest.raises(ValueError):
            boom()
        recorder.flush(fsync=True)
        events = _events(backend)
        exc = next(e for e in events if e.type == EventType.FUNCTION_EXCEPTION)
        assert exc.payload["exc_type"] == "ValueError"
        assert exc.payload["exception_id"]
        snap = next(e for e in events if e.type == EventType.VARIABLE_SNAPSHOT)
        assert snap.payload["exception_id"] == exc.payload["exception_id"]

    def test_nested_spans_parented(self, recorder, backend):
        @track
        def inner():
            return 1

        @track
        def outer():
            return inner()

        outer()
        recorder.flush(fsync=True)
        events = _events(backend)
        enters = [e for e in events if e.type == EventType.FUNCTION_ENTER]
        outer_e = next(e for e in enters if e.payload["function"].endswith("outer"))
        inner_e = next(e for e in enters if e.payload["function"].endswith("inner"))
        assert inner_e.parent_span_id == outer_e.span_id

    def test_async(self, recorder, backend):
        @track
        async def fetch(x):
            await asyncio.sleep(0)
            return x * 2

        assert asyncio.run(fetch(21)) == 42
        recorder.flush(fsync=True)
        events = _events(backend)
        assert any(
            e.type == EventType.FUNCTION_EXIT and e.payload.get("result") == 42 for e in events
        )

    def test_generator(self, recorder, backend):
        @track
        def gen(n):
            yield from range(n)

        assert list(gen(3)) == [0, 1, 2]
        recorder.flush(fsync=True)
        events = _events(backend)
        assert any(
            e.type == EventType.FUNCTION_EXIT and e.payload["function"].endswith("gen")
            for e in events
        )

    def test_disabled_fast_path(self):
        Recorder._reset_for_tests()

        @track
        def f(x):
            return x + 1

        assert f(1) == 2  # no recorder enabled — must just work

    def test_secret_args_redacted(self, recorder, backend):
        @track
        def login(username, password):
            return True

        login("amit", password="s3cret!")
        recorder.flush(fsync=True)
        events = _events(backend)
        enter = next(e for e in events if e.type == EventType.FUNCTION_ENTER)
        assert enter.payload["kwargs"]["password"] == "[REDACTED]"
        raw = json.dumps([e.payload for e in events])
        assert "s3cret!" not in raw


class TestHooks:
    def test_logging_hook(self, recorder, backend):
        logging.getLogger("myapp").warning("disk almost full: %d%%", 95)
        recorder.flush(fsync=True)
        events = _events(backend)
        w = next(e for e in events if e.type == EventType.WARNING)
        assert "disk almost full: 95%" in w.payload["message"]
        assert w.payload["logger"] == "myapp"

    def test_sqlite_hook(self, recorder, backend):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t (x INTEGER)")
        db.execute("INSERT INTO t VALUES (1)")
        rows = db.execute("SELECT * FROM t").fetchall()
        assert rows == [(1,)]
        recorder.flush(fsync=True)
        events = _events(backend)
        queries = [e for e in events if e.type == EventType.SQL_QUERY]
        results = [e for e in events if e.type == EventType.SQL_RESULT]
        assert len(queries) == 3 and len(results) == 3
        assert queries[0].payload["query_id"] == results[0].payload["query_id"]
        assert "params" not in queries[1].payload  # off by default

    def test_sqlite_error_recorded(self, recorder, backend):
        db = sqlite3.connect(":memory:")
        with pytest.raises(sqlite3.OperationalError):
            db.execute("SELECT * FROM missing_table")
        recorder.flush(fsync=True)
        events = _events(backend)
        assert any(
            e.type == EventType.SQL_RESULT and e.payload.get("error") == "OperationalError"
            for e in events
        )

    def test_requests_hook_no_network(self, recorder, backend):
        import requests
        from requests.adapters import BaseAdapter
        from requests.models import Response

        class FakeAdapter(BaseAdapter):
            def send(self, request, **kwargs):
                resp = Response()
                resp.status_code = 200
                resp._content = b'{"ok": true}'
                resp.headers["Content-Type"] = "application/json"
                resp.url = request.url
                resp.request = request
                return resp

            def close(self):
                pass

        # hook may not have been installed at enable() time if requests was
        # imported later — mirror the documented late-import path
        import slomo

        slomo.install_hooks()

        s = requests.Session()
        s.mount("https://", FakeAdapter())
        r = s.get("https://api.test/items", headers={"Authorization": "Bearer tok123"})
        assert r.status_code == 200
        recorder.flush(fsync=True)
        events = _events(backend)
        req = next(e for e in events if e.type == EventType.HTTP_REQUEST)
        resp = next(e for e in events if e.type == EventType.HTTP_RESPONSE)
        assert req.payload["method"] == "GET"
        assert req.payload["headers"]["Authorization"] == "[REDACTED]"
        assert resp.payload["status"] == 200
        assert req.payload["request_id"] == resp.payload["request_id"]

    def test_httpx_hook_no_network(self, recorder, backend):
        import httpx

        import slomo

        slomo.install_hooks()

        def handler(request):
            return httpx.Response(404, json={"missing": True})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            r = client.get("https://api.test/nope")
        assert r.status_code == 404
        recorder.flush(fsync=True)
        events = _events(backend)
        resp = next(e for e in events if e.type == EventType.HTTP_RESPONSE)
        assert resp.payload["status"] == 404
        assert resp.severity.value == "warning"

    def test_sqlalchemy_hook(self, recorder, backend):
        import sqlalchemy as sa

        import slomo

        slomo.install_hooks()

        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            conn.execute(sa.text("CREATE TABLE t (x INTEGER)"))
            conn.execute(sa.text("INSERT INTO t VALUES (7)"))
            rows = conn.execute(sa.text("SELECT x FROM t")).fetchall()
        assert rows == [(7,)]
        recorder.flush(fsync=True)
        events = _events(backend)
        sa_queries = [
            e
            for e in events
            if e.type == EventType.SQL_QUERY and e.payload.get("engine") == "sqlalchemy"
        ]
        assert len(sa_queries) >= 3
        # the sqlite3-level hook must NOT double-record what sqlalchemy ran
        raw_queries = [
            e
            for e in events
            if e.type == EventType.SQL_QUERY
            and e.payload.get("engine") == "sqlite3"
            and "t " in e.payload.get("sql", "")
        ]
        assert raw_queries == []


class TestSnapshotAPI:
    def test_manual_snapshot(self, recorder, backend):
        import slomo

        slomo.snapshot("checkpoint", user_id=42, token="abc123")
        recorder.flush(fsync=True)
        events = _events(backend)
        snap = next(e for e in events if e.type == EventType.VARIABLE_SNAPSHOT)
        assert snap.payload["label"] == "checkpoint"
        assert snap.payload["variables"]["user_id"] == 42
        assert snap.payload["variables"]["token"] == "[REDACTED]"

    def test_custom_event(self, recorder, backend):
        import slomo

        slomo.event("cache.warmed", entries=100)
        recorder.flush(fsync=True)
        events = _events(backend)
        e = next(e for e in events if e.type == EventType.CUSTOM)
        assert e.payload["name"] == "cache.warmed"
        assert e.payload["entries"] == 100

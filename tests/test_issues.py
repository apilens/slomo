"""Fingerprinting, classification, and the issue engine."""

from __future__ import annotations

import pytest

from slomo.issues.classify import Category, classify
from slomo.issues.engine import IssueEngine
from slomo.issues.fingerprint import fingerprint, normalize_message
from slomo.issues.index import IssueIndex
from slomo.storage import paths as storage_paths

FRAMES_A = [
    {"file": "/app/payment.py", "line": 180, "function": "process", "code": "x = charge()"},
    {"file": "/app/checkout.py", "line": 42, "function": "checkout", "code": "return user.id"},
]


class TestNormalization:
    def test_volatile_parts_normalized(self):
        a = normalize_message("user 12345 not found in 'db-prod-7' at /var/lib/app/data.db")
        b = normalize_message("user 99999 not found in 'db-prod-9' at /var/lib/app/other.db")
        assert a == b

    def test_uuid_and_hex(self):
        a = normalize_message("token deadbeefcafe1234 for 550e8400-e29b-41d4-a716-446655440000")
        assert "<hex>" in a and "<uuid>" in a


class TestFingerprint:
    def test_stable_across_line_numbers(self):
        moved = [dict(f, line=f["line"] + 30) for f in FRAMES_A]
        assert fingerprint("AttributeError", FRAMES_A, "x") == fingerprint(
            "AttributeError", moved, "x"
        )

    def test_stable_across_volatile_messages(self):
        assert fingerprint("KeyError", FRAMES_A, "user 123 missing") == fingerprint(
            "KeyError", FRAMES_A, "user 456 missing"
        )

    def test_different_frames_differ(self):
        other = [{"file": "/app/other.py", "line": 1, "function": "boom", "code": ""}]
        assert fingerprint("KeyError", FRAMES_A, "x") != fingerprint("KeyError", other, "x")

    def test_different_exc_type_differs(self):
        assert fingerprint("KeyError", FRAMES_A, "x") != fingerprint("TypeError", FRAMES_A, "x")


class TestClassify:
    @pytest.mark.parametrize(
        "exc_type,module,message,expected",
        [
            (
                "AttributeError",
                "",
                "'NoneType' object has no attribute 'id'",
                Category.NULL_REFERENCE,
            ),
            ("TypeError", "", "'NoneType' object is not subscriptable", Category.NULL_REFERENCE),
            ("ConnectionRefusedError", "", "connection refused", Category.NETWORK),
            ("OperationalError", "sqlite3", "no such table: users", Category.DATABASE),
            ("FileNotFoundError", "", "no such file: config.yml", Category.FILESYSTEM),
            ("TimeoutError", "", "operation timed out", Category.TIMEOUT),
            ("MemoryError", "", "", Category.MEMORY),
            ("ModuleNotFoundError", "", "no module named 'pandas'", Category.DEPENDENCY),
            ("IndexError", "", "list index out of range", Category.PROGRAMMING_ERROR),
            ("SomethingWeird", "", "???", Category.UNKNOWN),
        ],
    )
    def test_categories(self, exc_type, module, message, expected):
        category, _, confidence = classify(exc_type, module, message)
        assert category == expected
        if expected != Category.UNKNOWN:
            assert confidence >= 0.5


def _crash_session(recorder_root, exc_message="boom 123"):
    """Record a session that raises the same crash shape."""
    from slomo._core.recorder import Recorder

    Recorder._reset_for_tests()
    rec = Recorder.get()
    rec.enable(root=recorder_root, hooks=False)

    def failing():
        raise AttributeError(f"'NoneType' object has no attribute 'id' ({exc_message})")

    try:
        failing()
    except AttributeError as e:
        rec.record_exception(e, unhandled=True)
    rec.shutdown()
    Recorder._reset_for_tests()


class TestEngine:
    def _engine(self, backend):
        index = IssueIndex(storage_paths.issues_dir(backend.root) / "index.sqlite")
        return IssueEngine(backend, index)

    def test_incidents_group_into_one_issue(self, storage_root, backend):
        for i in range(3):
            _crash_session(storage_root, f"attempt {i}")
        engine = self._engine(backend)
        stats = engine.refresh()
        assert stats.incidents_added == 3
        issues = engine.index.list_issues()
        assert len(issues) == 1
        issue = issues[0]
        assert issue.occurrences == 3
        assert issue.affected_sessions == 3
        assert issue.category == "Null Reference"
        assert issue.id.startswith("SM-")

    def test_incremental_refresh_no_double_count(self, storage_root, backend):
        _crash_session(storage_root)
        engine = self._engine(backend)
        engine.refresh()
        first = engine.index.list_issues()[0].occurrences
        stats = engine.refresh()  # nothing new
        assert stats.incidents_added == 0
        assert engine.index.list_issues()[0].occurrences == first

    def test_exception_dedup_within_session(self, storage_root, backend):
        """function.exception + unhandled error for the SAME exception = one incident."""
        from slomo._core.recorder import Recorder
        from slomo.track import track

        Recorder._reset_for_tests()
        rec = Recorder.get()
        rec.enable(root=storage_root, hooks=False)

        @track
        def boom():
            raise KeyError("k")

        try:
            boom()
        except KeyError as e:
            rec.record_exception(e, unhandled=True)  # what the excepthook would do
        rec.shutdown()
        Recorder._reset_for_tests()

        engine = self._engine(backend)
        engine.refresh()
        issues = engine.index.list_issues()
        assert len(issues) == 1
        assert issues[0].occurrences == 1
        incidents = engine.index.incidents_for_issue(issues[0].id)
        assert incidents[0].unhandled is True  # upgraded by the later sighting

    def test_resolve_and_auto_reopen(self, storage_root, backend):
        _crash_session(storage_root)
        engine = self._engine(backend)
        engine.refresh()
        issue = engine.index.list_issues()[0]
        engine.resolve(issue.id)
        assert engine.index.get_issue(issue.id).status == "resolved"

        _crash_session(storage_root, "boom 999")  # same shape after normalization
        engine.refresh()
        reopened = engine.index.get_issue(issue.id)
        assert reopened.status == "open"
        assert reopened.occurrences == 2

    def test_rebuild_matches_refresh(self, storage_root, backend):
        for _ in range(2):
            _crash_session(storage_root)
        engine = self._engine(backend)
        engine.refresh()
        before = engine.index.counts()
        engine.rebuild()
        assert engine.index.counts() == before

    def test_get_issue_by_prefix(self, storage_root, backend):
        _crash_session(storage_root)
        engine = self._engine(backend)
        engine.refresh()
        issue = engine.index.list_issues()[0]
        assert engine.get_issue(issue.id).id == issue.id
        assert engine.get_issue(issue.id[:7]).id == issue.id  # "SM-xxxx" prefix
        assert engine.get_issue(issue.fingerprint[:10]).id == issue.id

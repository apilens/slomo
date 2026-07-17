"""Core primitives: ids, clock, events, serialization, redaction."""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from slomo._core.clock import HybridClock
from slomo._core.events import Event, EventType, Severity
from slomo._core.ids import new_span_id, uuid7
from slomo._core.redact import REDACTED, Redactor
from slomo._core.serialize import safe_repr, to_jsonable


class TestIds:
    def test_uuid7_is_time_ordered(self):
        ids = [uuid7() for _ in range(1000)]
        assert ids == sorted(ids)
        assert len(set(ids)) == 1000

    def test_uuid7_shape(self):
        u = uuid7()
        assert len(u) == 36
        assert u[14] == "7"  # version nibble

    def test_span_id(self):
        assert len(new_span_id()) == 16
        assert new_span_id() != new_span_id()


class TestClock:
    def test_monotonic(self):
        clock = HybridClock()
        stamps = [clock.now_ns() for _ in range(10_000)]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == len(stamps)


class TestEvent:
    def test_roundtrip(self):
        e = Event(
            id=uuid7(),
            session_id="s",
            timestamp=123,
            type=EventType.HTTP_REQUEST,
            severity=Severity.WARNING,
            trace_id="t",
            span_id="sp",
            parent_span_id=None,
            payload={"url": "https://x.test"},
        )
        back = Event.from_dict(json.loads(e.to_json_line()))
        assert back == e

    def test_unknown_type_tolerated(self):
        e = Event.from_dict({"type": "future.event", "severity": "hyper"})
        assert e.type == EventType.CUSTOM
        assert e.severity == Severity.INFO


class TestSerialize:
    def test_primitives_pass_through(self):
        assert to_jsonable({"a": 1, "b": [True, None, 2.5]}) == {"a": 1, "b": [True, None, 2.5]}

    def test_string_truncation(self):
        out = to_jsonable("x" * 5000, max_str=100)
        assert len(out) < 200 and "+4900 chars" in out

    def test_collection_cap(self):
        out = to_jsonable(list(range(100)), max_items=10)
        assert len(out) == 11 and out[-1] == {"__truncated__": 90}

    def test_cycles(self):
        d: dict = {}
        d["self"] = d
        out = to_jsonable(d)
        assert out["self"] == {"__cycle__": "dict"}

    def test_hostile_repr(self):
        class Evil:
            def __repr__(self):
                raise RuntimeError("no repr for you")

        text = safe_repr(Evil())
        assert "Evil" in text
        out = to_jsonable(Evil())
        assert out["__type__"].endswith("Evil")

    def test_depth_cap(self):
        nested = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
        out = to_jsonable(nested, max_depth=3)
        assert "__repr__" in json.dumps(out)


class TestRedactor:
    def test_secret_keys(self):
        r = Redactor()
        out = r.redact({"password": "hunter2", "user": "amit", "API_KEY": "abc"})
        assert out == {"password": REDACTED, "user": "amit", "API_KEY": REDACTED}

    def test_nested(self):
        r = Redactor()
        out = r.redact({"outer": {"authorization": "Bearer abc123xyz", "ok": 1}})
        assert out["outer"]["authorization"] == REDACTED
        assert out["outer"]["ok"] == 1

    def test_jwt_value(self):
        r = Redactor()
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpM"
        assert REDACTED in r.redact(f"token is {jwt}")

    def test_luhn_card(self):
        r = Redactor()
        assert REDACTED in r.redact("card 4242 4242 4242 4242 ok")  # Luhn-valid
        assert "1234 5678 9012 3456" in r.redact("num 1234 5678 9012 3456")  # Luhn-invalid

    def test_custom_patterns_merge(self):
        r = Redactor(key_patterns=["internal_id"], value_patterns=[r"MYCO-\d+"])
        out = r.redact({"internal_id": 5, "note": "see MYCO-123", "password": "x"})
        assert out["internal_id"] == REDACTED
        assert REDACTED in out["note"]
        assert out["password"] == REDACTED  # defaults still merged

    def test_headers(self):
        r = Redactor()
        out = r.redact_headers({"Authorization": "Bearer tok", "Accept": "json"})
        assert out["Authorization"] == REDACTED
        assert out["Accept"] == "json"

    @given(
        st.recursive(
            st.dictionaries(
                st.sampled_from(["password", "token", "name", "data", "secret_key"]),
                st.text(max_size=30),
                max_size=4,
            ),
            lambda children: st.dictionaries(
                st.sampled_from(["nest", "inner"]), children, max_size=2
            ),
            max_leaves=8,
        )
    )
    def test_no_secret_key_survives(self, data):
        out = Redactor().redact(data)

        def check(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ("password", "token", "secret_key"):
                        assert v == REDACTED
                    else:
                        check(v)

        check(out)


class TestFrameClassification:
    def test_user_project_named_slomo_is_not_internal(self, tmp_path):
        """A project directory literally called 'slomo' (e.g. a repo checkout)
        must not classify user frames as slomo-internal — that would silently
        drop crash-locals snapshots (regression: CI checkout at .../slomo/slomo)."""
        from slomo._core import frames

        user_file = str(tmp_path / "slomo" / "app.py")
        assert not frames.is_internal_file(user_file)
        assert frames.is_project_file(user_file)

    def test_package_files_are_internal(self):
        from slomo._core import frames

        assert frames.is_internal_file(frames.__file__)
        assert frames.is_internal_file("<string>")
        assert frames.is_internal_file("")

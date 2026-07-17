"""CLI commands via Typer's CliRunner against fixture storage."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from slomo.cli.app import app

runner = CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def recorded(storage_root):
    """One finished session with a tracked crash, sql-ish and custom events."""
    from slomo._core.recorder import Recorder
    from slomo.track import track

    Recorder._reset_for_tests()
    rec = Recorder.get()
    rec.enable(root=storage_root, hooks=False)

    @track
    def fetch_user(user_id):
        raise AttributeError("'NoneType' object has no attribute 'id'")

    try:
        fetch_user(42)
    except AttributeError as e:
        rec.record_exception(e, unhandled=True)
        rec.mark_crashed()  # what the excepthook does before the process dies
    rec.shutdown()
    Recorder._reset_for_tests()
    return storage_root


def _invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, f"{args}: {result.output}"
    return result.output


def _first_issue_id(storage_root) -> str:
    from slomo.issues.engine import IssueEngine
    from slomo.issues.index import IssueIndex
    from slomo.storage import paths
    from slomo.storage.jsonl import JsonlBackend

    backend = JsonlBackend(storage_root)
    index = IssueIndex(paths.issues_dir(storage_root) / "index.sqlite")
    engine = IssueEngine(backend, index)
    engine.refresh()
    return engine.index.list_issues()[0].id


class TestCli:
    def test_no_root_is_helpful(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SLOMO_HOME", str(tmp_path / "nowhere"))
        result = runner.invoke(app, ["sessions"])
        assert result.exit_code == 1
        assert "enable()" in result.output

    def test_sessions(self, recorded):
        out = _invoke("sessions")
        assert "crashed" in out
        assert "1 session(s)" in out

    def test_session_show_roundtrips_short_id(self, recorded):
        import re

        out = _invoke("sessions")
        short = re.search(r"[0-9a-f]{12}", out).group(0)
        out = _invoke("session", "show", short)
        assert "crashed" in out
        assert "AttributeError" in out

    def test_issues_and_doctor(self, recorded):
        out = _invoke("issues")
        assert "SM-" in out and "Null Reference" in out
        issue_id = _first_issue_id(recorded)
        out = _invoke("doctor", issue_id)
        assert "Likely root cause" in out
        assert "Suggested fix" in out
        out = _invoke("issue", "explain", issue_id)
        assert "Null Reference" in out

    def test_issue_lifecycle(self, recorded):
        issue_id = _first_issue_id(recorded)
        assert "resolved" in _invoke("issue", "resolve", issue_id)
        assert "no issues" in _invoke("issues")  # open filter hides it
        assert issue_id in _invoke("issues", "--all")
        assert "reopened" in _invoke("issue", "reopen", issue_id)

    def test_timeline_and_replay_trace(self, recorded):
        out = _invoke("timeline")
        assert "session.started" in out and "error" in out
        issue_id = _first_issue_id(recorded)
        out = _invoke("replay", issue_id, "--trace")
        assert "function.enter" in out

    def test_replay_json(self, recorded):
        import json

        out = _invoke("replay", "--json")
        lines = [json.loads(ln) for ln in out.strip().splitlines() if ln.startswith("{")]
        assert lines[0]["type"] == "session.started"

    def test_search(self, recorded):
        _first_issue_id(recorded)  # populates FTS
        out = _invoke("search", "NoneType")
        assert "error" in out

    def test_vars(self, recorded):
        out = _invoke("vars")
        assert "user_id" in out  # exception frame locals captured

    def test_stats(self, recorded):
        out = _invoke("stats")
        assert "sessions" in out and "issues" in out

    def test_export_all_formats(self, recorded, tmp_path):
        for fmt in ("json", "markdown", "csv", "html"):
            out = _invoke("export", fmt)
            assert "exported" in out

    def test_session_delete(self, recorded):
        import re

        out = _invoke("sessions")
        short = re.search(r"[0-9a-f]{12}", out).group(0)
        _invoke("session", "delete", short, "--yes")
        assert "no sessions" in _invoke("sessions")

    def test_version(self, recorded):
        assert "slomo" in _invoke("--version")


class TestInit:
    def test_creates_root_with_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SLOMO_HOME", raising=False)
        out = _invoke("init", str(tmp_path))
        assert "initialized" in out
        root = tmp_path / ".slomo"
        assert (root / "config.toml").is_file()
        assert (root / "sessions").is_dir()
        assert (root / ".gitignore").read_text().strip().endswith("*")

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SLOMO_HOME", raising=False)
        _invoke("init", str(tmp_path))
        marker = tmp_path / ".slomo" / "config.toml"
        marker.write_text("# customized\n", encoding="utf-8")
        out = _invoke("init", str(tmp_path))
        assert "already initialized" in out
        assert marker.read_text() == "# customized\n"  # never clobbers edits

    def test_warns_when_parent_project_initialized(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SLOMO_HOME", raising=False)
        _invoke("init", str(tmp_path))
        child = tmp_path / "subdir"
        child.mkdir()
        out = _invoke("init", str(child))
        assert "parent project is already initialized" in out
        assert (child / ".slomo" / "config.toml").is_file()

    def test_rejects_missing_directory(self, tmp_path):
        result = runner.invoke(app, ["init", str(tmp_path / "nope")])
        assert result.exit_code == 1

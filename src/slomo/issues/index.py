"""SQLite index over the JSONL timelines. Owned by the CLI process only;
always rebuildable from the source-of-truth JSONL files."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from slomo.issues.models import Incident, Issue

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    fingerprint TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    stability TEXT NOT NULL DEFAULT 'one-time',
    occurrences INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    affected_sessions INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    exc_type TEXT NOT NULL DEFAULT '',
    top_frame_json TEXT NOT NULL DEFAULT '{}',
    resolved_at INTEGER,
    fp_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS incidents (
    event_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(id),
    session_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    exc_type TEXT NOT NULL,
    message TEXT NOT NULL,
    frames_json TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL,
    unhandled INTEGER NOT NULL DEFAULT 0,
    exception_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    span_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_issue ON incidents(issue_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_incidents_session ON incidents(session_id);

CREATE TABLE IF NOT EXISTS sessions_indexed (
    session_id TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    indexed_at INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    event_id UNINDEXED,
    session_id UNINDEXED,
    type UNINDEXED,
    timestamp UNINDEXED,
    text
);
"""


class IssueIndex:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- incremental cursor ----------

    def session_offset(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT byte_offset FROM sessions_indexed WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["byte_offset"]) if row else 0

    def set_session_offset(self, session_id: str, offset: int, indexed_at: int) -> None:
        self._conn.execute(
            "INSERT INTO sessions_indexed (session_id, byte_offset, indexed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET byte_offset = ?, indexed_at = ?",
            (session_id, offset, indexed_at, offset, indexed_at),
        )

    # ---------- issues ----------

    def get_issue(self, issue_id: str) -> Issue | None:
        row = self._conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return self._row_to_issue(row) if row else None

    def get_issue_by_fingerprint(self, fp: str) -> Issue | None:
        row = self._conn.execute("SELECT * FROM issues WHERE fingerprint = ?", (fp,)).fetchone()
        return self._row_to_issue(row) if row else None

    def resolve_issue_ref(self, ref: str) -> Issue | None:
        """Accept an exact id, an id prefix, or a fingerprint prefix."""
        issue = self.get_issue(ref)
        if issue:
            return issue
        rows = self._conn.execute(
            "SELECT * FROM issues WHERE id LIKE ? OR fingerprint LIKE ?",
            (f"{ref}%", f"{ref}%"),
        ).fetchall()
        if len(rows) == 1:
            return self._row_to_issue(rows[0])
        return None

    def upsert_issue(self, issue: Issue) -> None:
        self._conn.execute(
            """
            INSERT INTO issues (id, fingerprint, title, category, severity, status, stability,
                occurrences, first_seen, last_seen, affected_sessions, confidence, exc_type,
                top_frame_json, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                title = excluded.title, category = excluded.category,
                severity = excluded.severity, status = excluded.status,
                stability = excluded.stability, occurrences = excluded.occurrences,
                first_seen = excluded.first_seen, last_seen = excluded.last_seen,
                affected_sessions = excluded.affected_sessions,
                confidence = excluded.confidence, exc_type = excluded.exc_type,
                top_frame_json = excluded.top_frame_json, resolved_at = excluded.resolved_at
            """,
            (
                issue.id,
                issue.fingerprint,
                issue.title,
                issue.category,
                issue.severity,
                issue.status,
                issue.stability,
                issue.occurrences,
                issue.first_seen,
                issue.last_seen,
                issue.affected_sessions,
                issue.confidence,
                issue.exc_type,
                json.dumps(issue.top_frame, default=str),
                issue.resolved_at,
            ),
        )

    def list_issues(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        since: int | None = None,
    ) -> list[Issue]:
        query = "SELECT * FROM issues WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if category:
            query += " AND lower(category) = lower(?)"
            params.append(category)
        if since:
            query += " AND last_seen >= ?"
            params.append(since)
        query += " ORDER BY last_seen DESC"
        return [self._row_to_issue(r) for r in self._conn.execute(query, params).fetchall()]

    def set_issue_status(self, issue_id: str, status: str, resolved_at: int | None) -> None:
        stability_sql = ", stability = 'resolved'" if status == "resolved" else ""
        self._conn.execute(
            f"UPDATE issues SET status = ?, resolved_at = ?{stability_sql} WHERE id = ?",
            (status, resolved_at, issue_id),
        )
        self._conn.commit()

    # ---------- incidents ----------

    def add_incident(self, incident: Incident) -> bool:
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO incidents (event_id, issue_id, session_id, timestamp, exc_type,
                message, frames_json, fingerprint, unhandled, exception_id, trace_id, span_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident.event_id,
                incident.issue_id,
                incident.session_id,
                incident.timestamp,
                incident.exc_type,
                incident.message,
                json.dumps(incident.frames, default=str),
                incident.fingerprint,
                int(incident.unhandled),
                incident.exception_id,
                incident.trace_id,
                incident.span_id,
            ),
        )
        return cur.rowcount > 0

    def mark_unhandled(self, session_id: str, exception_id: str) -> None:
        self._conn.execute(
            "UPDATE incidents SET unhandled = 1 WHERE session_id = ? AND exception_id = ?",
            (session_id, exception_id),
        )

    def has_exception_id(self, session_id: str, exception_id: str) -> bool:
        if not exception_id:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM incidents WHERE session_id = ? AND exception_id = ? LIMIT 1",
            (session_id, exception_id),
        ).fetchone()
        return row is not None

    def incidents_for_issue(self, issue_id: str, limit: int = 100) -> list[Incident]:
        rows = self._conn.execute(
            "SELECT * FROM incidents WHERE issue_id = ? ORDER BY timestamp DESC LIMIT ?",
            (issue_id, limit),
        ).fetchall()
        return [self._row_to_incident(r) for r in rows]

    def issue_stats(self, issue_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS occurrences,
                   COUNT(DISTINCT session_id) AS affected_sessions,
                   MIN(timestamp) AS first_seen,
                   MAX(timestamp) AS last_seen,
                   SUM(unhandled) AS unhandled_count
            FROM incidents WHERE issue_id = ?
            """,
            (issue_id,),
        ).fetchone()
        return dict(row) if row else {}

    def distinct_days(self, issue_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT timestamp / 86400000000000) AS days "
            "FROM incidents WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        return int(row["days"]) if row else 0

    # ---------- full-text search ----------

    def add_fts(
        self, event_id: str, session_id: str, type_: str, timestamp: int, text: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO events_fts (event_id, session_id, type, timestamp, text) VALUES (?, ?, ?, ?, ?)",
            (event_id, session_id, type_, timestamp, text),
        )

    def search_fts(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        try:
            rows = self._conn.execute(
                "SELECT event_id, session_id, type, timestamp, "
                "snippet(events_fts, 4, '[', ']', '…', 12) AS snippet "
                "FROM events_fts WHERE events_fts MATCH ? ORDER BY timestamp DESC LIMIT ?",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS syntax error from raw user input: retry as a quoted phrase
            try:
                quoted = '"' + query.replace('"', '""') + '"'
                rows = self._conn.execute(
                    "SELECT event_id, session_id, type, timestamp, "
                    "snippet(events_fts, 4, '[', ']', '…', 12) AS snippet "
                    "FROM events_fts WHERE events_fts MATCH ? ORDER BY timestamp DESC LIMIT ?",
                    (quoted, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    # ---------- maintenance ----------

    def commit(self) -> None:
        self._conn.commit()

    def rebuild(self) -> None:
        for table in ("issues", "incidents", "sessions_indexed", "events_fts"):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("issues", "incidents", "sessions_indexed"):
            out[table] = self._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        return out

    # ---------- row mapping ----------

    @staticmethod
    def _row_to_issue(row: sqlite3.Row) -> Issue:
        return Issue(
            id=row["id"],
            fingerprint=row["fingerprint"],
            title=row["title"],
            category=row["category"],
            severity=row["severity"],
            status=row["status"],
            stability=row["stability"],
            occurrences=row["occurrences"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            affected_sessions=row["affected_sessions"],
            confidence=row["confidence"],
            exc_type=row["exc_type"],
            top_frame=json.loads(row["top_frame_json"] or "{}"),
            resolved_at=row["resolved_at"],
        )

    @staticmethod
    def _row_to_incident(row: sqlite3.Row) -> Incident:
        return Incident(
            event_id=row["event_id"],
            issue_id=row["issue_id"],
            session_id=row["session_id"],
            timestamp=row["timestamp"],
            exc_type=row["exc_type"],
            message=row["message"],
            frames=json.loads(row["frames_json"] or "[]"),
            fingerprint=row["fingerprint"],
            unhandled=bool(row["unhandled"]),
            exception_id=row["exception_id"],
            trace_id=row["trace_id"],
            span_id=row["span_id"],
        )

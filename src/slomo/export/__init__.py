"""Exporters: session or issue data to json / markdown / csv / html files
under .slomo/exports/."""

from __future__ import annotations

import csv
import html
import io
import json
import time
from pathlib import Path
from typing import Any

from slomo._core.events import Event
from slomo.cli.render import format_ts
from slomo.issues.models import Incident, Issue

FORMATS = ("json", "markdown", "csv", "html")


def export_events(
    events: list[Event],
    fmt: str,
    out_dir: Path,
    *,
    title: str,
    issues: list[Issue] | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_title = "".join(c if c.isalnum() or c in "-_" else "-" for c in title)[:40]
    ext = {"json": "json", "markdown": "md", "csv": "csv", "html": "html"}[fmt]
    path = out_dir / f"{safe_title}-{stamp}.{ext}"
    if fmt == "json":
        path.write_text(_to_json(events, issues), encoding="utf-8")
    elif fmt == "markdown":
        path.write_text(_to_markdown(events, title, issues), encoding="utf-8")
    elif fmt == "csv":
        path.write_text(_to_csv(events), encoding="utf-8")
    elif fmt == "html":
        path.write_text(_to_html(events, title, issues), encoding="utf-8")
    else:
        raise ValueError(f"unknown format: {fmt}")
    return path


def _event_dict(e: Event) -> dict[str, Any]:
    return {
        "id": e.id,
        "session_id": e.session_id,
        "timestamp": e.timestamp,
        "time": format_ts(e.timestamp),
        "type": str(e.type),
        "severity": str(e.severity),
        "trace_id": e.trace_id,
        "span_id": e.span_id,
        "parent_span_id": e.parent_span_id,
        "payload": e.payload,
    }


def _issue_dict(i: Issue) -> dict[str, Any]:
    return {
        "id": i.id,
        "title": i.title,
        "category": i.category,
        "severity": i.severity,
        "status": i.status,
        "stability": i.stability,
        "occurrences": i.occurrences,
        "first_seen": format_ts(i.first_seen),
        "last_seen": format_ts(i.last_seen),
        "affected_sessions": i.affected_sessions,
        "confidence": i.confidence,
    }


def _to_json(events: list[Event], issues: list[Issue] | None) -> str:
    doc: dict[str, Any] = {"generator": "slomo", "events": [_event_dict(e) for e in events]}
    if issues:
        doc["issues"] = [_issue_dict(i) for i in issues]
    return json.dumps(doc, indent=2, default=str)


def _to_markdown(events: list[Event], title: str, issues: list[Issue] | None) -> str:
    from slomo.cli.render import event_summary

    lines = [f"# slomo export — {title}", ""]
    if issues:
        lines += [
            "## Issues",
            "",
            "| issue | title | category | count | status |",
            "|---|---|---|---|---|",
        ]
        for i in issues:
            lines.append(f"| {i.id} | {i.title} | {i.category} | {i.occurrences} | {i.status} |")
        lines.append("")
    lines += ["## Timeline", "", "| time | type | severity | summary |", "|---|---|---|---|"]
    for e in events:
        summary = event_summary(e).replace("|", "\\|")
        lines.append(f"| {format_ts(e.timestamp)} | {e.type} | {e.severity} | {summary} |")
    lines.append("")
    return "\n".join(lines)


def _to_csv(events: list[Event]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "session_id",
            "timestamp",
            "time",
            "type",
            "severity",
            "trace_id",
            "span_id",
            "payload",
        ]
    )
    for e in events:
        writer.writerow(
            [
                e.id,
                e.session_id,
                e.timestamp,
                format_ts(e.timestamp),
                str(e.type),
                str(e.severity),
                e.trace_id,
                e.span_id or "",
                json.dumps(e.payload, default=str),
            ]
        )
    return buf.getvalue()


def _to_html(events: list[Event], title: str, issues: list[Issue] | None) -> str:
    from slomo.cli.render import event_summary

    def esc(s: Any) -> str:
        return html.escape(str(s))

    rows = "\n".join(
        f"<tr class='sev-{esc(e.severity)}'><td>{esc(format_ts(e.timestamp))}</td>"
        f"<td>{esc(e.type)}</td><td>{esc(e.severity)}</td><td>{esc(event_summary(e))}</td></tr>"
        for e in events
    )
    issues_html = ""
    if issues:
        issue_rows = "\n".join(
            f"<tr><td>{esc(i.id)}</td><td>{esc(i.title)}</td><td>{esc(i.category)}</td>"
            f"<td>{i.occurrences}</td><td>{esc(i.status)}</td></tr>"
            for i in issues
        )
        issues_html = f"<h2>Issues</h2><table><tr><th>issue</th><th>title</th><th>category</th><th>count</th><th>status</th></tr>{issue_rows}</table>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>slomo — {esc(title)}</title>
<style>
body {{ font-family: ui-monospace, monospace; margin: 2rem; background: #0d1117; color: #e6edf3; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
td, th {{ border-bottom: 1px solid #30363d; padding: 4px 10px; text-align: left; font-size: 13px; }}
th {{ color: #58a6ff; }}
.sev-error td, .sev-critical td {{ color: #f85149; }}
.sev-warning td {{ color: #d29922; }}
h1, h2 {{ color: #58a6ff; }}
</style></head>
<body><h1>slomo — {esc(title)}</h1>
{issues_html}
<h2>Timeline ({len(events)} events)</h2>
<table><tr><th>time</th><th>type</th><th>severity</th><th>summary</th></tr>
{rows}
</table></body></html>
"""


__all__ = ["export_events", "FORMATS"]


def export_incidents_csv(incidents: list[Incident]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["event_id", "issue_id", "session_id", "timestamp", "exc_type", "message", "unhandled"]
    )
    for i in incidents:
        writer.writerow(
            [i.event_id, i.issue_id, i.session_id, i.timestamp, i.exc_type, i.message, i.unhandled]
        )
    return buf.getvalue()

"""Shared Rich rendering helpers for the CLI."""

from __future__ import annotations

import time as _time

from rich.table import Table
from rich.text import Text

from slomo._core.events import Event, EventType, Severity
from slomo._core.session import SessionMeta
from slomo.issues.models import Issue

_SEVERITY_STYLES = {
    Severity.DEBUG: "dim",
    Severity.INFO: "white",
    Severity.WARNING: "yellow",
    Severity.ERROR: "red",
    Severity.CRITICAL: "bold red",
}

_STATUS_STYLES = {
    "running": "cyan",
    "finished": "green",
    "crashed": "bold red",
    "abandoned": "yellow",
    "open": "red",
    "resolved": "green",
}


def severity_style(sev: Severity) -> str:
    return _SEVERITY_STYLES.get(sev, "white")


def status_text(status: str) -> Text:
    return Text(status, style=_STATUS_STYLES.get(status, "white"))


def format_ts(ns: int, *, time_only: bool = False) -> str:
    if not ns:
        return "-"
    t = _time.localtime(ns / 1e9)
    if time_only:
        return _time.strftime("%H:%M:%S", t) + f".{(ns // 1_000_000) % 1000:03d}"
    return _time.strftime("%Y-%m-%d %H:%M:%S", t)


def format_ago(ns: int) -> str:
    if not ns:
        return "-"
    delta = _time.time() - ns / 1e9
    if delta < 0:
        return "now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= size:
            return f"{int(delta // size)}{unit} ago"
    return f"{int(delta)}s ago"


def format_duration(ns: int | None) -> str:
    if ns is None:
        return "-"
    if ns >= 1_000_000_000:
        return f"{ns / 1e9:.2f}s"
    if ns >= 1_000_000:
        return f"{ns / 1e6:.1f}ms"
    return f"{ns / 1e3:.0f}µs"


def short_id(id_: str, n: int = 12) -> str:
    return id_.replace("-", "")[:n]


def event_summary(e: Event) -> str:
    p = e.payload
    match e.type:
        case EventType.FUNCTION_ENTER:
            return f"→ {p.get('function', '?')}()"
        case EventType.FUNCTION_EXIT:
            out = f"← {p.get('function', '?')}()  {format_duration(p.get('duration_ns'))}"
            if p.get("outcome") == "exception":
                out += f"  ✗ {p.get('exc_type', '')}"
            return out
        case EventType.FUNCTION_EXCEPTION | EventType.ERROR:
            return f"{p.get('exc_type', 'error')}: {str(p.get('message', ''))[:100]}"
        case EventType.HTTP_REQUEST:
            return f"{p.get('method', '?')} {str(p.get('url', ''))[:90]}"
        case EventType.HTTP_RESPONSE:
            if p.get("error"):
                return f"✗ {p.get('error')} {str(p.get('message', ''))[:70]}"
            return f"{p.get('status', '?')} in {format_duration(p.get('duration_ns'))}"
        case EventType.SQL_QUERY:
            return str(p.get("sql", ""))[:100].replace("\n", " ")
        case EventType.SQL_RESULT:
            if p.get("error"):
                return f"✗ {p.get('error')}"
            rc = p.get("rowcount")
            rows = f"{rc} rows, " if isinstance(rc, int) and rc >= 0 else ""
            return f"{rows}{format_duration(p.get('duration_ns'))}"
        case EventType.VARIABLE_SNAPSHOT:
            label = p.get("label") or p.get("source", "")
            keys = list((p.get("variables") or {}).keys())[:6]
            return f"{label} {{{', '.join(keys)}}}" if keys else str(label)
        case EventType.LOG | EventType.WARNING:
            return f"[{p.get('logger', '')}] {str(p.get('message', ''))[:100]}"
        case EventType.SESSION_STARTED:
            return " ".join(p.get("argv", []))[:100]
        case EventType.SESSION_FINISHED:
            return f"status={p.get('status', '?')} events={p.get('event_count', '?')}"
        case EventType.CUSTOM:
            return str(p.get("name", ""))[:100]
        case _:
            return str(p)[:100]


def sessions_table(sessions: list[SessionMeta]) -> Table:
    table = Table(title=None, header_style="bold")
    table.add_column("session", style="cyan")
    table.add_column("started")
    table.add_column("status")
    table.add_column("events", justify="right")
    table.add_column("errors", justify="right")
    table.add_column("entrypoint", overflow="ellipsis", max_width=40)
    for m in sessions:
        table.add_row(
            short_id(m.id),
            f"{format_ts(m.started_at)} ({format_ago(m.started_at)})",
            status_text(m.status),
            str(m.event_count),
            Text(str(m.error_count), style="red" if m.error_count else "dim"),
            m.entrypoint,
        )
    return table


def issues_table(issues: list[Issue]) -> Table:
    table = Table(header_style="bold")
    table.add_column("issue", style="cyan")
    table.add_column("title", overflow="ellipsis", max_width=56)
    table.add_column("category")
    table.add_column("count", justify="right")
    table.add_column("stability")
    table.add_column("last seen")
    table.add_column("status")
    for i in issues:
        table.add_row(
            i.id,
            i.title,
            i.category,
            str(i.occurrences),
            i.stability,
            format_ago(i.last_seen),
            status_text(i.status),
        )
    return table

"""The slomo CLI."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from slomo import __version__
from slomo._core.events import Event, EventType, Severity
from slomo.cli import render
from slomo.issues.engine import IssueEngine
from slomo.issues.index import IssueIndex
from slomo.query.reader import EventReader
from slomo.storage import paths
from slomo.storage.jsonl import JsonlBackend

app = typer.Typer(
    name="slomo",
    help="The black box flight recorder for Python applications.",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
)
session_app = typer.Typer(help="Inspect and manage recorded sessions.")
issue_app = typer.Typer(help="Inspect and manage issues (grouped crashes).")
app.add_typer(session_app, name="session")
app.add_typer(issue_app, name="issue")

console = Console()
err_console = Console(stderr=True)


# ---------- plumbing ----------


def _root() -> Path:
    root = paths.find_root()
    if root is None:
        err_console.print(
            "[red]No .slomo directory found here or in any parent.[/]\n"
            "Record something first:\n\n"
            "    from slomo import enable\n"
            "    enable()\n"
        )
        raise typer.Exit(1)
    return root


def _backend() -> JsonlBackend:
    return JsonlBackend(_root())


def _engine(backend: JsonlBackend | None = None) -> IssueEngine:
    backend = backend or _backend()
    index = IssueIndex(paths.issues_dir(backend.root) / "index.sqlite")
    engine = IssueEngine(backend, index)
    engine.refresh()
    return engine


def _resolve_session(backend: JsonlBackend, ref: str) -> str:
    try:
        return backend.resolve_session_id(ref)
    except KeyError as e:
        err_console.print(f"[red]{e.args[0]}[/]")
        raise typer.Exit(1) from None


def _events_for_ref(ref: str | None) -> tuple[list[Event], str]:
    """A ref is an issue id (SM-xxxx), a session id prefix, or None (latest session)."""
    backend = _backend()
    if ref and ref.upper().startswith("SM-"):
        engine = _engine(backend)
        issue = engine.get_issue(ref)
        if issue is None:
            err_console.print(f"[red]no issue matches {ref!r}[/]")
            raise typer.Exit(1)
        incidents = engine.index.incidents_for_issue(issue.id, limit=1)
        if not incidents:
            err_console.print(f"[red]{issue.id} has no recorded incidents[/]")
            raise typer.Exit(1)
        session_id = incidents[0].session_id
        return list(
            backend.iter_events(session_id)
        ), f"issue {issue.id} (session {render.short_id(session_id)})"
    if ref is None:
        sessions = backend.list_sessions()
        if not sessions:
            err_console.print("[red]no sessions recorded yet[/]")
            raise typer.Exit(1)
        session_id = sessions[-1].id
    else:
        session_id = _resolve_session(backend, ref)
    return list(backend.iter_events(session_id)), f"session {render.short_id(session_id)}"


# ---------- root ----------


@app.callback(invoke_without_command=True)
def _main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
) -> None:
    if version:
        console.print(f"slomo {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty():
            from slomo.cli.shell import run_shell

            run_shell()
        else:
            console.print(ctx.get_help())


# ---------- sessions ----------


@app.command("sessions")
def sessions_list(
    limit: int = typer.Option(25, "--limit", "-n", help="Show at most N sessions."),
) -> None:
    """List recorded sessions (newest last)."""
    sessions = _backend().list_sessions()
    if not sessions:
        console.print("[dim]no sessions recorded yet[/]")
        return
    console.print(render.sessions_table(sessions[-limit:]))
    console.print(f"[dim]{len(sessions)} session(s) total[/]")


@session_app.command("show")
def session_show(ref: str = typer.Argument(help="Session id (prefix ok).")) -> None:
    """Session metadata and event breakdown."""
    backend = _backend()
    session_id = _resolve_session(backend, ref)
    meta = backend.read_session_meta(session_id)
    counts: dict[str, int] = {}
    errors = []
    for e in backend.iter_events(session_id):
        counts[str(e.type)] = counts.get(str(e.type), 0) + 1
        if e.type in (EventType.FUNCTION_EXCEPTION, EventType.ERROR):
            errors.append(e)
    body = Text()
    body.append(f"id          {meta.id}\n")
    body.append(
        f"started     {render.format_ts(meta.started_at)} ({render.format_ago(meta.started_at)})\n"
    )
    body.append(f"finished    {render.format_ts(meta.finished_at) if meta.finished_at else '-'}\n")
    body.append("status      ")
    body.append(
        meta.status,
        style={
            "crashed": "bold red",
            "finished": "green",
            "running": "cyan",
            "abandoned": "yellow",
        }.get(meta.status, "white"),
    )
    body.append(f"\npid         {meta.pid}\n")
    body.append(f"entrypoint  {' '.join(meta.argv)}\n")
    body.append(f"python      {meta.python} on {meta.platform} ({meta.hostname})\n")
    if meta.labels:
        body.append(f"labels      {meta.labels}\n")
    console.print(Panel(body, title=f"session {render.short_id(meta.id)}", border_style="cyan"))
    table = Table(header_style="bold", box=None)
    table.add_column("event type")
    table.add_column("count", justify="right")
    for t, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        table.add_row(t, str(c))
    console.print(table)
    for e in errors[:3]:
        console.print(
            Panel(
                f"[red]{e.payload.get('exc_type')}[/]: {e.payload.get('message')}",
                title="error",
                border_style="red",
            )
        )


@session_app.command("inspect")
def session_inspect(ref: str = typer.Argument(help="Session id (prefix ok).")) -> None:
    """Span tree: tracked function calls with durations."""
    backend = _backend()
    session_id = _resolve_session(backend, ref)
    reader = EventReader(backend)
    root = reader.spans_tree(session_id)
    tree = Tree(f"[bold cyan]session {render.short_id(session_id)}[/]")

    def add(node, branch) -> None:
        for child in node.children:
            duration = None
            failed = False
            is_function = False
            for e in child.events:
                if e.type in (EventType.FUNCTION_ENTER, EventType.FUNCTION_EXIT):
                    is_function = True
                if e.type == EventType.FUNCTION_EXIT:
                    duration = e.payload.get("duration_ns")
                    failed = e.payload.get("outcome") == "exception"
                if e.type == EventType.FUNCTION_EXCEPTION:
                    failed = True
            if is_function:
                label = f"{child.label}()  [dim]{render.format_duration(duration)}[/]"
            else:
                first = child.events[0]
                label = f"[dim]{first.type}[/]  {render.event_summary(first)}"
                if any(e.payload.get("error") for e in child.events):
                    failed = True
            if failed:
                label += "  [red]✗[/]"
            add(child, branch.add(label))

    add(root, tree)
    console.print(tree)


@session_app.command("delete")
def session_delete(
    ref: str = typer.Argument(help="Session id (prefix ok)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a recorded session."""
    backend = _backend()
    session_id = _resolve_session(backend, ref)
    if not yes and not typer.confirm(f"delete session {render.short_id(session_id)}?"):
        raise typer.Exit()
    backend.delete_session(session_id)
    console.print(f"[green]deleted[/] {render.short_id(session_id)}")


# ---------- issues ----------


@app.command("issues")
def issues_list(
    status: str | None = typer.Option(None, "--status", help="open | resolved"),
    category: str | None = typer.Option(None, "--category"),
    all_: bool = typer.Option(False, "--all", "-a", help="Include resolved issues."),
) -> None:
    """Crashes grouped into issues. One issue = many identical incidents."""
    engine = _engine()
    if status is None and not all_:
        status = "open"
    issues = engine.index.list_issues(status=status, category=category)
    if not issues:
        console.print("[dim]no issues — nice.[/]")
        return
    console.print(render.issues_table(issues))


def _get_issue_or_exit(engine: IssueEngine, ref: str):
    issue = engine.get_issue(ref)
    if issue is None:
        err_console.print(f"[red]no issue matches {ref!r}[/]")
        raise typer.Exit(1)
    return issue


@issue_app.command("show")
def issue_show(ref: str = typer.Argument(help="Issue id, e.g. SM-1a2b3c4d.")) -> None:
    """Full detail for one issue, including a sample traceback."""
    engine = _engine()
    issue = _get_issue_or_exit(engine, ref)
    body = Text()
    body.append(f"{issue.title}\n\n", style="bold")
    body.append(f"category    {issue.category}   (confidence {issue.confidence:.0%})\n")
    body.append(f"severity    {issue.severity}\n")
    body.append(f"status      {issue.status}   stability {issue.stability}\n")
    body.append(f"occurrences {issue.occurrences} across {issue.affected_sessions} session(s)\n")
    body.append(
        f"first seen  {render.format_ts(issue.first_seen)} ({render.format_ago(issue.first_seen)})\n"
    )
    body.append(
        f"last seen   {render.format_ts(issue.last_seen)} ({render.format_ago(issue.last_seen)})\n"
    )
    console.print(
        Panel(body, title=issue.id, border_style="red" if issue.status == "open" else "green")
    )

    incidents = engine.index.incidents_for_issue(issue.id, limit=1)
    if incidents:
        from slomo._core.frames import is_internal_file

        frames = [f for f in incidents[0].frames if not is_internal_file(str(f.get("file", "")))]
        if frames:
            tb = Text()
            for f in frames[-8:]:
                tb.append(
                    f"  {f.get('file')}:{f.get('line')} in {f.get('function')}\n", style="dim"
                )
                if f.get("code"):
                    tb.append(f"    {f.get('code')}\n")
            tb.append(f"{incidents[0].exc_type}: {incidents[0].message}", style="red")
            console.print(Panel(tb, title="latest traceback", border_style="dim"))

    from slomo.issues.similarity import similar_issues

    related = similar_issues(issue, engine.index)
    if related:
        console.print("[bold]possibly related:[/]")
        for other, score in related:
            console.print(f"  {other.id}  {other.title[:70]}  [dim]{score:.0%} similar[/]")
    console.print(f"[dim]replay it:  slomo replay {issue.id}    diagnose:  slomo doctor {issue.id}[/]")


@issue_app.command("occurrences")
def issue_occurrences(ref: str, limit: int = typer.Option(50, "--limit", "-n")) -> None:
    """Every recorded incident of this issue."""
    engine = _engine()
    issue = _get_issue_or_exit(engine, ref)
    incidents = engine.index.incidents_for_issue(issue.id, limit=limit)
    table = Table(header_style="bold")
    table.add_column("when")
    table.add_column("session", style="cyan")
    table.add_column("unhandled")
    table.add_column("message", overflow="ellipsis", max_width=60)
    for i in incidents:
        table.add_row(
            f"{render.format_ts(i.timestamp)} ({render.format_ago(i.timestamp)})",
            render.short_id(i.session_id),
            "[red]yes[/]" if i.unhandled else "[dim]no[/]",
            i.message,
        )
    console.print(table)
    console.print(f"[dim]{issue.occurrences} total occurrence(s)[/]")


@issue_app.command("timeline")
def issue_timeline(ref: str) -> None:
    """Timeline of the latest incident's session, focused on its trace."""
    engine = _engine()
    issue = _get_issue_or_exit(engine, ref)
    incidents = engine.index.incidents_for_issue(issue.id, limit=1)
    if not incidents:
        console.print("[dim]no incidents recorded[/]")
        return
    backend = _backend()
    events = [
        e
        for e in backend.iter_events(incidents[0].session_id)
        if e.trace_id == incidents[0].trace_id
    ]
    _print_timeline(events, f"issue {issue.id} — latest incident")


@issue_app.command("sessions")
def issue_sessions(ref: str) -> None:
    """Sessions affected by this issue."""
    engine = _engine()
    issue = _get_issue_or_exit(engine, ref)
    backend = _backend()
    session_ids = {i.session_id for i in engine.index.incidents_for_issue(issue.id, limit=500)}
    sessions = [m for m in backend.list_sessions() if m.id in session_ids]
    console.print(render.sessions_table(sessions))


@issue_app.command("resolve")
def issue_resolve(ref: str) -> None:
    """Mark an issue resolved (auto-reopens if it recurs)."""
    engine = _engine()
    issue = engine.resolve(ref)
    if issue is None:
        err_console.print(f"[red]no issue matches {ref!r}[/]")
        raise typer.Exit(1)
    console.print(f"[green]resolved[/] {issue.id} — will auto-reopen on regression")


@issue_app.command("reopen")
def issue_reopen(ref: str) -> None:
    """Reopen a resolved issue."""
    engine = _engine()
    issue = engine.reopen(ref)
    if issue is None:
        err_console.print(f"[red]no issue matches {ref!r}[/]")
        raise typer.Exit(1)
    console.print(f"[yellow]reopened[/] {issue.id}")


@issue_app.command("explain")
def issue_explain(ref: str) -> None:
    """One-paragraph explanation of the issue (doctor-lite)."""
    engine = _engine()
    issue = _get_issue_or_exit(engine, ref)
    from slomo.issues.doctor import diagnose

    d = diagnose(issue, engine.index, EventReader(_backend()))
    console.print(
        f"[bold]{issue.id}[/] is a [bold]{issue.category}[/] issue "
        f"({issue.confidence:.0%} confidence), seen [bold]{issue.occurrences}[/] time(s) "
        f"across {issue.affected_sessions} session(s), most recently {render.format_ago(issue.last_seen)}.\n\n"
        f"{d.likely_root_cause}\n\n"
        f"[bold]suggested fix:[/] {d.suggested_fix}"
    )


# ---------- doctor ----------


@app.command("doctor")
def doctor(ref: str = typer.Argument(help="Issue id, e.g. SM-1a2b3c4d.")) -> None:
    """Heuristic root-cause diagnosis for an issue."""
    engine = _engine()
    issue = _get_issue_or_exit(engine, ref)
    from slomo.issues.doctor import diagnose

    d = diagnose(issue, engine.index, EventReader(_backend()))

    def row(label: str, value, style: str = "white") -> None:
        console.print(f"[dim]{label:<22}[/][{style}]{value}[/]")
        console.print("[dim]" + "─" * 60 + "[/]")

    console.print(
        Panel(f"[bold red]{issue.title}[/]", title=f"doctor — {issue.id}", border_style="red")
    )
    row("Category", f"{issue.category}  ({issue.confidence:.0%} confidence)")
    row("Severity", issue.severity)
    row("Status", f"{issue.status} / {issue.stability}")
    row(
        "Occurrences",
        f"{issue.occurrences}  ({d.unhandled_count} unhandled) across {issue.affected_sessions} session(s)",
    )
    row(
        "First seen",
        f"{render.format_ts(issue.first_seen)}  ({render.format_ago(issue.first_seen)})",
    )
    row("Last seen", f"{render.format_ts(issue.last_seen)}  ({render.format_ago(issue.last_seen)})")
    row("Likely root cause", d.likely_root_cause, style="yellow")
    if d.first_bad_function:
        row("First bad function", f"{d.first_bad_function}()")
    if d.first_bad_variable:
        row("First bad variable", d.first_bad_variable)
    row("Suggested fix", d.suggested_fix, style="green")
    if d.correlated_events:
        console.print("[dim]Context just before the crash:[/]")
        for e in d.correlated_events:
            console.print(
                f"  [dim]{render.format_ts(e.timestamp, time_only=True)}[/] {e.type}  {render.event_summary(e)}"
            )
        console.print("[dim]" + "─" * 60 + "[/]")
    if d.related_issues:
        row("Related issues", ", ".join(f"{i.id} ({s:.0%})" for i, s in d.related_issues))
    console.print(f"[dim]replay the crash step by step:  slomo replay {issue.id}[/]")


# ---------- replay ----------


@app.command("replay")
def replay(
    ref: str | None = typer.Argument(
        None, help="Issue id (SM-xxxx) or session id; default latest session."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print events as JSON instead of interactive replay."
    ),
    trace: bool = typer.Option(
        False, "--trace", help="Print the whole timeline non-interactively."
    ),
) -> None:
    """Step through a recorded execution in the terminal."""
    from slomo.replay.player import ReplayState
    from slomo.replay.tui import ReplayTUI

    events, label = _events_for_ref(ref)
    if not events:
        err_console.print("[red]nothing to replay[/]")
        raise typer.Exit(1)
    if as_json:
        for e in events:
            sys.stdout.write(e.to_json_line())
        return
    state = ReplayState(events)
    first_error = state.first_error_index()
    if ref and ref.upper().startswith("SM-") and first_error is not None:
        state.jump(first_error)
    if trace or not sys.stdin.isatty():
        _print_timeline(events, label)
        return
    console.print(f"[dim]replaying {label}[/]")
    ReplayTUI(state, console=console).run()


# ---------- timeline / filtered views ----------


def _print_timeline(events: list[Event], title: str) -> None:
    console.print(f"[bold]timeline — {title}[/]")
    for i, e in enumerate(events):
        style = render.severity_style(e.severity)
        console.print(
            f"[dim]{i + 1:>5}  {render.format_ts(e.timestamp, time_only=True)}[/]  "
            f"[{style}]{str(e.type):<20}[/] {render.event_summary(e)}"
        )


@app.command("timeline")
def timeline(
    ref: str | None = typer.Argument(
        None, help="Session id, issue id, or empty for latest session."
    ),
    follow: bool = typer.Option(False, "--follow", "-f", help="Live-tail a running session."),
    errors_only: bool = typer.Option(False, "--errors", help="Only warnings and errors."),
) -> None:
    """Chronological event feed for a session or an issue's latest incident."""
    if follow:
        _timeline_follow(ref)
        return
    events, label = _events_for_ref(ref)
    if errors_only:
        events = [
            e for e in events if e.severity in (Severity.WARNING, Severity.ERROR, Severity.CRITICAL)
        ]
    _print_timeline(events, label)


def _timeline_follow(ref: str | None) -> None:
    backend = _backend()
    if ref is None:
        sessions = backend.list_sessions()
        if not sessions:
            err_console.print("[red]no sessions to follow[/]")
            raise typer.Exit(1)
        session_id = sessions[-1].id
    else:
        session_id = _resolve_session(backend, ref)
    console.print(f"[dim]following session {render.short_id(session_id)} — ctrl-c to stop[/]")
    offset = 0
    try:
        while True:
            for event, end in backend.iter_events_with_offset(session_id, from_offset=offset):
                style = render.severity_style(event.severity)
                console.print(
                    f"[dim]{render.format_ts(event.timestamp, time_only=True)}[/]  "
                    f"[{style}]{str(event.type):<20}[/] {render.event_summary(event)}"
                )
                offset = end
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def _filtered_view(ref: str | None, types: set[EventType], title: str) -> None:
    events, label = _events_for_ref(ref)
    picked = [e for e in events if e.type in types]
    if not picked:
        console.print(f"[dim]no {title} events in {label}[/]")
        return
    _print_timeline(picked, f"{title} — {label}")


@app.command("vars")
def vars_cmd(ref: str | None = typer.Argument(None)) -> None:
    """Variable snapshots (manual and exception-captured)."""
    events, label = _events_for_ref(ref)
    picked = [e for e in events if e.type == EventType.VARIABLE_SNAPSHOT]
    if not picked:
        console.print(f"[dim]no variable snapshots in {label}[/]")
        return
    for e in picked:
        title = e.payload.get("label") or e.payload.get("source", "snapshot")
        data = e.payload.get("variables") or e.payload.get("frames")
        console.print(
            Panel(
                JSON(json.dumps(data, default=str)),
                title=f"{title} — {render.format_ts(e.timestamp, time_only=True)}",
            )
        )


@app.command("http")
def http_cmd(ref: str | None = typer.Argument(None)) -> None:
    """HTTP requests/responses recorded in a session or issue."""
    _filtered_view(ref, {EventType.HTTP_REQUEST, EventType.HTTP_RESPONSE}, "http")


@app.command("sql")
def sql_cmd(ref: str | None = typer.Argument(None)) -> None:
    """SQL queries/results recorded in a session or issue."""
    _filtered_view(ref, {EventType.SQL_QUERY, EventType.SQL_RESULT}, "sql")


# ---------- search ----------


@app.command("search")
def search_cmd(
    terms: list[str] = typer.Argument(help="Free text and/or field=value filters."),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """Search everything: `slomo search timeout`, `slomo search module=checkout sql`."""
    from slomo.query.search import parse_query, search

    backend = _backend()
    engine = _engine(backend)
    hits = search(parse_query(terms), engine.index, EventReader(backend), limit=limit)
    if not hits:
        console.print("[dim]no matches[/]")
        return
    table = Table(header_style="bold")
    table.add_column("when")
    table.add_column("session", style="cyan")
    table.add_column("type")
    table.add_column("match", overflow="ellipsis", max_width=70)
    for h in hits:
        table.add_row(
            render.format_ts(h.timestamp),
            render.short_id(h.session_id),
            h.type,
            h.snippet,
        )
    console.print(table)


# ---------- stats ----------


@app.command("stats")
def stats(
    rebuild: bool = typer.Option(
        False, "--rebuild-index", help="Drop and rebuild the issue index from JSONL."
    ),
) -> None:
    """Totals: sessions, events, issues by category, storage."""
    backend = _backend()
    index = IssueIndex(paths.issues_dir(backend.root) / "index.sqlite")
    engine = IssueEngine(backend, index)
    engine.rebuild() if rebuild else engine.refresh()

    sessions = backend.list_sessions()
    total_events = sum(m.event_count for m in sessions)
    crashed = sum(1 for m in sessions if m.status == "crashed")
    issues = index.list_issues()
    open_issues = [i for i in issues if i.status == "open"]

    body = Text()
    body.append(f"sessions      {len(sessions)}  ({crashed} crashed)\n")
    body.append(f"events        {total_events}\n")
    body.append(f"issues        {len(issues)}  ({len(open_issues)} open)\n")
    size = sum(f.stat().st_size for f in backend.root.rglob("*") if f.is_file())
    body.append(f"storage       {size / 1024 / 1024:.1f} MB in {backend.root}\n")
    console.print(Panel(body, title="slomo stats", border_style="cyan"))

    if issues:
        by_cat: dict[str, int] = {}
        for i in issues:
            by_cat[i.category] = by_cat.get(i.category, 0) + i.occurrences
        table = Table(header_style="bold", box=None)
        table.add_column("category")
        table.add_column("incidents", justify="right")
        for cat, count in sorted(by_cat.items(), key=lambda kv: -kv[1]):
            table.add_row(cat, str(count))
        console.print(table)


# ---------- maintenance ----------


@app.command("prune")
def prune(
    keep: int | None = typer.Option(
        None, "--keep", "-k", help="Sessions to keep (default: config retention_max_sessions)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete the oldest sessions beyond the retention limit."""
    from slomo._core.config import load_config

    backend = _backend()
    limit = keep if keep is not None else load_config(backend.root).retention_max_sessions
    sessions = backend.list_sessions()
    excess = [m for m in sessions[:-limit]] if limit else []
    excess = [m for m in excess if m.status != "running"]
    if not excess:
        console.print(f"[dim]nothing to prune ({len(sessions)} session(s), limit {limit})[/]")
        return
    if not yes and not typer.confirm(f"delete {len(excess)} old session(s)?"):
        raise typer.Exit()
    for m in excess:
        backend.delete_session(m.id)
    console.print(f"[green]pruned[/] {len(excess)} session(s), kept {len(sessions) - len(excess)}")


# ---------- export ----------


@app.command("export")
def export_cmd(
    fmt: str = typer.Argument(help="json | markdown | csv | html"),
    ref: str | None = typer.Option(None, "--session", "-s", help="Session id (default: latest)."),
    issue_ref: str | None = typer.Option(None, "--issue", "-i", help="Issue id to export instead."),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Output file (default: .slomo/exports/)."
    ),
) -> None:
    """Export a session or issue to a shareable file."""
    from slomo.export import FORMATS, export_events

    if fmt not in FORMATS:
        err_console.print(f"[red]unknown format {fmt!r} — pick one of {', '.join(FORMATS)}[/]")
        raise typer.Exit(1)
    backend = _backend()
    issues = None
    if issue_ref:
        engine = _engine(backend)
        issue = _get_issue_or_exit(engine, issue_ref)
        issues = [issue]
        events, title = _events_for_ref(
            issue_ref if issue_ref.upper().startswith("SM-") else issue.id
        )
    else:
        events, title = _events_for_ref(ref)
    path = export_events(
        events,
        fmt,
        out.parent if out else paths.exports_dir(backend.root),
        title=title,
        issues=issues,
    )
    if out:
        path.rename(out)
        path = out
    console.print(f"[green]exported[/] {len(events)} events → {path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()

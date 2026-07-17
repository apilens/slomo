"""Terminal replay: a Rich-rendered command loop (no raw-keypress capture,
so it works everywhere and is testable by feeding stdin)."""

from __future__ import annotations

import json
from collections.abc import Callable

from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from slomo._core.events import Event, EventType, Severity
from slomo.cli.render import event_summary, format_ts, severity_style
from slomo.replay.player import ReplayState

HELP = """\
[bold]n[/] \\[N]   next event (or N forward)      [bold]p[/] \\[N]  previous
[bold]j[/] N     jump to event N                 [bold]t[/]      jump to next error
[bold]/[/]text   search forward                  [bold]?[/]text  search backward
[bold]i[/]       inspect full payload            [bold]v[/]      linked variable snapshot
[bold]w[/]       context window                  [bold]h[/]      help
[bold]q[/]       quit"""


class ReplayTUI:
    def __init__(
        self,
        state: ReplayState,
        console: Console | None = None,
        input_fn: Callable[[str], str] = input,
        snapshot_loader: Callable[[Event], dict | None] | None = None,
    ) -> None:
        self.state = state
        self.console = console or Console()
        self.input_fn = input_fn
        self.snapshot_loader = snapshot_loader

    def run(self) -> None:
        c = self.console
        c.print(
            Panel(
                f"[bold]slomo replay[/] — {len(self.state)} events. Type [bold]h[/] for help.",
                border_style="cyan",
            )
        )
        self._render_current()
        while True:
            try:
                raw = self.input_fn(self._prompt()).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not raw:
                raw = "n"
            cmd, _, arg = raw.partition(" ")
            if cmd in ("q", "quit", "exit"):
                break
            elif cmd in ("n", "next"):
                self.state.next(int(arg) if arg.isdigit() else 1)
                self._render_current()
            elif cmd in ("p", "prev"):
                self.state.prev(int(arg) if arg.isdigit() else 1)
                self._render_current()
            elif cmd in ("j", "jump") and arg.isdigit():
                self.state.jump(int(arg))
                self._render_current()
            elif cmd == "t":
                if self.state.next_error() is None:
                    c.print("[dim]no further errors[/]")
                self._render_current()
            elif cmd.startswith("/") or cmd == "search":
                needle = (cmd[1:] + " " + arg).strip() if cmd.startswith("/") else arg
                if self.state.search(needle) is None:
                    c.print(f"[dim]'{needle}' not found ahead[/]")
                self._render_current()
            elif cmd.startswith("?"):
                needle = (cmd[1:] + " " + arg).strip()
                if self.state.search(needle, direction=-1) is None:
                    c.print(f"[dim]'{needle}' not found behind[/]")
                self._render_current()
            elif cmd in ("i", "inspect"):
                c.print(JSON(json.dumps(self.state.inspect(), default=str)))
            elif cmd == "v":
                self._show_snapshot()
            elif cmd in ("w", "window"):
                self._render_window()
            elif cmd in ("h", "help"):
                c.print(Panel(HELP, title="commands", border_style="dim"))
            else:
                c.print("[dim]unknown command — h for help[/]")

    # ---------- rendering ----------

    def _prompt(self) -> str:
        e = self.state.current()
        return f"[{self.state.cursor + 1}/{len(self.state)} {e.type}] > "

    def _render_current(self) -> None:
        e = self.state.current()
        style = severity_style(e.severity)
        body = Text()
        body.append(f"{format_ts(e.timestamp)}  ", style="dim")
        body.append(f"{e.type}", style=style)
        body.append(f"  {event_summary(e)}")
        subtitle = f"trace {e.trace_id[:8]}" + (f" · span {e.span_id[:8]}" if e.span_id else "")
        self.console.print(
            Panel(
                body,
                title=f"event {self.state.cursor + 1}/{len(self.state)}",
                subtitle=subtitle,
                border_style=style,
            )
        )

    def _render_window(self) -> None:
        table = Table(box=None, pad_edge=False)
        table.add_column("#", justify="right", style="dim")
        table.add_column("time", style="dim")
        table.add_column("type")
        table.add_column("summary")
        for i, e in self.state.context_window(before=5, after=5):
            marker = "→" if i == self.state.cursor else " "
            table.add_row(
                f"{marker}{i + 1}",
                format_ts(e.timestamp, time_only=True),
                Text(str(e.type), style=severity_style(e.severity)),
                event_summary(e),
            )
        self.console.print(table)

    def _show_snapshot(self) -> None:
        e = self.state.current()
        snapshot = None
        if e.type == EventType.VARIABLE_SNAPSHOT:
            snapshot = e.payload
        elif self.snapshot_loader is not None:
            snapshot = self.snapshot_loader(e)
        if snapshot is None:
            # look ahead for a snapshot linked to this exception
            exc_id = e.payload.get("exception_id")
            if exc_id:
                for other in self.state.events:
                    if (
                        other.type == EventType.VARIABLE_SNAPSHOT
                        and other.payload.get("exception_id") == exc_id
                    ):
                        snapshot = other.payload
                        break
        if snapshot is None:
            self.console.print("[dim]no variable snapshot linked to this event[/]")
            return
        self.console.print(JSON(json.dumps(snapshot, default=str)))


__all__ = ["ReplayTUI", "ReplayState", "Severity"]

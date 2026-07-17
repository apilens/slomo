"""Interactive shell: bare `slomo` on a TTY. History + tab completion via
stdlib readline; every line dispatches back through the Typer app so the
shell and the CLI share one implementation."""

from __future__ import annotations

import shlex

from rich.console import Console

console = Console()

_COMMANDS = [
    "sessions",
    "session show",
    "session inspect",
    "session delete",
    "issues",
    "issue show",
    "issue timeline",
    "issue occurrences",
    "issue sessions",
    "issue resolve",
    "issue reopen",
    "issue explain",
    "replay",
    "doctor",
    "search",
    "timeline",
    "vars",
    "http",
    "sql",
    "stats",
    "export",
    "help",
    "exit",
]


def _completer_factory(candidates: list[str]):
    def complete(text: str, state: int):
        matches = [c for c in candidates if c.startswith(text)]
        return matches[state] if state < len(matches) else None

    return complete


def _issue_and_session_ids() -> list[str]:
    ids: list[str] = []
    try:
        from slomo.issues.index import IssueIndex
        from slomo.storage import paths
        from slomo.storage.jsonl import JsonlBackend

        root = paths.find_root()
        if root is None:
            return ids
        backend = JsonlBackend(root)
        ids.extend(m.id.replace("-", "")[:12] for m in backend.list_sessions())
        index_path = paths.issues_dir(root) / "index.sqlite"
        if index_path.exists():
            index = IssueIndex(index_path)
            ids.extend(i.id for i in index.list_issues())
            index.close()
    except Exception:
        pass
    return ids


def run_shell() -> None:
    from slomo import __version__
    from slomo.cli.app import app

    try:
        import readline

        candidates = sorted(
            set(_COMMANDS + [c.split()[0] for c in _COMMANDS]) | set(_issue_and_session_ids())
        )
        readline.set_completer(_completer_factory(candidates))
        readline.parse_and_bind("tab: complete")
        try:
            from slomo.storage import paths as _paths

            root = _paths.find_root()
            if root is not None:
                histfile = _paths.cache_dir(root) / "history"
                histfile.parent.mkdir(exist_ok=True)
                try:
                    readline.read_history_file(histfile)
                except OSError:
                    pass
                import atexit

                atexit.register(lambda: _safe_write_history(readline, histfile))
        except Exception:
            pass
    except ImportError:
        pass

    console.print(
        f"[bold cyan]slomo[/] interactive shell v{__version__} — type [bold]help[/] or [bold]exit[/]"
    )
    while True:
        try:
            line = input("slomo > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            continue
        if line in ("exit", "quit", "q"):
            break
        if line == "help":
            console.print("commands: " + ", ".join(sorted({c.split()[0] for c in _COMMANDS})))
            continue
        try:
            args = shlex.split(line)
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            continue
        try:
            app(args, standalone_mode=False)
        except SystemExit:
            pass
        except Exception as e:  # a failed command must not kill the shell
            console.print(f"[red]{type(e).__name__}: {e}[/]")


def _safe_write_history(readline_mod, histfile) -> None:
    try:
        readline_mod.write_history_file(histfile)
    except OSError:
        pass

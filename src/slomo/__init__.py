"""slomo — the black box flight recorder for Python applications.

    from slomo import enable
    enable()

Everything is recorded locally under ``.slomo/``; inspect it with the
``slomo`` CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from slomo._core.events import EventType, Severity
from slomo._core.recorder import get_recorder
from slomo.track import track

__version__ = "0.1.2"
__all__ = [
    "enable",
    "disable",
    "track",
    "snapshot",
    "event",
    "install_hooks",
    "flush",
    "__version__",
]


def enable(
    *,
    root: str | Path | None = None,
    labels: dict[str, str] | None = None,
    hooks: bool = True,
) -> None:
    """Start recording this process. Idempotent, zero-config."""
    get_recorder().enable(root=root, labels=labels, hooks=hooks)


def disable() -> None:
    """Stop recording and finalize the session."""
    get_recorder().disable()


def snapshot(label: str | None = None, /, **variables: Any) -> None:
    """Record an explicit variable snapshot: ``snapshot("before-retry", user=user)``."""
    rec = get_recorder()
    if not rec.active:
        return
    rec.emit(
        EventType.VARIABLE_SNAPSHOT,
        {
            "label": label,
            "source": "manual",
            "variables": rec.prepare_payload(variables),
        },
    )


def event(name: str, /, severity: str = "info", **payload: Any) -> None:
    """Record a custom event."""
    rec = get_recorder()
    if not rec.active:
        return
    try:
        sev = Severity(severity)
    except ValueError:
        sev = Severity.INFO
    rec.emit(EventType.CUSTOM, {"name": name, **rec.prepare_payload(payload)}, severity=sev)


def install_hooks() -> None:
    """Re-run hook installation (e.g. after importing requests/httpx late)."""
    rec = get_recorder()
    if rec.active and rec.config is not None:
        from slomo.hooks import install_all

        rec._hooks.extend(install_all(rec, rec.config))


def flush() -> None:
    """Block until all buffered events are on disk."""
    get_recorder().flush(fsync=True)

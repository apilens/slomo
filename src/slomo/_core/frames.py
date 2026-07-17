"""Structured traceback frames shared by exception hooks, @track, and the
issue engine (which never re-parses traceback text)."""

from __future__ import annotations

import os
import sys
import sysconfig
import traceback
from typing import Any

# the installed slomo package directory — matched by real path, never by the
# name "slomo" appearing in a path (a user's project dir may be called slomo)
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep
_STDLIB = sysconfig.get_paths().get("stdlib", "")
_SITE_MARKERS = ("site-packages", "dist-packages")


def extract_frames(exc: BaseException, limit: int = 50) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    try:
        for fs in traceback.extract_tb(exc.__traceback__, limit=limit):
            frames.append(
                {
                    "file": fs.filename,
                    "line": fs.lineno,
                    "function": fs.name,
                    "code": fs.line,
                }
            )
    except Exception:
        pass
    return frames


def is_internal_file(filename: str) -> bool:
    if not filename or filename.startswith("<"):
        return True
    return os.path.abspath(filename).startswith(_PKG_DIR)


def is_library_file(filename: str) -> bool:
    if filename.startswith("<"):
        return True
    if any(m in filename for m in _SITE_MARKERS):
        return True
    return bool(_STDLIB) and filename.startswith(_STDLIB)


def is_project_file(filename: str) -> bool:
    return not is_internal_file(filename) and not is_library_file(filename)


def exception_payload(exc: BaseException, *, unhandled: bool) -> dict[str, Any]:
    return {
        "exc_type": type(exc).__qualname__,
        "exc_module": type(exc).__module__,
        "message": _safe_str(exc),
        "frames": extract_frames(exc),
        "unhandled": unhandled,
        "python": sys.version.split()[0],
    }


def _safe_str(exc: BaseException) -> str:
    try:
        return str(exc)
    except BaseException:
        return f"<unprintable {type(exc).__qualname__}>"

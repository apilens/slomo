"""Structured traceback frames shared by exception hooks, @track, and the
issue engine (which never re-parses traceback text)."""

from __future__ import annotations

import sys
import sysconfig
import traceback
from typing import Any

_PKG_PREFIX = __name__.split(".")[0]  # "slomo"
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
    return f"{_PKG_PREFIX}/" in filename.replace("\\", "/") or filename.startswith("<")


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

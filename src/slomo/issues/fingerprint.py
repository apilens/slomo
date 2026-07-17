"""Exception fingerprinting: similar crashes must hash identically.

The fingerprint deliberately excludes line numbers (they churn across
edits) and normalizes volatile message parts (ids, hex, paths, quoted
strings) so tomorrow's occurrence of today's bug lands in the same Issue.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from slomo._core.frames import is_internal_file, is_library_file, is_project_file

FP_VERSION = 1

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_RE = re.compile(r"\b(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{6,}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_URL_RE = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.~ -]+){2,}")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_WS_RE = re.compile(r"\s+")


def normalize_message(msg: str) -> str:
    msg = _URL_RE.sub("<url>", msg)
    msg = _EMAIL_RE.sub("<email>", msg)
    msg = _UUID_RE.sub("<uuid>", msg)
    msg = _PATH_RE.sub("<path>", msg)
    msg = _HEX_RE.sub("<hex>", msg)
    msg = _QUOTED_RE.sub("<s>", msg)
    msg = _NUM_RE.sub("<n>", msg)
    return _WS_RE.sub(" ", msg).strip()[:300]


def normalize_frames(frames: list[dict[str, Any]]) -> list[str]:
    """['relpath:function', ...] — project frames keep their path (relative
    to cwd where possible); library frames collapse to the library name so
    dependency-internal churn doesn't split issues."""
    out: list[str] = []
    for f in frames:
        filename = str(f.get("file", ""))
        function = str(f.get("function", ""))
        if is_internal_file(filename):
            continue  # slomo's own wrappers are noise, not signal
        if is_library_file(filename):
            lib = _library_name(filename)
            entry = f"<lib:{lib}>"
            if out and out[-1] == entry:
                continue  # collapse consecutive library frames
        else:
            entry = f"{_relative(filename)}:{function}"
        out.append(entry)
    return out


def top_project_frame(frames: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The deepest in-project frame — the likely root-cause location."""
    for f in reversed(frames):
        if is_project_file(str(f.get("file", ""))):
            return f
    for f in reversed(frames):
        if not is_internal_file(str(f.get("file", ""))):
            return f
    return frames[-1] if frames else None


def fingerprint(exc_type: str, frames: list[dict[str, Any]], message: str) -> str:
    top = top_project_frame(frames)
    parts = [
        f"v{FP_VERSION}",
        exc_type,
        "\n".join(normalize_frames(frames)),
        f"{_relative(str(top.get('file', '')))}:{top.get('function', '')}" if top else "",
        normalize_message(message),
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8", errors="replace")).hexdigest()


def _relative(filename: str) -> str:
    import os

    try:
        cwd = os.getcwd()
        if filename.startswith(cwd):
            return filename[len(cwd) :].lstrip("/\\")
    except OSError:
        pass
    # fall back to the last three path components for stability across hosts
    parts = filename.replace("\\", "/").split("/")
    return "/".join(parts[-3:])


def _library_name(filename: str) -> str:
    norm = filename.replace("\\", "/")
    for marker in ("site-packages/", "dist-packages/"):
        if marker in norm:
            rest = norm.split(marker, 1)[1]
            return rest.split("/", 1)[0]
    if norm.startswith("<"):
        return norm.strip("<>")
    return "stdlib"

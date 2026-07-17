"""Discovery and creation of the .slomo data directory.

Both the recorder (writer) and the CLI (reader) resolve the same root by a
git-style walk-up from the working directory, so data recorded by one
process is readable by another. ``SLOMO_HOME`` overrides discovery.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR_NAME = ".slomo"


def find_root(start: Path | None = None) -> Path | None:
    env = os.environ.get("SLOMO_HOME")
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        root = candidate / ROOT_DIR_NAME
        if root.is_dir():
            return root
    return None


def ensure_root(start: Path | None = None) -> Path:
    """Find the root, or create .slomo/ (with defaults) at start/cwd."""
    root = find_root(start)
    if root is not None:
        return root
    env = os.environ.get("SLOMO_HOME")
    root = Path(env) if env else (start or Path.cwd()).resolve() / ROOT_DIR_NAME
    initialize_root(root)
    return root


def initialize_root(root: Path) -> None:
    from slomo._core.config import DEFAULT_CONFIG_TOML

    root.mkdir(parents=True, exist_ok=True)
    for sub in ("sessions", "issues", "cache", "exports", "plugins"):
        (root / sub).mkdir(exist_ok=True)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# recordings are local by design\n*\n", encoding="utf-8")
    config = root / "config.toml"
    if not config.exists():
        config.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")


def sessions_dir(root: Path) -> Path:
    return root / "sessions"


def issues_dir(root: Path) -> Path:
    return root / "issues"


def exports_dir(root: Path) -> Path:
    return root / "exports"


def cache_dir(root: Path) -> Path:
    return root / "cache"

"""Hook registry and lazy installation.

Library hooks (requests/httpx/sqlalchemy) only patch when the target library
is already imported by the host app; ``slomo.install_hooks()`` can be
called again after late imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slomo._core.config import Config
    from slomo._core.recorder import Recorder


def _build_hooks(config: Config) -> list:
    from slomo.hooks.exceptions import ExceptionHook

    hooks: list = [ExceptionHook()]
    if config.hooks.autotrace:
        from slomo.hooks.auto_trace import AutoTraceHook

        hooks.append(AutoTraceHook())
    if config.hooks.logging:
        from slomo.hooks.logging_hook import LoggingHook

        hooks.append(LoggingHook())
    if config.hooks.sql:
        from slomo.hooks.sql_sqlite3 import Sqlite3Hook

        hooks.append(Sqlite3Hook())
        from slomo.hooks.sql_sqlalchemy import SqlalchemyHook

        hooks.append(SqlalchemyHook())
    if config.hooks.http:
        from slomo.hooks.http_requests import RequestsHook

        hooks.append(RequestsHook())
        from slomo.hooks.http_httpx import HttpxHook

        hooks.append(HttpxHook())
    return hooks


def install_all(recorder: Recorder, config: Config) -> list:
    already = {h.name for h in getattr(recorder, "_hooks", [])}
    installed = []
    for hook in _build_hooks(config):
        if hook.name in already:
            continue
        try:
            if hook.available():
                hook.install(recorder, config)
                installed.append(hook)
        except Exception:
            continue  # a hook that cannot install is skipped, never fatal
    return installed

"""Forwards WARNING+ log records (configurable) into the timeline."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from slomo._core.events import EventType, Severity

if TYPE_CHECKING:
    from slomo._core.config import Config
    from slomo._core.recorder import Recorder

_LEVEL_MAP = {
    logging.DEBUG: (EventType.LOG, Severity.DEBUG),
    logging.INFO: (EventType.LOG, Severity.INFO),
    logging.WARNING: (EventType.WARNING, Severity.WARNING),
    logging.ERROR: (EventType.ERROR, Severity.ERROR),
    logging.CRITICAL: (EventType.ERROR, Severity.CRITICAL),
}


class _ForwardingHandler(logging.Handler):
    def __init__(self, recorder: Recorder, level: int) -> None:
        super().__init__(level=level)
        self._recorder = recorder

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("slomo"):
                return
            rec = self._recorder
            if not rec.active:
                return
            etype, sev = _LEVEL_MAP.get(
                min(logging.CRITICAL, max(logging.DEBUG, record.levelno // 10 * 10)),
                (EventType.LOG, Severity.INFO),
            )
            payload = {
                "logger": record.name,
                "level": record.levelname,
                "message": rec.redactor.redact(record.getMessage()),
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            }
            if record.exc_info and record.exc_info[1] is not None:
                rec.record_exception(record.exc_info[1])
            rec.emit(etype, payload, severity=sev)
        except Exception:
            pass


class LoggingHook:
    name = "logging"

    def __init__(self) -> None:
        self._handler: _ForwardingHandler | None = None

    def available(self) -> bool:
        return True

    def install(self, recorder: Recorder, config: Config) -> None:
        if self._handler is not None:
            return
        level = getattr(logging, config.hooks.logging_level, logging.WARNING)
        self._handler = _ForwardingHandler(recorder, level)
        logging.getLogger().addHandler(self._handler)

    def uninstall(self) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None

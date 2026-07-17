"""Global exception capture: sys.excepthook, threading.excepthook,
sys.unraisablehook. Always chains to the previously-installed hook and
fsync-flushes before the process dies."""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slomo._core.config import Config
    from slomo._core.recorder import Recorder


class ExceptionHook:
    name = "exceptions"

    def __init__(self) -> None:
        self._recorder: Recorder | None = None
        self._prev_excepthook = None
        self._prev_threading_hook = None
        self._prev_unraisable = None
        self._installed = False

    def available(self) -> bool:
        return True

    def install(self, recorder: Recorder, config: Config) -> None:
        if self._installed:
            return
        self._recorder = recorder
        self._prev_excepthook = sys.excepthook
        sys.excepthook = self._excepthook
        self._prev_threading_hook = threading.excepthook
        threading.excepthook = self._threading_hook
        if config.hooks.unraisable:
            self._prev_unraisable = sys.unraisablehook
            sys.unraisablehook = self._unraisable_hook
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        if sys.excepthook == self._excepthook and self._prev_excepthook is not None:
            sys.excepthook = self._prev_excepthook
        if threading.excepthook == self._threading_hook and self._prev_threading_hook is not None:
            threading.excepthook = self._prev_threading_hook
        if self._prev_unraisable is not None and sys.unraisablehook == self._unraisable_hook:
            sys.unraisablehook = self._prev_unraisable
        self._installed = False

    # ---------- hooks ----------

    def _excepthook(self, exc_type, exc, tb) -> None:
        try:
            rec = self._recorder
            if rec is not None and rec.active and exc is not None:
                rec.record_exception(exc, unhandled=True)
                rec.mark_crashed()
        except Exception:
            pass
        prev = self._prev_excepthook or sys.__excepthook__
        prev(exc_type, exc, tb)

    def _threading_hook(self, args) -> None:
        try:
            rec = self._recorder
            if rec is not None and rec.active and args.exc_value is not None:
                rec.record_exception(args.exc_value, unhandled=True)
                rec.flush(fsync=True)
        except Exception:
            pass
        prev = self._prev_threading_hook or threading.__excepthook__
        prev(args)

    def _unraisable_hook(self, args) -> None:
        try:
            rec = self._recorder
            if rec is not None and rec.active and args.exc_value is not None:
                rec.record_exception(args.exc_value, unhandled=False)
        except Exception:
            pass
        prev = self._prev_unraisable or sys.__unraisablehook__
        prev(args)

"""Automatic function tracing via ``sys.monitoring`` (PEP 669, Python 3.12+).

One ``enable()`` instruments the whole process: every call into *your* code
is recorded as a span — enter, arguments, exit, result, duration, and the
exception if one escapes — with no decorators required. "Your code" means
files under the project root (the directory containing ``.slomo/``),
plus anything matched by ``[hooks.autotrace] include`` globs.

Everything else — stdlib, site-packages, slomo itself — is filtered
out, and filtered call sites are switched off inside the interpreter
(``sys.monitoring.DISABLE``) after their first hit, so the steady-state
cost outside project code is zero.

Functions wrapped with ``@track`` are skipped here (the decorator records
them with richer control), so nothing is captured twice.
"""

from __future__ import annotations

import fnmatch
import os
import sys
import threading
import time
import weakref
from typing import TYPE_CHECKING, Any

from slomo._core import context
from slomo._core.events import EventType, Severity
from slomo._core.ids import new_span_id
from slomo._core.tracked import is_tracked

if TYPE_CHECKING:
    from slomo._core.config import Config
    from slomo._core.recorder import Recorder

_CO_OPTIMIZED = 0x01  # set on real functions; absent on module/class bodies

_EVENT_NAMES = ("PY_START", "PY_RETURN", "PY_YIELD", "PY_RESUME", "PY_THROW", "PY_UNWIND")


class _Span:
    __slots__ = ("span_id", "parent", "token", "t0", "function", "module", "code")

    def __init__(self, span_id, parent, token, t0, function, module, code) -> None:
        self.span_id = span_id
        self.parent = parent
        self.token = token
        self.t0 = t0
        self.function = function
        self.module = module
        self.code = code


class AutoTraceHook:
    name = "autotrace"

    def __init__(self) -> None:
        self._recorder: Recorder | None = None
        self._tool_id: int | None = None
        self._installed = False
        self._capture_args = True
        self._capture_results = True
        self._include: list[str] = []
        self._exclude: list[str] = []
        self._project_root = ""
        self._pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep
        self._tl = threading.local()
        # id(frame) -> _Span. Frames aren't weakref-able; entries are removed
        # on PY_RETURN/PY_UNWIND, and abandoned generators still unwind with
        # GeneratorExit when collected, so the map self-cleans. Each entry
        # keeps its code object and is validated on lookup, so a recycled
        # frame id can never be paired with a stale span.
        self._frames: dict[int, _Span] = {}
        self._frames_lock = threading.Lock()
        # id(code) -> (weakref to code, decision). Identity-keyed on purpose:
        # code objects compare equal by value ignoring filename, so an
        # equality-keyed mapping could hand one file's decision to another.
        self._decisions: dict[int, tuple[weakref.ref, bool]] = {}

    # ---------- hook protocol ----------

    def available(self) -> bool:
        return hasattr(sys, "monitoring")

    def install(self, recorder: Recorder, config: Config) -> None:
        if self._installed:
            return
        mon = sys.monitoring
        tool_id = None
        for candidate in (3, 4):  # slots not reserved for debugger/coverage/profiler
            try:
                mon.use_tool_id(candidate, "slomo")
                tool_id = candidate
                break
            except ValueError:
                continue
        if tool_id is None:
            raise RuntimeError("no free sys.monitoring tool id")

        self._recorder = recorder
        self._tool_id = tool_id
        self._capture_args = config.hooks.autotrace_capture_args
        self._capture_results = config.hooks.autotrace_capture_results
        self._include = list(config.hooks.autotrace_include)
        self._exclude = list(config.hooks.autotrace_exclude)
        self._project_root = str(config.root.resolve().parent)

        E = mon.events
        mon.register_callback(tool_id, E.PY_START, self._py_start)
        mon.register_callback(tool_id, E.PY_RETURN, self._py_return)
        mon.register_callback(tool_id, E.PY_YIELD, self._py_yield)
        mon.register_callback(tool_id, E.PY_RESUME, self._py_resume)
        mon.register_callback(tool_id, E.PY_THROW, self._py_throw)
        mon.register_callback(tool_id, E.PY_UNWIND, self._py_unwind)
        # a previous session may have DISABLEd locations that are now included
        mon.restart_events()
        mon.set_events(
            tool_id,
            E.PY_START | E.PY_RETURN | E.PY_YIELD | E.PY_RESUME | E.PY_THROW | E.PY_UNWIND,
        )
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        mon = sys.monitoring
        try:
            mon.set_events(self._tool_id, 0)
            for name in _EVENT_NAMES:
                mon.register_callback(self._tool_id, getattr(mon.events, name), None)
            mon.free_tool_id(self._tool_id)
        except Exception:
            pass
        with self._frames_lock:
            self._frames.clear()
        self._decisions.clear()
        self._tool_id = None
        self._installed = False

    # ---------- filtering ----------

    def _decide(self, code) -> bool:
        if not (code.co_flags & _CO_OPTIMIZED):  # module and class bodies
            return False
        if code.co_name.startswith("<"):  # lambdas, genexprs
            return False
        filename = code.co_filename
        if not filename or filename.startswith("<"):
            return False
        path = os.path.abspath(filename)
        if path.startswith(self._pkg_dir):
            return False
        parts = path.split(os.sep)
        if "site-packages" in parts or "dist-packages" in parts:
            return False
        for pattern in self._exclude:
            if fnmatch.fnmatch(path, pattern):
                return False
        if path.startswith(self._project_root + os.sep):
            return True
        return any(fnmatch.fnmatch(path, pattern) for pattern in self._include)

    def _should_trace(self, code) -> bool:
        entry = self._decisions.get(id(code))
        if entry is not None and entry[0]() is code:
            decision = entry[1]
        else:
            decision = self._decide(code)
            try:
                self._decisions[id(code)] = (weakref.ref(code), decision)
            except TypeError:
                pass
        return decision and not is_tracked(code)

    # ---------- frame map ----------

    def _pop_span(self, frame) -> _Span | None:
        with self._frames_lock:
            span = self._frames.pop(id(frame), None)
        if span is not None and span.code is not frame.f_code:
            return None  # frame id recycled; stale entry already discarded
        return span

    def _get_span(self, frame) -> _Span | None:
        with self._frames_lock:
            span = self._frames.get(id(frame))
        if span is not None and span.code is not frame.f_code:
            return None
        return span

    # ---------- sys.monitoring callbacks ----------
    #
    # Reentrancy: serializing args/results may run user __repr__ code, whose
    # own PY_START/PY_RETURN would fire mid-callback. The thread-local flag
    # makes those inner events no-ops (return None, never DISABLE, so the
    # location keeps working for normal calls).

    def _py_start(self, code, offset) -> Any:
        if getattr(self._tl, "busy", False):
            return None
        rec = self._recorder
        if rec is None or not rec.active:
            return None
        if not self._should_trace(code):
            return sys.monitoring.DISABLE
        self._tl.busy = True
        try:
            frame = sys._getframe(1)
            span_id = new_span_id()
            parent = context.current_span_id()
            token = context.push_span(span_id)
            module = frame.f_globals.get("__name__", "")
            span = _Span(
                span_id, parent, token, time.perf_counter_ns(), code.co_qualname, module, code
            )
            with self._frames_lock:
                self._frames[id(frame)] = span
            payload: dict[str, Any] = {
                "function": code.co_qualname,
                "module": module,
                "file": code.co_filename,
                "line": code.co_firstlineno,
                "source": "auto",
            }
            if self._capture_args:
                args = frame.f_locals  # at PY_START, locals == the arguments
                if args:
                    payload["kwargs"] = rec.prepare_payload(dict(args))
            rec.emit(EventType.FUNCTION_ENTER, payload, span_id=span_id, parent_span_id=parent)
        except Exception:
            pass
        finally:
            self._tl.busy = False
        return None

    def _py_return(self, code, offset, retval) -> Any:
        if getattr(self._tl, "busy", False):
            return None
        rec = self._recorder
        if rec is None or not rec.active:
            return None
        span = self._pop_span(sys._getframe(1))
        if span is None:
            return None if self._should_trace(code) else sys.monitoring.DISABLE
        self._tl.busy = True
        try:
            self._restore_context(span)
            payload: dict[str, Any] = {
                "function": span.function,
                "module": span.module,
                "duration_ns": time.perf_counter_ns() - span.t0,
                "source": "auto",
            }
            if self._capture_results and retval is not None:
                payload["result"] = rec.prepare_payload(retval)
            rec.emit(
                EventType.FUNCTION_EXIT, payload, span_id=span.span_id, parent_span_id=span.parent
            )
        except Exception:
            pass
        finally:
            self._tl.busy = False
        return None

    def _py_yield(self, code, offset, retval) -> Any:
        """Generator/coroutine suspends: restore the caller's span context."""
        if getattr(self._tl, "busy", False):
            return None
        span = self._get_span(sys._getframe(1))
        if span is None:
            return None if self._should_trace(code) else sys.monitoring.DISABLE
        self._restore_context(span)
        span.token = None
        return None

    def _py_resume(self, code, offset) -> Any:
        """Generator/coroutine resumes: its span becomes current again."""
        if getattr(self._tl, "busy", False):
            return None
        span = self._get_span(sys._getframe(1))
        if span is None:
            return None if self._should_trace(code) else sys.monitoring.DISABLE
        span.token = context.push_span(span.span_id)
        return None

    def _py_throw(self, code, offset, exception) -> None:
        # resumes the frame with an exception (gen.throw / task cancellation)
        if getattr(self._tl, "busy", False):
            return
        span = self._get_span(sys._getframe(1))
        if span is not None:
            span.token = context.push_span(span.span_id)

    def _py_unwind(self, code, offset, exception) -> None:
        """An exception is escaping the frame."""
        if getattr(self._tl, "busy", False):
            return
        rec = self._recorder
        if rec is None or not rec.active:
            return
        span = self._pop_span(sys._getframe(1))
        if span is None:
            return
        self._tl.busy = True
        try:
            self._restore_context(span)
            if isinstance(exception, GeneratorExit):
                # a discarded generator being closed is a normal exit
                rec.emit(
                    EventType.FUNCTION_EXIT,
                    {
                        "function": span.function,
                        "module": span.module,
                        "duration_ns": time.perf_counter_ns() - span.t0,
                        "source": "auto",
                    },
                    span_id=span.span_id,
                    parent_span_id=span.parent,
                )
                return
            rec.record_exception(
                exception,
                span_id=span.span_id,
                parent_span_id=span.parent,
                function=span.function,
            )
            rec.emit(
                EventType.FUNCTION_EXIT,
                {
                    "function": span.function,
                    "module": span.module,
                    "duration_ns": time.perf_counter_ns() - span.t0,
                    "outcome": "exception",
                    "exc_type": type(exception).__qualname__,
                    "source": "auto",
                },
                severity=Severity.ERROR,
                span_id=span.span_id,
                parent_span_id=span.parent,
            )
        except Exception:
            pass
        finally:
            self._tl.busy = False

    def _restore_context(self, span: _Span) -> None:
        if span.token is not None:
            if not context.pop_span(span.token):
                # token from another context (rare: frame finalized elsewhere)
                context.push_span(span.parent)
            span.token = None

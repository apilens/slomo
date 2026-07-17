"""End-to-end: real subprocesses, hard kills, fork, import hygiene, perf."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap

import pytest

from slomo.storage.jsonl import JsonlBackend


def _run_script(code: str, root, check: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ, SLOMO_HOME=str(root), PYTHONPATH="")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=check,
    )


class TestSubprocess:
    def test_unhandled_crash_recorded(self, storage_root):
        result = _run_script(
            """
            import slomo
            slomo.enable()
            raise RuntimeError("kaboom in production")
            """,
            storage_root,
        )
        assert result.returncode == 1
        assert "kaboom in production" in result.stderr  # traceback still printed
        backend = JsonlBackend(storage_root)
        sessions = backend.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].status == "crashed"
        events = list(backend.iter_events(sessions[0].id))
        error = next(e for e in events if str(e.type) == "error")
        assert error.payload["exc_type"] == "RuntimeError"
        assert error.payload["unhandled"] is True

    def test_prior_excepthook_still_chained(self, storage_root):
        result = _run_script(
            """
            import sys, slomo
            def custom_hook(t, v, tb):
                print("CUSTOM HOOK RAN", file=sys.stderr)
            sys.excepthook = custom_hook
            slomo.enable()
            raise ValueError("x")
            """,
            storage_root,
        )
        assert "CUSTOM HOOK RAN" in result.stderr

    def test_thread_exception_recorded(self, storage_root):
        _run_script(
            """
            import threading, slomo
            slomo.enable()
            t = threading.Thread(target=lambda: 1 / 0)
            t.start(); t.join()
            """,
            storage_root,
        )
        backend = JsonlBackend(storage_root)
        events = list(backend.iter_events(backend.list_sessions()[0].id))
        assert any(
            e.payload.get("exc_type") == "ZeroDivisionError" and e.payload.get("unhandled")
            for e in events
        )

    @pytest.mark.slow
    def test_sigkill_leaves_parseable_prefix(self, storage_root):
        env = dict(os.environ, SLOMO_HOME=str(storage_root), PYTHONPATH="")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    """
                    import sys, time, slomo
                    slomo.enable()
                    for i in range(100_000):
                        slomo.event("tick", i=i)
                        if i == 5000:
                            slomo.flush()  # first 5001 ticks guaranteed on disk
                            print("ready", flush=True)
                    time.sleep(30)
                    """
                ),
            ],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout.readline().strip() == "ready"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

        backend = JsonlBackend(storage_root)
        sessions = backend.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].status == "abandoned"  # pid dead, never finalized
        events = list(backend.iter_events(sessions[0].id))  # must not raise
        ticks = [e for e in events if e.payload.get("name") == "tick"]
        # everything flushed before "ready" must survive the kill; the exact
        # tail beyond it is timing-dependent (deliberately not asserted)
        assert len(ticks) > 5000

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX only")
    def test_fork_gets_own_session(self, storage_root):
        _run_script(
            """
            import os, slomo
            slomo.enable()
            slomo.event("parent.before_fork")
            pid = os.fork()
            if pid == 0:
                slomo.event("child.event")
                slomo.flush()  # os._exit skips atexit, so flush explicitly
                os._exit(0)
            os.waitpid(pid, 0)
            slomo.event("parent.after_fork")
            """,
            storage_root,
        )
        backend = JsonlBackend(storage_root)
        sessions = backend.list_sessions()
        assert len(sessions) == 2
        forked = [m for m in sessions if "forked_from" in m.labels]
        assert len(forked) == 1
        child_events = list(backend.iter_events(forked[0].id))
        assert any(e.payload.get("name") == "child.event" for e in child_events)
        parent = next(m for m in sessions if "forked_from" not in m.labels)
        parent_events = list(backend.iter_events(parent.id))
        names = [e.payload.get("name") for e in parent_events]
        assert "parent.before_fork" in names and "parent.after_fork" in names
        assert "child.event" not in names  # no cross-process interleaving


class TestBudgets:
    def test_import_hygiene_no_cli_stack_in_sdk_path(self, storage_root):
        result = _run_script(
            """
            import sys, json
            import slomo
            slomo.enable()
            print(json.dumps({
                "typer": "typer" in sys.modules,
                "rich": "rich" in sys.modules,
                "click": "click" in sys.modules,
            }))
            """,
            storage_root,
            check=True,
        )
        loaded = json.loads(result.stdout)
        assert loaded == {"typer": False, "rich": False, "click": False}

    @pytest.mark.slow
    def test_enable_under_5ms(self, storage_root):
        result = _run_script(
            """
            import time, statistics, slomo
            from slomo._core.recorder import Recorder
            samples = []
            for _ in range(20):
                Recorder._reset_for_tests()
                t0 = time.perf_counter()
                slomo.enable(hooks=True)
                samples.append(time.perf_counter() - t0)
            print(statistics.median(samples) * 1000)
            """,
            storage_root,
            check=True,
        )
        median_ms = float(result.stdout.strip())
        assert median_ms < 5.0, f"enable() median {median_ms:.2f}ms exceeds 5ms budget"

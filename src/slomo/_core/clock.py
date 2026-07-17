"""Wall-clock anchored, monotonic-advancing nanosecond clock.

Anchoring to the wall clock gives real timestamps for cross-session
correlation; advancing by ``perf_counter_ns`` deltas makes the clock immune
to NTP jumps within a session. ``now_ns`` never goes backwards.
"""

from __future__ import annotations

import threading
import time


class HybridClock:
    __slots__ = ("_anchor_wall", "_anchor_perf", "_last", "_lock")

    def __init__(self) -> None:
        self._anchor_wall = time.time_ns()
        self._anchor_perf = time.perf_counter_ns()
        self._last = 0
        self._lock = threading.Lock()

    def now_ns(self) -> int:
        now = self._anchor_wall + (time.perf_counter_ns() - self._anchor_perf)
        with self._lock:
            if now <= self._last:
                now = self._last + 1
            self._last = now
            return now

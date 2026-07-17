"""Time-ordered id generation (RFC 9562 UUIDv7) and span ids.

Python 3.12 has no ``uuid.uuid7``; this is a self-contained implementation.
Ids generated within the same millisecond stay monotonic via a 12-bit
counter in the rand_a field (RFC 9562 §6.2, method 3).
"""

from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7() -> str:
    """Return a lowercase hex UUIDv7 string (time-ordered, sortable)."""
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            ms = _last_ms
            _counter += 1
            if _counter > 0x0FFF:  # counter overflow: borrow a millisecond
                ms += 1
                _counter = 0
        else:
            _counter = int.from_bytes(os.urandom(2)) & 0x07FF  # random start, headroom to count up
        _last_ms = ms
        counter = _counter

    rand_b = int.from_bytes(os.urandom(8)) & 0x3FFF_FFFF_FFFF_FFFF
    value = (
        (ms & 0xFFFF_FFFF_FFFF) << 80
        | 0x7 << 76  # version 7
        | (counter & 0x0FFF) << 64
        | 0b10 << 62  # variant
        | rand_b
    )
    hex_ = f"{value:032x}"
    return f"{hex_[0:8]}-{hex_[8:12]}-{hex_[12:16]}-{hex_[16:20]}-{hex_[20:32]}"


def new_trace_id() -> str:
    return uuid7()


def new_span_id() -> str:
    """16-hex-char span id (OpenTelemetry-sized)."""
    return os.urandom(8).hex()

"""Registry of code objects wrapped by ``@track``.

The auto-trace hook skips these so a tracked function is recorded once
(by its decorator, which captures more) rather than twice.

Keyed by identity, not equality: code objects compare equal by value
(ignoring filename), so an equality-based set would silently drop a new
registration whenever an equal code object from an unloaded module was
still present — and lose both when the old one was collected.
"""

from __future__ import annotations

import weakref

_BY_ID: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def register(code) -> None:
    try:
        _BY_ID[id(code)] = code
    except TypeError:
        pass


def is_tracked(code) -> bool:
    return _BY_ID.get(id(code)) is code

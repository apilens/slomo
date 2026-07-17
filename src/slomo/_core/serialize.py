"""Safe, bounded conversion of arbitrary objects to JSON-serializable trees.

Capture is best-effort by design: a value that cannot be represented must
never raise into the host application.
"""

from __future__ import annotations

import dataclasses
from typing import Any

TRUNCATED = "__truncated__"


def safe_repr(obj: Any, max_len: int = 2048) -> str:
    try:
        text = repr(obj)
    except BaseException:
        try:
            text = object.__repr__(obj)
        except BaseException:
            text = "<unrepresentable>"
    if len(text) > max_len:
        text = text[:max_len] + f"…(+{len(text) - max_len} chars)"
    return text


def to_jsonable(
    obj: Any,
    *,
    max_depth: int = 4,
    max_items: int = 25,
    max_str: int = 2048,
    _depth: int = 0,
    _memo: set[int] | None = None,
) -> Any:
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        if len(obj) > max_str:
            return obj[:max_str] + f"…(+{len(obj) - max_str} chars)"
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes__": len(obj), "__preview__": safe_repr(bytes(obj[:64]), max_str)}

    if _depth >= max_depth:
        return {"__repr__": safe_repr(obj, max_str), "__type__": type(obj).__qualname__}

    if _memo is None:
        _memo = set()
    oid = id(obj)
    if oid in _memo:
        return {"__cycle__": type(obj).__qualname__}

    kwargs: dict[str, Any] = dict(max_depth=max_depth, max_items=max_items, max_str=max_str)

    if isinstance(obj, dict):
        _memo.add(oid)
        try:
            out: dict[str, Any] = {}
            for i, (k, v) in enumerate(obj.items()):
                if i >= max_items:
                    out[TRUNCATED] = len(obj) - max_items
                    break
                key = k if isinstance(k, str) else safe_repr(k, 128)
                out[key] = to_jsonable(v, _depth=_depth + 1, _memo=_memo, **kwargs)
            return out
        finally:
            _memo.discard(oid)

    if isinstance(obj, (list, tuple, set, frozenset)):
        _memo.add(oid)
        try:
            items: list[Any] = []
            for i, v in enumerate(obj):
                if i >= max_items:
                    items.append({TRUNCATED: len(obj) - max_items})
                    break
                items.append(to_jsonable(v, _depth=_depth + 1, _memo=_memo, **kwargs))
            return items
        finally:
            _memo.discard(oid)

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        _memo.add(oid)
        try:
            out = {"__type__": type(obj).__qualname__}
            for i, f in enumerate(dataclasses.fields(obj)):
                if i >= max_items:
                    out[TRUNCATED] = len(dataclasses.fields(obj)) - max_items
                    break
                try:
                    out[f.name] = to_jsonable(
                        getattr(obj, f.name), _depth=_depth + 1, _memo=_memo, **kwargs
                    )
                except BaseException:
                    out[f.name] = "<unreadable>"
            return out
        finally:
            _memo.discard(oid)

    return {"__repr__": safe_repr(obj, max_str), "__type__": type(obj).__qualname__}

"""Variable snapshots: locals captured from traceback frames on exception,
plus the explicit ``slomo.snapshot(**vars)`` API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from slomo._core.frames import is_internal_file, is_project_file

if TYPE_CHECKING:
    from slomo._core.recorder import Recorder


def capture_exception_locals(exc: BaseException, recorder: Recorder) -> list[dict[str, Any]]:
    """Redacted, size-capped locals of the deepest in-project frames."""
    config = recorder.config
    max_frames = config.hooks.snapshot_frames if config else 5
    frames: list[dict[str, Any]] = []
    try:
        tb = exc.__traceback__
        raw_frames = []
        while tb is not None:
            raw_frames.append(tb.tb_frame)
            tb = tb.tb_next
        picked = [f for f in raw_frames if is_project_file(f.f_code.co_filename)]
        if not picked:
            picked = [f for f in raw_frames if not is_internal_file(f.f_code.co_filename)]
        for frame in picked[-max_frames:]:
            try:
                local_vars = dict(frame.f_locals)
            except Exception:
                continue
            local_vars.pop("__builtins__", None)
            frames.append(
                {
                    "file": frame.f_code.co_filename,
                    "function": frame.f_code.co_qualname,
                    "line": frame.f_lineno,
                    "locals": recorder.prepare_payload(local_vars),
                }
            )
    except Exception:
        pass
    return frames

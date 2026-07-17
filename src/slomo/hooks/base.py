"""Hook protocol. A broken hook must never break the host application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from slomo._core.config import Config
    from slomo._core.recorder import Recorder

PATCH_SENTINEL = "_slomo_patched"


@runtime_checkable
class Hook(Protocol):
    name: str

    def available(self) -> bool: ...
    def install(self, recorder: Recorder, config: Config) -> None: ...
    def uninstall(self) -> None: ...

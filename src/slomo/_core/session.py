"""Session metadata."""

from __future__ import annotations

import getpass
import os
import platform
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from slomo._core.ids import uuid7

SessionStatus = Literal["running", "finished", "crashed", "abandoned"]


@dataclass(slots=True)
class SessionMeta:
    id: str
    started_at: int
    finished_at: int | None
    status: SessionStatus
    pid: int
    argv: list[str]
    cwd: str
    python: str
    platform: str
    hostname: str
    user: str
    entrypoint: str
    event_count: int = 0
    error_count: int = 0
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def dir_name(self) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started_at / 1e9))
        return f"{stamp}-{self.id.replace('-', '')[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionMeta:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def create(cls, started_at: int, labels: dict[str, str] | None = None) -> SessionMeta:
        argv = list(sys.argv)
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
        return cls(
            id=uuid7(),
            started_at=started_at,
            finished_at=None,
            status="running",
            pid=os.getpid(),
            argv=argv,
            cwd=os.getcwd(),
            python=platform.python_version(),
            platform=platform.platform(terse=True),
            hostname=socket.gethostname(),
            user=user,
            entrypoint=argv[0] if argv else "",
            labels=dict(labels or {}),
        )

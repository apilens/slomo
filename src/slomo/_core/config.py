"""Configuration: .slomo/config.toml overlaid with SLOMO_* env vars."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class HookConfig:
    http: bool = True
    sql: bool = True
    logging: bool = True
    logging_level: str = "WARNING"
    snapshots: bool = True
    snapshot_frames: int = 5
    sql_capture_params: bool = False
    unraisable: bool = True
    autotrace: bool = True
    autotrace_capture_args: bool = True
    autotrace_capture_results: bool = True
    autotrace_include: list[str] = field(default_factory=list)
    autotrace_exclude: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Config:
    root: Path
    enabled: bool = True
    max_value_repr: int = 2048
    max_payload_bytes: int = 65536
    max_collection_items: int = 25
    max_depth: int = 4
    flush_interval_s: float = 0.5
    queue_max: int = 10_000
    retention_max_sessions: int = 200
    hooks: HookConfig = field(default_factory=HookConfig)
    redact_keys: list[str] = field(default_factory=list)
    redact_patterns: list[str] = field(default_factory=list)
    redact_defaults: bool = True


DEFAULT_CONFIG_TOML = """\
# slomo configuration — https://github.com/bhatt-neel-dev/slomo
[recording]
enabled = true
max_value_repr = 2048
max_payload_bytes = 65536
max_collection_items = 25
flush_interval_s = 0.5

[storage]
retention_max_sessions = 200

[redaction]
# extra_keys = ["internal_id"]
# extra_patterns = ["MYCO-[0-9]+"]
# defaults = true

[hooks.http]
enabled = true

[hooks.sql]
enabled = true
capture_params = false

[hooks.logging]
enabled = true
level = "WARNING"

[hooks.snapshots]
enabled = true
frames = 5

[hooks.autotrace]
# Records every function call in project code automatically (sys.monitoring).
enabled = true
capture_args = true
capture_results = true
# include = ["/opt/shared-lib/*"]      # trace extra paths outside the project
# exclude = ["*/generated/*"]          # skip noisy project paths
"""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def load_config(root: Path) -> Config:
    data: dict[str, Any] = {}
    cfg_path = root / "config.toml"
    if cfg_path.is_file():
        try:
            data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            data = {}

    rec = data.get("recording", {})
    storage = data.get("storage", {})
    redaction = data.get("redaction", {})
    hooks_data = data.get("hooks", {})
    http = hooks_data.get("http", {})
    sql = hooks_data.get("sql", {})
    log = hooks_data.get("logging", {})
    snaps = hooks_data.get("snapshots", {})
    autotrace = hooks_data.get("autotrace", {})

    cfg = Config(
        root=root,
        enabled=_env_bool("SLOMO_ENABLED", bool(rec.get("enabled", True))),
        max_value_repr=int(rec.get("max_value_repr", 2048)),
        max_payload_bytes=int(rec.get("max_payload_bytes", 65536)),
        max_collection_items=int(rec.get("max_collection_items", 25)),
        max_depth=int(rec.get("max_depth", 4)),
        flush_interval_s=float(rec.get("flush_interval_s", 0.5)),
        queue_max=int(rec.get("queue_max", 10_000)),
        retention_max_sessions=int(storage.get("retention_max_sessions", 200)),
        hooks=HookConfig(
            http=_env_bool("SLOMO_HOOK_HTTP", bool(http.get("enabled", True))),
            sql=_env_bool("SLOMO_HOOK_SQL", bool(sql.get("enabled", True))),
            logging=_env_bool("SLOMO_HOOK_LOGGING", bool(log.get("enabled", True))),
            logging_level=str(log.get("level", "WARNING")).upper(),
            snapshots=_env_bool("SLOMO_SNAPSHOTS", bool(snaps.get("enabled", True))),
            snapshot_frames=int(snaps.get("frames", 5)),
            sql_capture_params=bool(sql.get("capture_params", False)),
            unraisable=bool(hooks_data.get("unraisable", True)),
            autotrace=_env_bool("SLOMO_AUTOTRACE", bool(autotrace.get("enabled", True))),
            autotrace_capture_args=bool(autotrace.get("capture_args", True)),
            autotrace_capture_results=bool(autotrace.get("capture_results", True)),
            autotrace_include=[str(p) for p in autotrace.get("include", [])],
            autotrace_exclude=[str(p) for p in autotrace.get("exclude", [])],
        ),
        redact_keys=[str(k) for k in redaction.get("extra_keys", [])],
        redact_patterns=[str(p) for p in redaction.get("extra_patterns", [])],
        redact_defaults=bool(redaction.get("defaults", True)),
    )
    return cfg

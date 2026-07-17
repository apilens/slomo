"""Built-in JSONL storage backend.

Layout per session::

    .slomo/sessions/<yyyymmdd-hhmmss>-<id12>/
        metadata.json      # atomic rewrite (tmp + os.replace)
        timeline.jsonl     # append-only, one event per line
        snapshots/         # oversized payload spill files
        attachments/

Append-only discipline is the crash-safety story: a hard kill loses at most
the final partial line, and the reader tolerates exactly that.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from slomo._core.events import Event
from slomo._core.session import SessionMeta
from slomo.storage import paths


class JsonlSessionWriter:
    def __init__(self, session_dir: Path) -> None:
        self._dir = session_dir
        self._file: IO[str] = (session_dir / "timeline.jsonl").open("a", encoding="utf-8")

    def write_event(self, event: Event) -> None:
        self._file.write(event.to_json_line())

    def flush(self, *, fsync: bool = False) -> None:
        self._file.flush()
        if fsync:
            try:
                os.fsync(self._file.fileno())
            except OSError:
                pass

    def close(self) -> None:
        try:
            self.flush(fsync=True)
        finally:
            self._file.close()


class JsonlBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._dir_cache: dict[str, Path] = {}

    # ---------- write side ----------

    def create_session(self, meta: SessionMeta) -> JsonlSessionWriter:
        session_dir = paths.sessions_dir(self.root) / meta.dir_name
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "snapshots").mkdir(exist_ok=True)
        (session_dir / "attachments").mkdir(exist_ok=True)
        self._write_meta(session_dir, meta.to_dict())
        self._dir_cache[meta.id] = session_dir
        return JsonlSessionWriter(session_dir)

    def finalize_session(self, session_id: str, meta_updates: dict[str, Any]) -> None:
        session_dir = self._dir_for(session_id)
        if session_dir is None:
            return
        try:
            meta = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        meta.update(meta_updates)
        self._write_meta(session_dir, meta)

    def write_attachment(self, session_id: str, name: str, data: bytes) -> str:
        session_dir = self._dir_for(session_id)
        if session_dir is None:
            raise KeyError(f"unknown session: {session_id}")
        path = session_dir / "attachments" / name
        path.write_bytes(data)
        return str(path)

    def write_snapshot_file(self, session_id: str, event_id: str, payload: dict[str, Any]) -> str:
        session_dir = self._dir_for(session_id)
        if session_dir is None:
            raise KeyError(f"unknown session: {session_id}")
        path = session_dir / "snapshots" / f"{event_id}.json"
        path.write_text(json.dumps(payload, default=str), encoding="utf-8")
        return str(path)

    @staticmethod
    def _write_meta(session_dir: Path, meta: dict[str, Any]) -> None:
        tmp = session_dir / "metadata.json.tmp"
        tmp.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, session_dir / "metadata.json")

    # ---------- read side ----------

    def list_sessions(self) -> list[SessionMeta]:
        out = []
        sess_root = paths.sessions_dir(self.root)
        if not sess_root.is_dir():
            return out
        for d in sorted(sess_root.iterdir()):
            if not d.is_dir():
                continue
            meta = self._load_meta(d)
            if meta is not None:
                out.append(meta)
        return out

    def read_session_meta(self, session_id: str) -> SessionMeta:
        session_dir = self._dir_for(session_id)
        if session_dir is None:
            raise KeyError(f"unknown session: {session_id}")
        meta = self._load_meta(session_dir)
        if meta is None:
            raise KeyError(f"corrupt session metadata: {session_id}")
        return meta

    def resolve_session_id(self, ref: str) -> str:
        """Accept a full id, an id prefix (with or without dashes), or a
        directory-name prefix."""
        bare_ref = ref.replace("-", "")
        matches = []
        for meta in self.list_sessions():
            if meta.id == ref:
                return meta.id
            if (
                meta.id.startswith(ref)
                or meta.id.replace("-", "").startswith(bare_ref)
                or meta.dir_name.startswith(ref)
            ):
                matches.append(meta.id)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"no session matches {ref!r}")
        raise KeyError(f"ambiguous session reference {ref!r} ({len(matches)} matches)")

    def iter_events(self, session_id: str, *, from_offset: int = 0) -> Iterator[Event]:
        for event, _ in self.iter_events_with_offset(session_id, from_offset=from_offset):
            yield event

    def iter_events_with_offset(
        self, session_id: str, *, from_offset: int = 0
    ) -> Iterator[tuple[Event, int]]:
        """Yield (event, byte offset after its line). Tolerates a truncated tail."""
        path = self.timeline_path(session_id)
        if path is None or not path.is_file():
            return
        with path.open("rb") as f:
            if from_offset:
                f.seek(from_offset)
            offset = from_offset
            for raw in f:
                offset += len(raw)
                if not raw.endswith(b"\n"):
                    return  # truncated tail from a hard kill — ignore
                try:
                    yield Event.from_dict(json.loads(raw)), offset
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue

    def timeline_path(self, session_id: str) -> Path | None:
        session_dir = self._dir_for(session_id)
        return None if session_dir is None else session_dir / "timeline.jsonl"

    def timeline_size(self, session_id: str) -> int:
        path = self.timeline_path(session_id)
        try:
            return path.stat().st_size if path else 0
        except OSError:
            return 0

    def snapshot_payload(self, session_id: str, event_id: str) -> dict[str, Any] | None:
        session_dir = self._dir_for(session_id)
        if session_dir is None:
            return None
        path = session_dir / "snapshots" / f"{event_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def delete_session(self, session_id: str) -> None:
        import shutil

        session_dir = self._dir_for(session_id)
        if session_dir is not None and session_dir.is_dir():
            shutil.rmtree(session_dir)
        self._dir_cache.pop(session_id, None)

    # ---------- helpers ----------

    def _load_meta(self, session_dir: Path) -> SessionMeta | None:
        try:
            data = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
            meta = SessionMeta.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        self._dir_cache[meta.id] = session_dir
        if meta.status == "running" and not _pid_alive(meta.pid):
            meta.status = "abandoned"
        return meta

    def _dir_for(self, session_id: str) -> Path | None:
        cached = self._dir_cache.get(session_id)
        if cached is not None and cached.is_dir():
            return cached
        sess_root = paths.sessions_dir(self.root)
        if not sess_root.is_dir():
            return None
        for d in sess_root.iterdir():
            if not d.is_dir():
                continue
            meta = self._load_meta(d)
            if meta is not None and meta.id == session_id:
                return d
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True

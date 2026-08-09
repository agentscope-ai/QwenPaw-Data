"""Persisted KG document ingest status (``.kg-status/`` under doc storage)."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, cast

from ..config import CFG, Config
from .local_doc_client import canonical_doc_id, filename_from_doc_id

IngestStatus = Literal["building", "ready", "failed"]
_VALID_INGEST_STATUSES = {"building", "ready", "failed"}
_MAX_ERROR_LEN = 500


def _is_process_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is NOT a liveness probe on Windows: signal 0 is
        # CTRL_C_EVENT and is broadcast to every process on the console.
        # Query the process handle instead.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            alive = bool(
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                and exit_code.value == 259  # STILL_ACTIVE
            )
        finally:
            kernel32.CloseHandle(handle)
        return alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class IngestState:
    status: IngestStatus
    error: str | None = None


class IngestStatusStore:
    """File-backed ingest status keyed by canonical ``doc_id``."""

    def __init__(self, status_dir: Path) -> None:
        self._status_dir = status_dir
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, cfg: Config = CFG) -> IngestStatusStore:
        storage_dir = Path(cfg.doc_storage_dir)
        return cls(status_dir=storage_dir / ".kg-status")

    def _status_path(self, doc_id: str) -> Path:
        return self._status_dir / f"{filename_from_doc_id(doc_id)}.json"

    def _read_payload(self, doc_id: str) -> dict[str, object] | None:
        status_path = self._status_path(doc_id)
        with self._lock:
            if not status_path.exists():
                return None
            try:
                raw = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return raw if isinstance(raw, dict) else None

    def _write_payload(self, doc_id: str, payload: dict[str, object]) -> None:
        status_path = self._status_path(doc_id)
        with self._lock:
            self._status_dir.mkdir(parents=True, exist_ok=True)
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self._status_dir,
                    prefix=".status-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                    temp_path = Path(handle.name)
                os.replace(temp_path, status_path)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

    def _delete_payload(self, doc_id: str) -> None:
        with self._lock:
            self._status_path(doc_id).unlink(missing_ok=True)

    def begin(self, doc_id: str) -> str:
        """Mark a document as building and return a token for ``finalize``."""
        canonical = canonical_doc_id(doc_id)
        token = uuid.uuid4().hex
        self._write_payload(
            doc_id,
            {
                "doc_id": canonical,
                "status": "building",
                "build_token": token,
                "error": None,
                "updated_at": datetime.now(tz=timezone.utc).isoformat(),
                "owner_host": socket.gethostname(),
                "owner_pid": os.getpid(),
            },
        )
        return token

    def mark_failed(self, doc_id: str, error: str) -> None:
        """Persist a terminal failed state (e.g. driver unavailable on upload)."""
        canonical = canonical_doc_id(doc_id)
        normalized = (error or "").strip() or "Knowledge graph build failed"
        self._write_payload(
            doc_id,
            {
                "doc_id": canonical,
                "status": "failed",
                "error": normalized[:_MAX_ERROR_LEN],
                "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            },
        )

    def finalize(
        self,
        doc_id: str,
        build_token: str,
        status: Literal["ready", "failed"],
        error: str | None = None,
    ) -> bool:
        """Apply a terminal status after background build; ignore stale tasks."""
        canonical = canonical_doc_id(doc_id)
        payload = self._read_payload(doc_id)
        if payload is None:
            return False
        if payload.get("doc_id") != canonical:
            return False
        if payload.get("status") != "building":
            return False
        if payload.get("build_token") != build_token:
            return False

        if status == "ready":
            self._delete_payload(doc_id)
            return True

        normalized = (error or "").strip() or "Knowledge graph build failed"
        self._write_payload(
            doc_id,
            {
                "doc_id": canonical,
                "status": "failed",
                "error": normalized[:_MAX_ERROR_LEN],
                "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            },
        )
        return True

    def remove(self, doc_id: str) -> None:
        self._delete_payload(doc_id)

    def resolve(self, doc_id: str) -> tuple[IngestStatus, str | None]:
        """Return persisted status; missing file means ``ready``."""
        canonical = canonical_doc_id(doc_id)
        payload = self._read_payload(doc_id)
        if payload is None:
            return "ready", None

        raw_status = payload.get("status")
        stored_doc_id = payload.get("doc_id")
        if (
            stored_doc_id != canonical
            or not isinstance(raw_status, str)
            or raw_status not in _VALID_INGEST_STATUSES
        ):
            return "failed", "Invalid ingest status metadata"

        if raw_status == "building":
            owner_host = payload.get("owner_host")
            owner_pid = payload.get("owner_pid")
            if owner_host == socket.gethostname() and not _is_process_alive(owner_pid):
                interrupted = "Knowledge graph build was interrupted"
                self.mark_failed(doc_id, interrupted)
                return "failed", interrupted

        ingest_status = cast(IngestStatus, raw_status)
        if ingest_status == "ready":
            return "ready", None
        if ingest_status != "failed":
            return ingest_status, None

        raw_error = payload.get("error")
        message = raw_error.strip() if isinstance(raw_error, str) else ""
        return "failed", message or "Knowledge graph build failed"


_store: Optional[IngestStatusStore] = None


def get_ingest_status_store() -> IngestStatusStore:
    global _store
    if _store is None:
        _store = IngestStatusStore.from_config()
    return _store

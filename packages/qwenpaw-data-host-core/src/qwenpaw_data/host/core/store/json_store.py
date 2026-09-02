# -*- coding: utf-8 -*-
"""JSON-file implementations of ChatStore and ChatEventStore.

Layout: ``<root>/chats/<chat_id>/chat.json`` and ``events.jsonl``.
The events file is append-only (O(1) per event); the chat record is
rewritten atomically. Sequence numbers are dense and monotonic per chat.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.api.models.stream_objects import (
    StreamObject,
    dump_stream_object,
    parse_stream_object,
)
from qwenpaw_data.host.core.domain.chat import ACTIVE, Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.session._locking import file_lock, write_atomic
from qwenpaw_data.host.core.utils.time import utcnow


def _dt_to_json(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dt_from_json(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _chat_to_dict(chat: Chat) -> dict[str, Any]:
    return {
        "id": chat.id,
        "session_id": chat.session_id,
        "identity": {
            "user_id": chat.identity.user_id,
            "attrs": dict(chat.identity.attrs),
        },
        "sequence": chat.sequence,
        "user_input": chat.user_input,
        "datasource_id": chat.datasource_id,
        "kind": chat.kind,
        "status": chat.status,
        "last_sequence_number": chat.last_sequence_number,
        "started_at": _dt_to_json(chat.started_at),
        "completed_at": _dt_to_json(chat.completed_at),
        "active_duration_ms": chat.active_duration_ms,
        "error": chat.error,
        "plan": chat.plan,
        "created_at": _dt_to_json(chat.created_at),
        "updated_at": _dt_to_json(chat.updated_at),
    }


def _chat_from_dict(data: dict[str, Any]) -> Chat:
    identity = data["identity"]
    return Chat(
        id=data["id"],
        session_id=data["session_id"],
        identity=Identity(
            user_id=identity["user_id"],
            attrs=identity.get("attrs") or {},
        ),
        sequence=data["sequence"],
        user_input=data["user_input"],
        datasource_id=data.get("datasource_id"),
        kind=data.get("kind", "simple"),
        status=data["status"],
        last_sequence_number=data.get("last_sequence_number", -1),
        started_at=_dt_from_json(data.get("started_at")),
        completed_at=_dt_from_json(data.get("completed_at")),
        active_duration_ms=data.get("active_duration_ms", 0),
        error=data.get("error"),
        plan=data.get("plan"),
        created_at=_dt_from_json(data["created_at"]),
        updated_at=_dt_from_json(data["updated_at"]),
    )


class JSONChatStore:
    def __init__(self, root: Path) -> None:
        self._chats_root = Path(root).expanduser().resolve() / "chats"

    def _chat_path(self, chat_id: str) -> Path:
        return self._chats_root / chat_id / "chat.json"

    async def add(self, chat: Chat) -> None:
        path = self._chat_path(chat.id)
        if await asyncio.to_thread(path.exists):
            raise RuntimeError(f"CONFLICT: chat already exists: {chat.id}")
        await asyncio.to_thread(self._write_locked, path, chat)

    async def get(
        self,
        chat_id: str,
        *,
        session_id: str | None = None,
    ) -> Chat:
        chat = await asyncio.to_thread(self._read_locked, self._chat_path(chat_id))
        if chat is None:
            raise LookupError(f"chat not found: {chat_id}")
        if session_id is not None and chat.session_id != session_id:
            raise LookupError(f"chat not found: {chat_id}")
        return chat

    async def save(self, chat: Chat) -> None:
        await asyncio.to_thread(self._write_locked, self._chat_path(chat.id), chat)

    async def get_active_for_session(self, session_id: str) -> Chat | None:
        for chat in await self.list_for_session(session_id):
            if chat.status in ACTIVE:
                return chat
        return None

    async def list_for_session(self, session_id: str) -> list[Chat]:
        chats = await asyncio.to_thread(self._scan)
        selected = [c for c in chats if c.session_id == session_id]
        selected.sort(key=lambda c: c.sequence)
        return selected

    async def list_active(self) -> list[Chat]:
        chats = await asyncio.to_thread(self._scan)
        return [c for c in chats if c.status in ACTIVE]

    async def update_plan(self, chat_id: str, plan: dict[str, Any]) -> None:
        chat = await self.get(chat_id)
        chat.plan = plan
        chat.updated_at = utcnow()
        await self.save(chat)

    async def reload_event_watermark(self, chat: Chat) -> None:
        events_path = self._chat_path(chat.id).with_name("events.jsonl")
        last = await asyncio.to_thread(_read_last_sequence, events_path)
        chat.apply_event_watermark(
            last_sequence_number=last,
            updated_at=utcnow(),
        )

    def _scan(self) -> list[Chat]:
        if not self._chats_root.exists():
            return []
        chats: list[Chat] = []
        for chat_dir in self._chats_root.iterdir():
            path = chat_dir / "chat.json"
            chat = self._read_locked(path)
            if chat is not None:
                chats.append(chat)
        return chats

    @staticmethod
    def _read_locked(path: Path) -> Chat | None:
        with file_lock(path):
            if not path.exists():
                return None
            return _chat_from_dict(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def _write_locked(path: Path, chat: Chat) -> None:
        with file_lock(path):
            write_atomic(path, _chat_to_dict(chat))


def _read_last_sequence(path: Path) -> int:
    if not path.exists():
        return -1
    last_line = ""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last_line = line
    if not last_line:
        return -1
    return int(json.loads(last_line)["sequence_number"])


class JSONChatEventStore:
    def __init__(self, root: Path) -> None:
        self._chats_root = Path(root).expanduser().resolve() / "chats"
        self._locks: dict[str, asyncio.Lock] = {}
        self._counters: dict[str, int] = {}

    def _events_path(self, chat_id: str) -> Path:
        return self._chats_root / chat_id / "events.jsonl"

    def _lock_for(self, chat_id: str) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    async def append(
        self,
        *,
        session_id: str,
        chat_id: str,
        payload: dict[str, Any],
    ) -> StreamObject:
        async with self._lock_for(chat_id):
            path = self._events_path(chat_id)
            counter = self._counters.get(chat_id)
            if counter is None:
                counter = await asyncio.to_thread(_read_last_sequence, path)
            sequence_number = counter + 1
            body = dict(payload)
            body["sequence_number"] = sequence_number
            body["session_id"] = session_id
            body["chat_id"] = chat_id
            obj = parse_stream_object(body)
            await asyncio.to_thread(self._append_locked, path, obj)
            self._counters[chat_id] = sequence_number
            return obj

    async def read_after(self, chat_id: str, after: int) -> list[StreamObject]:
        path = self._events_path(chat_id)
        return await asyncio.to_thread(self._read_after_locked, path, after)

    async def last_sequence_number(self, chat_id: str) -> int:
        async with self._lock_for(chat_id):
            counter = self._counters.get(chat_id)
            if counter is not None:
                return counter
            path = self._events_path(chat_id)
            return await asyncio.to_thread(_read_last_sequence, path)

    @staticmethod
    def _append_locked(path: Path, obj: StreamObject) -> None:
        line = json.dumps(
            dump_stream_object(obj),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with file_lock(path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()

    @staticmethod
    def _read_after_locked(path: Path, after: int) -> list[StreamObject]:
        with file_lock(path):
            if not path.exists():
                return []
            lines = path.read_text(encoding="utf-8").splitlines()
        events: list[StreamObject] = []
        for line in lines:
            if not line.strip():
                continue
            data = json.loads(line)
            if data["sequence_number"] > after:
                events.append(parse_stream_object(data))
        return events

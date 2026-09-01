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
from qwenpaw_data.host.core.domain.preference import (
    ModelOverride,
    ProviderCredential,
    UserPreferences,
)
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.session._locking import file_lock, write_atomic
from qwenpaw_data.host.core.store._prefs_logic import (
    clean_model_upsert,
    merge_provider_patch,
)
from qwenpaw_data.host.core.utils.secrets import decrypt_api_key
from qwenpaw_data.host.core.utils.time import utcnow


class JSONPreferencesStore:
    def __init__(self, root: Path) -> None:
        self._prefs_root = Path(root).expanduser().resolve() / "preferences"

    def _path(self, user_id: str) -> Path:
        return self._prefs_root / f"{user_id}.json"

    def _read(self, user_id: str) -> dict[str, Any]:
        path = self._path(user_id)
        with file_lock(path):
            if not path.exists():
                return {"providers": {}, "models": {}, "active": None}
            return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, user_id: str, doc: dict[str, Any]) -> None:
        path = self._path(user_id)
        with file_lock(path):
            write_atomic(path, doc)

    async def load(self, user_id: str) -> UserPreferences:
        doc = await asyncio.to_thread(self._read, user_id)
        providers = {
            pid: ProviderCredential(
                api_key=decrypt_api_key(rec["api_key_enc"]),
                base_url=rec.get("base_url"),
            )
            for pid, rec in doc["providers"].items()
        }
        models = {}
        for key, rec in doc["models"].items():
            pid, model_id = key.split("/", 1)
            models[(pid, model_id)] = ModelOverride(
                source=rec["source"],
                name=rec.get("name"),
                thinking_enabled=rec.get("thinking_enabled"),
                generate_kwargs=rec.get("generate_kwargs"),
            )
        active = doc.get("active") or {}
        return UserPreferences(
            user_id=user_id,
            providers=providers,
            models=models,
            default_provider_id=active.get("default_provider_id"),
            default_model_id=active.get("default_model_id"),
            light_provider_id=active.get("light_provider_id"),
            light_model_id=active.get("light_model_id"),
        )

    async def upsert_provider(
        self,
        user_id: str,
        provider_id: str,
        patch: dict[str, Any],
    ) -> None:
        doc = await asyncio.to_thread(self._read, user_id)
        current = doc["providers"].get(provider_id)
        api_key_enc, base_url = merge_provider_patch(
            exists=current is not None,
            current_api_key_enc=None if current is None else current["api_key_enc"],
            current_base_url=None if current is None else current.get("base_url"),
            patch=patch,
            provider_id=provider_id,
        )
        doc["providers"][provider_id] = {
            "api_key_enc": api_key_enc,
            "base_url": base_url,
        }
        await asyncio.to_thread(self._write, user_id, doc)

    async def delete_provider(self, user_id: str, provider_id: str) -> None:
        doc = await asyncio.to_thread(self._read, user_id)
        if provider_id not in doc["providers"]:
            raise LookupError(f"provider config not found: {provider_id}")
        del doc["providers"][provider_id]
        await asyncio.to_thread(self._write, user_id, doc)

    async def upsert_model(
        self,
        user_id: str,
        provider_id: str,
        model_id: str,
        *,
        source: str,
        name: str | None = None,
        thinking_enabled: bool | None = None,
        generate_kwargs: dict[str, Any] | None = None,
    ) -> None:
        model_id, name = clean_model_upsert(
            provider_id, model_id, source=source, name=name
        )
        doc = await asyncio.to_thread(self._read, user_id)
        doc["models"][f"{provider_id}/{model_id}"] = {
            "source": source,
            "name": name,
            "thinking_enabled": thinking_enabled,
            "generate_kwargs": generate_kwargs,
        }
        await asyncio.to_thread(self._write, user_id, doc)

    async def delete_model(
        self,
        user_id: str,
        provider_id: str,
        model_id: str,
    ) -> None:
        doc = await asyncio.to_thread(self._read, user_id)
        key = f"{provider_id}/{model_id}"
        if key not in doc["models"]:
            raise LookupError(
                f"model config not found: {provider_id}/{model_id}"
            )
        del doc["models"][key]
        await asyncio.to_thread(self._write, user_id, doc)

    async def get_active_models(self, user_id: str) -> dict[str, Any]:
        doc = await asyncio.to_thread(self._read, user_id)
        active = doc.get("active") or {}
        return {
            "default_provider_id": active.get("default_provider_id"),
            "default_model_id": active.get("default_model_id"),
            "light_provider_id": active.get("light_provider_id"),
            "light_model_id": active.get("light_model_id"),
        }

    async def set_active_models(
        self,
        user_id: str,
        *,
        default_provider_id: str,
        default_model_id: str,
        light_provider_id: str | None = None,
        light_model_id: str | None = None,
    ) -> dict[str, Any]:
        if (light_provider_id is None) != (light_model_id is None):
            raise ValueError(
                "light_provider_id and light_model_id must be set together"
            )
        prefs = await self.load(user_id)
        prefs.default_provider_id = default_provider_id
        prefs.default_model_id = default_model_id
        prefs.light_provider_id = light_provider_id
        prefs.light_model_id = light_model_id
        prefs.validate_selection()
        doc = await asyncio.to_thread(self._read, user_id)
        doc["active"] = {
            "default_provider_id": default_provider_id,
            "default_model_id": default_model_id,
            "light_provider_id": light_provider_id,
            "light_model_id": light_model_id,
        }
        await asyncio.to_thread(self._write, user_id, doc)
        return await self.get_active_models(user_id)


def _session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "identity": {
            "user_id": session.identity.user_id,
            "attrs": dict(session.identity.attrs),
        },
        "agent_id": session.agent_id,
        "title": session.title,
        "datasource_id": session.datasource_id,
        "chat_count": session.chat_count,
        "channel": session.channel,
        "created_at": _dt_to_json(session.created_at),
        "updated_at": _dt_to_json(session.updated_at),
        "deleted_at": _dt_to_json(session.deleted_at),
        "parent_session_id": session.parent_session_id,
        "forked_from_chat_id": session.forked_from_chat_id,
    }


def _session_from_dict(data: dict[str, Any]) -> Session:
    identity = data["identity"]
    return Session(
        id=data["id"],
        identity=Identity(
            user_id=identity["user_id"],
            attrs=identity.get("attrs") or {},
        ),
        agent_id=data.get("agent_id", "default"),
        title=data.get("title", ""),
        datasource_id=data.get("datasource_id"),
        chat_count=data.get("chat_count", 0),
        channel=data.get("channel", "console"),
        created_at=_dt_from_json(data["created_at"]),
        updated_at=_dt_from_json(data["updated_at"]),
        deleted_at=_dt_from_json(data.get("deleted_at")),
        parent_session_id=data.get("parent_session_id"),
        forked_from_chat_id=data.get("forked_from_chat_id"),
    )


class JSONSessionStore:
    def __init__(self, root: Path) -> None:
        self._sessions_root = Path(root).expanduser().resolve() / "sessions"
        self._chats = JSONChatStore(root)

    def _session_path(self, session_id: str) -> Path:
        return self._sessions_root / f"{session_id}.json"

    async def add(self, session: Session) -> None:
        path = self._session_path(session.id)
        if await asyncio.to_thread(path.exists):
            raise RuntimeError(f"CONFLICT: session already exists: {session.id}")
        await asyncio.to_thread(self._write_locked, path, session)

    async def get(self, session_id: str) -> Session:
        session = await asyncio.to_thread(
            self._read_locked, self._session_path(session_id)
        )
        if session is None or session.deleted_at is not None:
            raise LookupError(f"session not found: {session_id}")
        return session

    async def save(self, session: Session) -> None:
        path = self._session_path(session.id)
        if not await asyncio.to_thread(path.exists):
            raise LookupError(f"session not found: {session.id}")
        await asyncio.to_thread(self._write_locked, path, session)

    async def has_active_chat(self, session_id: str) -> bool:
        return await self._chats.get_active_for_session(session_id) is not None

    async def list(
        self,
        *,
        search_text: str | None = None,
        status: str | None = None,
        datasource_id: str | None = None,
        channel: str | None = None,
        sort: str = "updated_desc",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[tuple[Session, bool]], int]:
        if page < 1 or page_size < 1:
            raise ValueError("page/page_size invalid")
        sessions = await asyncio.to_thread(self._scan_sessions)
        selected: list[tuple[Session, bool]] = []
        for session in sessions:
            if session.deleted_at is not None:
                continue
            if search_text and search_text not in session.title:
                continue
            if datasource_id and session.datasource_id != datasource_id:
                continue
            if channel and session.channel != channel:
                continue
            active = await self.has_active_chat(session.id)
            if status == "running" and not active:
                continue
            if status == "idle" and active:
                continue
            selected.append((session, active))
        if sort == "chat_count_desc":
            selected.sort(key=lambda p: (p[0].chat_count, p[0].updated_at), reverse=True)
        elif sort == "updated_asc":
            selected.sort(key=lambda p: p[0].updated_at)
        else:
            selected.sort(key=lambda p: p[0].updated_at, reverse=True)
        total = len(selected)
        start = (page - 1) * page_size
        return selected[start : start + page_size], total

    async def delete(self, session_id: str) -> None:
        path = self._session_path(session_id)
        session = await asyncio.to_thread(self._read_locked, path)
        if session is None or session.deleted_at is not None:
            return
        session.soft_delete(has_active_chat=await self.has_active_chat(session_id))
        await asyncio.to_thread(self._write_locked, path, session)

    def _scan_sessions(self) -> list[Session]:
        if not self._sessions_root.exists():
            return []
        sessions: list[Session] = []
        for path in self._sessions_root.glob("*.json"):
            session = self._read_locked(path)
            if session is not None:
                sessions.append(session)
        return sessions

    @staticmethod
    def _read_locked(path: Path) -> Session | None:
        with file_lock(path):
            if not path.exists():
                return None
            return _session_from_dict(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def _write_locked(path: Path, session: Session) -> None:
        with file_lock(path):
            write_atomic(path, _session_to_dict(session))


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

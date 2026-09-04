# -*- coding: utf-8 -*-
"""Fork a session: copy records, events, uploads, artifacts, agent state."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.api.models.stream_objects import dump_stream_object
from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.session_fork import SessionFork
from qwenpaw_data.host.core.paths import Paths
from qwenpaw_data.host.core.store.protocols import (
    AttachmentStore,
    ChatEventStore,
    ChatStore,
    SessionStore,
)

_BASE_FIELDS = ("sequence_number", "session_id", "chat_id")


async def fork_session(
    *,
    sessions: SessionStore,
    chats: ChatStore,
    events: ChatEventStore,
    attachments: AttachmentStore,
    home: Path,
    session_id: str,
    chat_id: str,
) -> Session:
    source = await sessions.get(session_id)
    at_chat = await chats.get(chat_id, session_id=session_id)
    fork = SessionFork(
        source,
        at_chat,
        has_active_chat=await sessions.has_active_chat(session_id),
    )
    selected = [
        chat
        for chat in await chats.list_for_session(session_id)
        if chat.sequence <= at_chat.sequence
    ]
    if not selected:
        raise LookupError("chat not found")
    for chat in selected:
        fork.remap(chat.id, "chat")

    user_id = source.identity.user_id
    loaded: dict[str, Attachment] = {}
    for chat in selected:
        for item in chat.attachments:
            attachment_id = item["attachment_id"]
            if attachment_id in loaded:
                continue
            attachment = await attachments.get(user_id, attachment_id)
            if attachment.session_id != source.id:
                raise LookupError("attachment not found")
            fork.remap(attachment.id, "att")
            loaded[attachment.id] = attachment

    created_paths = await asyncio.to_thread(
        _copy_files, fork, home, list(loaded.values())
    )
    try:
        await sessions.add(fork.target)
        for chat in selected:
            await chats.add(fork.copy_chat(chat))
        for chat in selected:
            target_chat_id = fork.mapped(chat.id)
            for obj in await events.read_after(chat.id, -1):
                payload = dump_stream_object(obj)
                for field in _BASE_FIELDS:
                    payload.pop(field, None)
                await events.append(
                    session_id=fork.target.id,
                    chat_id=target_chat_id,
                    payload=fork.rewrite(payload),
                )
        for attachment in loaded.values():
            await attachments.add(fork.copy_attachment(attachment))
    except Exception:
        await asyncio.to_thread(_cleanup, created_paths)
        raise
    return fork.target


def _copy_files(
    fork: SessionFork,
    home: Path,
    attachments: list[Attachment],
) -> list[Path]:
    source_paths = Paths(home, session_id=fork.source.id)
    target_paths = Paths(home, session_id=fork.target.id)
    created: list[Path] = []

    if attachments:
        uploads_dir = target_paths.workspace / "uploads" / fork.target.id
        uploads_dir.mkdir(parents=True, exist_ok=True)
        created.append(uploads_dir)
        for attachment in attachments:
            src = source_paths.workspace / attachment.storage_path
            if src.is_file():
                shutil.copy2(src, uploads_dir / attachment.filename)

    if source_paths.artifact_dir.is_dir():
        shutil.copytree(
            source_paths.artifact_dir,
            target_paths.artifact_dir,
            dirs_exist_ok=True,
        )
        created.append(target_paths.artifact_dir)

    # Agent conversational state and DAG snapshots: `{user}_{sid}.json`.
    for state_root in (source_paths.console_root, source_paths.dag_root):
        if not state_root.is_dir():
            continue
        for src in state_root.glob(f"*_{fork.source.id}.json"):
            dst = src.with_name(
                src.name.replace(fork.source.id, fork.target.id)
            )
            dst.write_text(
                _rewrite_text(src.read_text(encoding="utf-8"), fork),
                encoding="utf-8",
            )
            created.append(dst)
    return created


def _rewrite_text(text: str, fork: SessionFork) -> str:
    # Validate JSON before and after so a broken rewrite fails loudly.
    json.loads(text)
    rewritten: Any = text
    rewritten = fork.rewrite(rewritten)
    json.loads(rewritten)
    return rewritten


def _cleanup(paths: list[Path]) -> None:
    for path in reversed(paths):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink(missing_ok=True)

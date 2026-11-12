# -*- coding: utf-8 -*-
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from agentscope.state import AgentState
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.session_fork import SessionFork
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.agent_state import AgentStateStore
from qwenpaw_data.host.core.repositories.artifact import ArtifactRepository
from qwenpaw_data.host.core.repositories.attachment import AttachmentRepository
from qwenpaw_data.host.core.repositories.biz_event import BizEventRepository
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.chat_event import (
    ChatEventRecord,
    ChatEventRepository,
)
from qwenpaw_data.host.core.repositories.followup import FollowUpRepository
from qwenpaw_data.host.core.repositories.segment import SegmentRepository
from qwenpaw_data.host.core.repositories.session import SessionRepository
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.session import access_session


class SessionForkRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    async def execute(self, session_id: str, chat_id: str) -> Session:
        sessions = SessionRepository(self.db, self.identity)
        chats = ChatRepository(self.db, self.identity)
        source = await access_session(self.db, self.identity, session_id, write=True)
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
        await self._persist(fork, selected)
        return fork.target

    async def _persist(self, fork: SessionFork, chats: list[Chat]) -> None:
        for chat in chats:
            fork.remap(chat.id, "chat")
        attachments = await self._attachments(fork, chats)
        artifacts = await self._artifacts(fork, chats)
        dest_dirs = self._copy_files(fork, attachments, artifacts)
        try:
            await SessionRepository(self.db, self.identity).add(fork.target)
            chat_repo = ChatRepository(self.db, self.identity)
            for chat in chats:
                await chat_repo.add(fork.copy_chat(chat))
            await self._copy_events(fork, chats)
            await self._copy_segments(fork, chats)
            await self._copy_biz_events(fork, chats)
            await self._copy_followups(fork, chats)
            attachments_repo = AttachmentRepository(self.db, self.identity)
            for attachment in attachments:
                await attachments_repo.add(fork.copy_attachment(attachment))
            artifacts_repo = ArtifactRepository(self.db, self.identity)
            for artifact in artifacts:
                copied = fork.copy_artifact(artifact)
                await artifacts_repo.register(
                    id=copied["id"],
                    session_id=copied["session_id"],
                    chat_id=copied["chat_id"],
                    name=copied["name"],
                    path=copied["path"],
                )
            await AgentStateStore(self.db, self.identity).save(
                fork.target.id,
                AgentState.model_validate(fork.rewrite(fork.at_chat.agent_state)),
            )
        except Exception:
            self._cleanup(dest_dirs)
            raise

    async def _attachments(
        self, fork: SessionFork, chats: list[Chat]
    ) -> list[Attachment]:
        repo = AttachmentRepository(self.db, self.identity)
        loaded: dict[str, Attachment] = {}
        for chat in chats:
            for item in chat.attachments:
                attachment_id = item["attachment_id"]
                if attachment_id in loaded:
                    continue
                attachment = await repo.get(attachment_id)
                if attachment.session_id != fork.source.id:
                    raise LookupError("attachment not found")
                fork.remap(attachment.id, "att")
                loaded[attachment.id] = attachment
        return list(loaded.values())

    async def _artifacts(
        self, fork: SessionFork, chats: list[Chat]
    ) -> list[dict[str, Any]]:
        by_chat = await ArtifactRepository(self.db, self.identity).list_for_chats(
            [chat.id for chat in chats]
        )
        artifacts = [item for items in by_chat.values() for item in items]
        for artifact in artifacts:
            fork.remap(artifact["id"], "art")
        return artifacts

    async def _copy_events(self, fork: SessionFork, chats: list[Chat]) -> None:
        repo = ChatEventRepository(self.db, self.identity)
        events = [
            ChatEventRecord(
                chat_id=fork.mapped(event.chat_id),
                sequence_number=event.sequence_number,
                object=event.object,
                session_id=fork.target.id,
                payload=fork.rewrite(event.payload),
                created_at=event.created_at,
            )
            for event in await repo.list_for_chats([chat.id for chat in chats])
        ]
        await repo.add_many(events)

    async def _copy_segments(self, fork: SessionFork, chats: list[Chat]) -> None:
        repo = SegmentRepository(self.db, self.identity)
        by_chat = await repo.list_for_chats([chat.id for chat in chats])
        for old_chat_id, items in by_chat.items():
            for item in items:
                await repo.upsert(
                    chat_id=fork.mapped(old_chat_id),
                    segment_id=item["segment_id"],
                    title=item["title"],
                    coverage=item["coverage"],
                    behavior=item["behavior"],
                    conclusion=item["conclusion"],
                    input=item["input"],
                    artifact=fork.rewrite(item["artifact"]),
                    confidence=fork.rewrite(item.get("confidence")),
                    started_at=item["started_at"],
                    ended_at=item["ended_at"],
                )

    async def _copy_biz_events(self, fork: SessionFork, chats: list[Chat]) -> None:
        repo = BizEventRepository(self.db, self.identity)
        by_chat = await repo.list_for_chats(
            [chat.id for chat in chats], main_only=False
        )
        for old_chat_id, items in by_chat.items():
            for item in items:
                await repo.upsert(
                    chat_id=fork.mapped(old_chat_id),
                    event_id=item["event_id"],
                    seq=item["seq"],
                    channel=item["channel"],
                    block_id=item["block_id"],
                    status=item["status"],
                    presentation=fork.rewrite(item["presentation"]),
                    started_at=item["started_at"],
                    ended_at=item["ended_at"],
                )

    async def _copy_followups(self, fork: SessionFork, chats: list[Chat]) -> None:
        repo = FollowUpRepository(self.db, self.identity)
        by_chat = await repo.list_for_chats([chat.id for chat in chats])
        for old_chat_id, item in by_chat.items():
            if item is None:
                continue
            await repo.upsert(
                chat_id=fork.mapped(old_chat_id), questions=item["questions"]
            )

    def _copy_files(
        self,
        fork: SessionFork,
        attachments: list[Attachment],
        artifacts: list[dict[str, Any]],
    ) -> list[Path]:
        src = self._paths(fork.source)
        dest = self._paths(fork.target)
        created: list[Path] = []
        try:
            for directory in (dest.uploads_dir, dest.artifact_dir):
                if directory.exists():
                    raise ValueError(f"fork dest already exists: {directory}")
            for attachment in attachments:
                copied = fork.copy_attachment(attachment)
                dest_file = dest.workspace / copied.storage_path
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                created.append(dest.uploads_dir)
                shutil.copy2(attachment.require_file(src.workspace), dest_file)
            for artifact in artifacts:
                copied = fork.copy_artifact(artifact)
                dest_file = dest.artifact_dir / copied["path"]
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                created.append(dest.artifact_dir)
                shutil.copy2(
                    self._resolve_artifact(src.artifact_dir, artifact["path"]),
                    dest_file,
                )
        except Exception:
            self._cleanup(created)
            raise
        return list(dict.fromkeys(created))

    @staticmethod
    def _resolve_artifact(artifact_dir: Path, relative_path: str) -> Path:
        candidate = (artifact_dir / relative_path).resolve()
        if not candidate.is_relative_to(artifact_dir.resolve()):
            raise ValueError("artifact path escapes session artifact directory")
        if not candidate.is_file():
            raise LookupError(f"artifact file not found: {relative_path}")
        return candidate

    @staticmethod
    def _paths(session: Session) -> UserAgentPaths:
        return UserAgentPaths.from_identity(
            home=get_settings().paths.home,
            identity=session.identity,
            agent_id=session.agent_id,
            session_id=session.id,
        )

    @staticmethod
    def _cleanup(paths: list[Path]) -> None:
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)

# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any

from qwenpaw_data.host.core.api.models.stream_objects import (
    StreamObject,
    dump_stream_object,
    parse_stream_object,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.tracing.records import tracer_send_confidence
from qwenpaw_data.host.core.repositories.artifact import ArtifactRepository
from qwenpaw_data.host.core.repositories.biz_event import BizEventRepository
from qwenpaw_data.host.core.repositories.chat_event import (
    ChatEventRecord,
    ChatEventRepository,
)
from qwenpaw_data.host.core.repositories.followup import FollowUpRepository
from qwenpaw_data.host.core.repositories.segment import SegmentRepository
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.stream.hub import get_hub
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.status import is_terminal_response
from qwenpaw_data.host.core.utils.time import utcnow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class OutputStream:
    """One writer per chat: assign seq, persist the log, publish to the process hub.

    The terminal ``response`` closes the log: nothing is appended after it.
    Domain rows that may arrive late (follow-ups, biz events, segments,
    artifacts) are still persisted, but no longer streamed.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session_id: str,
        chat_id: str,
        identity: Identity,
        last_sequence_number: int,
    ) -> None:
        self.session_factory = session_factory
        self.session_id = session_id
        self.chat_id = chat_id
        self.identity = identity
        self.hub = get_hub()
        self.response_id = create_id("response")
        self._lock = asyncio.Lock()
        self._seq = last_sequence_number
        self._pending: list[ChatEventRecord] = []
        self._terminal = False

    @property
    def closed(self) -> bool:
        """Whether this turn's terminal response has been appended."""
        return self._terminal

    async def flush(self, *, delete_content: bool = False) -> None:
        async with self._lock:
            await self._flush_locked(delete_content=delete_content)

    async def _flush_locked(self, *, delete_content: bool = False) -> None:
        if not self._pending and not delete_content:
            return
        batch = self._pending
        self._pending = []
        try:
            async with self.session_factory() as db:
                events = ChatEventRepository(db, self.identity)
                if batch:
                    await events.persist_batch(batch)
                if delete_content:
                    await events.delete_chat_content(self.chat_id)
                await db.commit()
        except Exception:
            self._pending = batch + self._pending
            raise

    async def append(self, payload: dict[str, Any]) -> StreamObject:
        async with self._lock:
            return await self._append_locked(payload)

    async def _append_locked(self, payload: dict[str, Any]) -> StreamObject:
        """Assign seq, persist when due and publish, all under ``_lock``.

        Publishing under the lock keeps live subscribers in sequence order, and
        a terminal response is committed before anyone can see it.
        """
        if "object" not in payload:
            raise ValueError("object is required")
        if self._terminal:
            raise RuntimeError(
                f"chat {self.chat_id} already sent its terminal response; "
                f"refusing to append {payload['object']!r}"
            )
        seq = self._next_seq()
        now = utcnow()
        data = {
            **payload,
            "sequence_number": seq,
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "created_at": now,
        }
        obj = parse_stream_object(data)
        self._pending.append(
            ChatEventRecord(
                chat_id=self.chat_id,
                sequence_number=seq,
                object=obj.object,
                session_id=self.session_id,
                payload=dump_stream_object(obj),
                created_at=now,
            )
        )
        terminal = is_terminal_response(obj)
        if terminal:
            self._terminal = True
        should_flush = (
            terminal
            or not self._is_delta(obj)
            or len(self._pending) >= get_settings().stream.delta_flush_size
        )
        if should_flush:
            await self._flush_locked(delete_content=terminal)
        await self.hub.publish(self.chat_id, obj)
        return obj

    async def _append_if_open(self, payload: dict[str, Any]) -> StreamObject | None:
        """Stream a persisted domain row unless the terminal already closed the log.

        Callers hold ``_lock`` across their upsert and this check, so the
        terminal response cannot slip in between.
        """
        if self._terminal:
            return None
        return await self._append_locked(payload)

    async def response(self, status: str, **extra: Any) -> StreamObject:
        payload: dict[str, Any] = {
            "object": "response",
            "id": self.response_id,
            "status": status,
        }
        payload.update(extra)
        return await self.append(payload)

    async def response_created(self) -> StreamObject:
        return await self.response("created")

    async def response_in_progress(self) -> StreamObject:
        return await self.response("in_progress")

    async def response_completed(self) -> StreamObject:
        return await self.response("completed")

    async def response_cancelled(self) -> StreamObject:
        return await self.response("cancelled")

    async def response_failed(self, *, error: dict[str, Any]) -> StreamObject:
        return await self.response("failed", error=error)

    async def message_start(
        self,
        *,
        msg_id: str,
        sequence: int,
        type: str,
        role: str,
        source_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StreamObject:
        payload: dict[str, Any] = {
            "object": "message",
            "id": msg_id,
            "sequence": sequence,
            "type": type,
            "role": role,
            "status": "in_progress",
            "content": [],
        }
        if source_id is not None:
            payload["source_id"] = source_id
        if metadata is not None:
            payload["metadata"] = metadata
        return await self.append(payload)

    async def message_complete(
        self,
        *,
        msg_id: str,
        sequence: int,
        type: str,
        role: str,
        content: list[dict[str, Any]],
        source_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StreamObject:
        payload: dict[str, Any] = {
            "object": "message",
            "id": msg_id,
            "sequence": sequence,
            "type": type,
            "role": role,
            "status": "completed",
            "content": content,
        }
        if source_id is not None:
            payload["source_id"] = source_id
        if metadata is not None:
            payload["metadata"] = metadata
        return await self.append(payload)

    async def text_delta(self, *, msg_id: str, index: int, text: str) -> StreamObject:
        return await self.append(
            {
                "object": "content",
                "type": "text",
                "delta": True,
                "msg_id": msg_id,
                "index": index,
                "text": text,
            }
        )

    async def text_end(self, *, msg_id: str, index: int, text: str) -> StreamObject:
        return await self.append(
            {
                "object": "content",
                "type": "text",
                "delta": False,
                "msg_id": msg_id,
                "index": index,
                "text": text,
            }
        )

    async def data_delta(
        self, *, msg_id: str, index: int, data: dict[str, Any]
    ) -> StreamObject:
        return await self.append(
            {
                "object": "content",
                "type": "data",
                "delta": True,
                "msg_id": msg_id,
                "index": index,
                "data": data,
            }
        )

    async def data_end(
        self, *, msg_id: str, index: int, data: dict[str, Any]
    ) -> StreamObject:
        return await self.append(
            {
                "object": "content",
                "type": "data",
                "delta": False,
                "msg_id": msg_id,
                "index": index,
                "data": data,
            }
        )

    async def biz_event(self, **fields: Any) -> StreamObject | None:
        async with self._lock:
            async with self.session_factory() as db:
                event = await BizEventRepository(db, self.identity).upsert(
                    chat_id=self.chat_id, **fields
                )
                await db.commit()
            return await self._append_if_open(
                {"object": "biz_event", "biz_event": event}
            )

    async def segment(self, **fields: Any) -> StreamObject | None:
        async with self._lock:
            async with self.session_factory() as db:
                seg = await SegmentRepository(db, self.identity).upsert(
                    chat_id=self.chat_id, **fields
                )
                await tracer_send_confidence(
                    db, self.identity, seg, chat_id=self.chat_id
                )
                await db.commit()
            return await self._append_if_open({"object": "segment", "segment": seg})

    async def artifact_registered(self, **fields: Any) -> StreamObject | None:
        async with self._lock:
            async with self.session_factory() as db:
                art = await ArtifactRepository(db, self.identity).register(
                    session_id=self.session_id, chat_id=self.chat_id, **fields
                )
                await db.commit()
            return await self._append_if_open(
                {"object": "artifact.registered", "artifact": art}
            )

    async def followup_generated(self, *, questions: list[str]) -> StreamObject | None:
        """Persist follow-ups; stream them only while the turn is still open.

        A late result (the recommender outran the turn) stays in the repository
        for the next snapshot but never lands behind the terminal response.
        """
        async with self._lock:
            async with self.session_factory() as db:
                followup = await FollowUpRepository(db, self.identity).upsert(
                    chat_id=self.chat_id, questions=questions
                )
                await db.commit()
            return await self._append_if_open(
                {"object": "followup.generated", "followup": followup}
            )

    async def context_usage(
        self,
        *,
        context_size: int,
        used_tokens: int,
        trigger_ratio: float,
        reserve_ratio: float,
        tool_result_limit: int,
    ) -> StreamObject:
        return await self.append(
            {
                "object": "context.usage",
                "context_size": context_size,
                "used_tokens": used_tokens,
                "trigger_ratio": trigger_ratio,
                "reserve_ratio": reserve_ratio,
                "tool_result_limit": tool_result_limit,
            }
        )

    async def mcp_connect(self, **fields: Any) -> StreamObject:
        return await self.append({"object": "mcp.connect", **fields})

    async def model_call(self, *, model_name: str) -> StreamObject:
        return await self.append({"object": "model.call", "model_name": model_name})

    async def model_call_end(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> StreamObject:
        return await self.append(
            {
                "object": "model.call.end",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )

    async def tool_result_end(self, *, tool_name: str, call_id: str) -> StreamObject:
        return await self.append(
            {
                "object": "tool.result.end",
                "tool_name": tool_name,
                "call_id": call_id,
            }
        )

    async def tool_result_start(self, *, tool_name: str, call_id: str) -> StreamObject:
        return await self.append(
            {"object": "tool.result.start", "tool_name": tool_name, "call_id": call_id}
        )

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _is_delta(obj: StreamObject) -> bool:
        return obj.object == "content" and obj.delta is True

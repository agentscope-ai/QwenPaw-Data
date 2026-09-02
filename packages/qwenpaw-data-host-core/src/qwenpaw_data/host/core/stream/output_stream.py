# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.api.models.stream_objects import StreamObject
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.store.protocols import ChatEventStore
from qwenpaw_data.host.core.stream.hub import get_hub
from qwenpaw_data.host.core.utils.ids import create_id


class OutputStream:
    """Protocol-level writer: persist chat events, then fan out via EventHub."""

    def __init__(
        self,
        events: ChatEventStore,
        *,
        session_id: str,
        chat_id: str,
        identity: Identity,
    ) -> None:
        self.events = events
        self.session_id = session_id
        self.chat_id = chat_id
        self.identity = identity
        self.hub = get_hub()
        self.response_id = create_id("response")

    async def append(self, payload: dict[str, Any]) -> StreamObject:
        if "object" not in payload:
            raise ValueError("object is required")
        obj = await self.events.append(
            session_id=self.session_id,
            chat_id=self.chat_id,
            payload=payload,
        )
        await self.hub.publish(self.chat_id, obj)
        return obj

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

    async def text_delta(
        self, *, msg_id: str, index: int, text: str
    ) -> StreamObject:
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

    async def text_end(
        self, *, msg_id: str, index: int, text: str
    ) -> StreamObject:
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

    async def biz_event(self, **fields: Any) -> StreamObject:
        event = {"chat_id": self.chat_id, **fields}
        return await self.append({"object": "biz_event", "biz_event": event})

    async def segment(self, **fields: Any) -> StreamObject:
        seg = {"chat_id": self.chat_id, **fields}
        return await self.append({"object": "segment", "segment": seg})

    async def artifact_registered(self, **fields: Any) -> StreamObject:
        art = {"session_id": self.session_id, "chat_id": self.chat_id, **fields}
        return await self.append({"object": "artifact.registered", "artifact": art})

    async def followup_generated(self, *, questions: list[str]) -> StreamObject:
        followup = {"chat_id": self.chat_id, "questions": questions}
        return await self.append(
            {"object": "followup.generated", "followup": followup}
        )

    async def task_status(
        self,
        *,
        event_type: str,
        graph_snapshot: dict[str, Any] | None = None,
        status: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> StreamObject:
        payload: dict[str, Any] = {
            "object": "task_status",
            "event_type": event_type,
        }
        if graph_snapshot is not None:
            payload["graph_snapshot"] = graph_snapshot
        if status is not None:
            payload["status"] = status
        if error is not None:
            payload["error"] = error
        return await self.append(payload)

# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

from qwenpaw_data.host.core.api.models.stream_objects import StreamObject
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.store.protocols import ChatEventStore
from qwenpaw_data.host.core.stream.hub import get_hub
from qwenpaw_data.host.core.utils.ids import create_id

logger = logging.getLogger(__name__)

# Guardrail against any producer that re-persists an ever-growing snapshot.
# Legitimate tool results are expected to stay well below this; the limit is
# configurable because deployments may stream larger artifacts.
_MAX_EVENT_BYTES = int(
    os.environ.get("QWENPAW_DATA_MAX_EVENT_BYTES", str(1024 * 1024))
)
_HEAD_RATIO = 0.7


def _cap_text(value: str, limit: int) -> str | None:
    """Return a head+tail truncation, or None when ``value`` fits."""
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return None
    head_bytes = max(0, int(limit * _HEAD_RATIO))
    tail_bytes = max(0, limit - head_bytes)
    head = raw[:head_bytes].decode("utf-8", "ignore")
    tail = raw[-tail_bytes:].decode("utf-8", "ignore") if tail_bytes else ""
    omitted = len(raw) - head_bytes - tail_bytes
    return f"{head}\n…[truncated {omitted} bytes]…\n{tail}"


def _cap_block(block: dict[str, Any], limit: int) -> bool:
    """Truncate oversized text inside one content block; report a change."""
    if not isinstance(block, dict):
        return False
    block_type = block.get("type")
    if block_type == "data":
        data = block.get("data")
        if not isinstance(data, dict):
            return False
        changed = False
        for key in ("output", "arguments"):
            value = data.get(key)
            if not isinstance(value, str):
                continue
            capped = _cap_text(value, limit)
            if capped is not None:
                data[key] = capped
                changed = True
        if changed:
            data["truncated"] = True
        return changed
    for key in ("text", "refusal"):
        value = block.get(key)
        if not isinstance(value, str):
            continue
        capped = _cap_text(value, limit)
        if capped is not None:
            block[key] = capped
            return True
    return False


def _cap_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if _MAX_EVENT_BYTES <= 0:
        return payload
    # Deltas are fragments; truncating one would corrupt the reconstruction
    # performed by the client. Only full snapshots are capped.
    if payload.get("delta"):
        return payload
    obj = payload.get("object")
    changed = False
    if obj == "content":
        changed = _cap_block(payload, _MAX_EVENT_BYTES)
    elif obj == "message":
        blocks = payload.get("content")
        if isinstance(blocks, list):
            for block in blocks:
                if _cap_block(block, _MAX_EVENT_BYTES):
                    changed = True
    if changed:
        logger.warning(
            "event payload exceeded %s bytes and was truncated "
            "(object=%s type=%s msg_id=%s)",
            _MAX_EVENT_BYTES,
            obj,
            payload.get("type"),
            payload.get("msg_id") or payload.get("id"),
        )
    return payload


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
        payload = _cap_event_payload(payload)
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

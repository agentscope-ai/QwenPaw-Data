# -*- coding: utf-8 -*-
"""SSE envelope state machine (ported from QwenPaw, sunk into OutputStream)."""

from __future__ import annotations

import json
import logging
from typing import Any

from agentscope.event import EventBase, EventType

from qwenpaw_data.host.core.stream.output_stream import OutputStream
from qwenpaw_data.host.core.utils.ids import create_id

logger = logging.getLogger(__name__)

_IGNORED_EVENTS = frozenset(
    {
        EventType.REPLY_START.value,
        EventType.REPLY_END.value,
        EventType.MODEL_CALL_START.value,
        # Permission confirmations are resolved inside QwenPawDataAgent but
        # still surface on its reply stream; they are not wire events.
        EventType.REQUIRE_USER_CONFIRM.value,
        EventType.USER_CONFIRM_RESULT.value,
        EventType.USER_INTERRUPT.value,
    }
)


def _media_type_to_block_type(media_type: str | None) -> str:
    if not media_type:
        return "data"
    major = media_type.split("/", 1)[0]
    if major in ("image", "video", "audio"):
        return major
    return "data"


class Envelope:
    """Translate AgentScope events into Console protocol via OutputStream.

    Public surface: begin / translate_event / complete|fail|cancel；
    send_biz_event / send_segment / send_followup。
    """

    def __init__(self, stream: OutputStream) -> None:
        self.stream = stream
        self._msg_seq = 0
        self._usage: dict[str, Any] | None = None
        self._terminal = False

        # Open text blocks: block_id -> {msg_id, seq, text}
        self._text_blocks: dict[str, dict[str, Any]] = {}

        # Open media/data blocks: block_id -> {msg_id, seq, media_type, data_acc}
        self._data_blocks: dict[str, dict[str, Any]] = {}

        # Reasoning blocks keyed by block_id.
        self._reasoning_blocks: dict[str, dict[str, Any]] = {}

        # Tool calls keyed by call_id.
        self._tool_calls: dict[str, dict[str, Any]] = {}

    def _next_seq(self) -> int:
        self._msg_seq += 1
        return self._msg_seq

    async def begin(self) -> None:
        await self.stream.response_created()
        await self.stream.response_in_progress()

    async def complete(self) -> None:
        await self._finalize_open_messages()
        self._terminal = True
        if self._usage is not None:
            await self.stream.response("completed", usage=self._usage)
        else:
            await self.stream.response_completed()

    async def fail(self, *, error: dict[str, Any]) -> None:
        await self._finalize_open_messages()
        self._terminal = True
        await self.stream.response_failed(error=error)

    async def cancel(self) -> None:
        await self._finalize_open_messages()
        self._terminal = True
        await self.stream.response_cancelled()

    # ---- algorithm-side send surface ----

    async def send_biz_event(self, biz_event: dict[str, Any]) -> None:
        """Persist and broadcast one biz_event; re-sends overwrite by event_id."""
        # Algorithm workers run concurrently with the chat main flow:
        # failures are logged, never raised back into the worker. A flush
        # that outlives the turn must not land behind the terminal frame.
        if self._terminal:
            return
        try:
            await self.stream.biz_event(**biz_event)
        except Exception:
            logger.exception(
                "envelope: failed to send biz_event for chat %s",
                self.stream.chat_id,
            )

    async def send_segment(self, segment: dict[str, Any]) -> None:
        """Persist and broadcast one segment; each segment is sent once."""
        if self._terminal:
            return
        try:
            await self.stream.segment(**segment)
        except Exception:
            logger.exception(
                "envelope: failed to send segment for chat %s",
                self.stream.chat_id,
            )

    async def send_followup(self, questions: list[str]) -> None:
        """Persist and broadcast follow-up questions (FollowUpCallback shape)."""
        try:
            await self.stream.followup_generated(questions=questions)
        except Exception:
            logger.exception(
                "envelope: failed to send followup for chat %s",
                self.stream.chat_id,
            )

    async def translate_event(self, event: EventBase) -> None:
        evt_type = event.type
        if hasattr(evt_type, "value"):
            evt_type = evt_type.value

        if evt_type in _IGNORED_EVENTS:
            return
        if evt_type == EventType.HINT_BLOCK.value:
            await self._on_hint_block(event)
            return

        if evt_type == EventType.TEXT_BLOCK_START.value:
            await self._on_text_start(event.block_id)
            return

        if evt_type == EventType.TEXT_BLOCK_DELTA.value:
            await self._on_text_delta(event.block_id, getattr(event, "delta", "") or "")
            return

        if evt_type == EventType.TEXT_BLOCK_END.value:
            await self._on_text_end(event.block_id)
            return

        if evt_type == EventType.THINKING_BLOCK_START.value:
            await self._finalize_open_text_and_data()
            await self._on_reasoning_start(event.block_id)
            return

        if evt_type == EventType.THINKING_BLOCK_DELTA.value:
            block_id = event.block_id
            delta = getattr(event, "delta", "") or ""
            state = self._reasoning_blocks.get(block_id)
            if state is None:
                await self._finalize_open_text_and_data()
                await self._on_reasoning_start(block_id)
                state = self._reasoning_blocks[block_id]
            state["text"] += delta
            if delta:
                await self.stream.text_delta(
                    msg_id=state["msg_id"], index=0, text=delta
                )
            return

        if evt_type == EventType.THINKING_BLOCK_END.value:
            state = self._reasoning_blocks.get(event.block_id)
            if state is None:
                return
            await self.stream.text_end(
                msg_id=state["msg_id"], index=0, text=state["text"]
            )
            await self.stream.message_complete(
                msg_id=state["msg_id"],
                sequence=state["seq"],
                type="reasoning",
                role="assistant",
                source_id=event.block_id,
                content=[
                    {
                        "object": "content",
                        "type": "text",
                        "delta": False,
                        "index": 0,
                        "text": state["text"],
                    }
                ],
            )
            del self._reasoning_blocks[event.block_id]
            return

        if evt_type == EventType.TOOL_CALL_START.value:
            await self._finalize_open_text_and_data()
            call_id = event.tool_call_id
            name = event.tool_call_name
            if not call_id or not name:
                raise ValueError("tool_call_id and tool_call_name are required")
            msg_id = create_id("msg")
            seq = self._next_seq()
            metadata = dict(getattr(event, "metadata", {}) or {}) or None
            await self.stream.message_start(
                msg_id=msg_id,
                sequence=seq,
                type="plugin_call",
                role="assistant",
                source_id=call_id,
                metadata=metadata,
            )
            await self.stream.data_delta(
                msg_id=msg_id,
                index=0,
                data={"call_id": call_id, "name": name, "arguments": ""},
            )
            self._tool_calls[call_id] = {
                "name": name,
                "argument_fragments": [],
                "msg_id": msg_id,
                "seq": seq,
                "output_text_acc": "",
                "output_data_blocks": {},
                "output_msg_id": None,
                "output_seq": None,
                "metadata": metadata,
            }
            return

        if evt_type == EventType.TOOL_CALL_DELTA.value:
            state = self._tool_calls.get(event.tool_call_id)
            if state is None:
                return
            argument_delta = getattr(event, "delta", "") or ""
            if not argument_delta:
                return
            state["argument_fragments"].append(argument_delta)
            await self.stream.data_delta(
                msg_id=state["msg_id"],
                index=0,
                data={"arguments": argument_delta},
            )
            return

        if evt_type == EventType.TOOL_CALL_END.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None:
                return
            arguments = "".join(state.pop("argument_fragments", []))
            data = {
                "call_id": call_id,
                "name": state["name"],
                "arguments": arguments,
            }
            await self.stream.data_end(
                msg_id=state["msg_id"], index=0, data=data
            )
            await self.stream.message_complete(
                msg_id=state["msg_id"],
                sequence=state["seq"],
                type="plugin_call",
                role="assistant",
                source_id=call_id,
                content=[
                    {
                        "object": "content",
                        "type": "data",
                        "delta": False,
                        "data": data,
                    }
                ],
                metadata=state["metadata"],
            )
            return

        if evt_type == EventType.TOOL_RESULT_START.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None:
                state = {
                    "name": event.tool_call_name,
                    "argument_fragments": [],
                    "msg_id": None,
                    "seq": None,
                    "output_text_acc": "",
                    "output_data_blocks": {},
                    "output_msg_id": None,
                    "output_seq": None,
                }
                self._tool_calls[call_id] = state
            out_msg_id = create_id("msg")
            out_seq = self._next_seq()
            state["output_msg_id"] = out_msg_id
            state["output_seq"] = out_seq
            state["output_text_acc"] = ""
            state["output_data_blocks"] = {}
            await self.stream.message_start(
                msg_id=out_msg_id,
                sequence=out_seq,
                type="plugin_call_output",
                role="tool",
                source_id=call_id,
            )
            await self.stream.append(
                {
                    "object": "content",
                    "type": "data",
                    "delta": False,
                    "msg_id": out_msg_id,
                    "index": 0,
                    "data": {
                        "call_id": call_id,
                        "name": state["name"],
                        "output": "",
                    },
                }
            )
            return

        if evt_type == EventType.TOOL_RESULT_TEXT_DELTA.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None or state.get("output_msg_id") is None:
                return
            state["output_text_acc"] += getattr(event, "delta", "") or ""
            await self._emit_tool_result_content(call_id, state, delta=False)
            return

        if evt_type == EventType.TOOL_RESULT_DATA_DELTA.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None or state.get("output_msg_id") is None:
                return
            self._accumulate_tool_data_block(event, state)
            await self._emit_tool_result_content(call_id, state, delta=False)
            return

        if evt_type == EventType.TOOL_RESULT_END.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None:
                return
            tool_state = getattr(event, "state", None)
            if hasattr(tool_state, "value"):
                tool_state = tool_state.value
            data = self._tool_result_payload(
                call_id, state, tool_state=tool_state
            )
            out_msg_id = state.get("output_msg_id")
            out_seq = state.get("output_seq")
            if out_msg_id is None or out_seq is None:
                out_msg_id = create_id("msg")
                out_seq = self._next_seq()
                await self.stream.message_start(
                    msg_id=out_msg_id,
                    sequence=out_seq,
                    type="plugin_call_output",
                    role="tool",
                    source_id=call_id,
                )
            await self.stream.data_end(
                msg_id=out_msg_id, index=0, data=data
            )
            await self.stream.message_complete(
                msg_id=out_msg_id,
                sequence=out_seq,
                type="plugin_call_output",
                role="tool",
                source_id=call_id,
                content=[
                    {
                        "object": "content",
                        "type": "data",
                        "delta": False,
                        "data": data,
                    }
                ],
            )
            state["output_msg_id"] = None
            state["output_seq"] = None
            return

        if evt_type == EventType.DATA_BLOCK_START.value:
            await self._on_data_start(
                event.block_id, getattr(event, "media_type", "") or ""
            )
            return

        if evt_type == EventType.DATA_BLOCK_DELTA.value:
            state = self._data_blocks.get(event.block_id)
            if state is None:
                return
            state["data_acc"] += getattr(event, "data", "") or ""
            return

        if evt_type == EventType.DATA_BLOCK_END.value:
            await self._on_data_end(event.block_id)
            return

        if evt_type == EventType.MODEL_CALL_END.value:
            self._usage = {
                "input_tokens": getattr(event, "input_tokens", 0) or 0,
                "output_tokens": getattr(event, "output_tokens", 0) or 0,
            }
            return

        if evt_type == EventType.EXCEED_MAX_ITERS.value:
            await self._on_exceed_max_iters(
                getattr(event, "name", "agent") or "agent"
            )
            return

        logger.error("envelope: unsupported agent event %r", event)
        raise ValueError(f"unsupported agent event: {evt_type}")

    async def _on_text_start(self, block_id: str) -> None:
        if not block_id:
            raise ValueError("block_id is required")
        if block_id in self._text_blocks:
            raise ValueError(f"text block already open: {block_id}")
        msg_id = create_id("msg")
        seq = self._next_seq()
        self._text_blocks[block_id] = {
            "msg_id": msg_id,
            "seq": seq,
            "text": "",
        }
        await self.stream.message_start(
            msg_id=msg_id,
            sequence=seq,
            type="message",
            role="assistant",
            source_id=block_id,
        )

    async def _on_text_delta(self, block_id: str, delta: str) -> None:
        state = self._text_blocks.get(block_id)
        if state is None:
            await self._on_text_start(block_id)
            state = self._text_blocks[block_id]
        state["text"] += delta
        if delta:
            await self.stream.text_delta(
                msg_id=state["msg_id"], index=0, text=delta
            )

    async def _on_text_end(self, block_id: str) -> None:
        state = self._text_blocks.get(block_id)
        if state is None:
            return
        await self.stream.text_end(
            msg_id=state["msg_id"], index=0, text=state["text"]
        )
        await self.stream.message_complete(
            msg_id=state["msg_id"],
            sequence=state["seq"],
            type="message",
            role="assistant",
            source_id=block_id,
            content=[
                {
                    "object": "content",
                    "type": "text",
                    "delta": False,
                    "index": 0,
                    "text": state["text"],
                }
            ],
        )
        del self._text_blocks[block_id]

    async def _on_data_start(self, block_id: str, media_type: str) -> None:
        if not block_id:
            raise ValueError("block_id is required")
        if block_id in self._data_blocks:
            raise ValueError(f"data block already open: {block_id}")
        msg_id = create_id("msg")
        seq = self._next_seq()
        self._data_blocks[block_id] = {
            "msg_id": msg_id,
            "seq": seq,
            "media_type": media_type,
            "data_acc": "",
        }
        await self.stream.message_start(
            msg_id=msg_id,
            sequence=seq,
            type="message",
            role="assistant",
            source_id=block_id,
        )

    async def _on_data_end(self, block_id: str) -> None:
        state = self._data_blocks.get(block_id)
        if state is None:
            return
        media_type = state["media_type"]
        b64_data = state["data_acc"]
        major = media_type.split("/", 1)[0] if media_type else ""
        if major == "audio":
            fmt = media_type.split("/", 1)[1] if "/" in media_type else ""
            content: dict[str, Any] = {
                "object": "content",
                "type": "audio",
                "delta": False,
                "index": 0,
                "data": b64_data,
                "format": fmt,
            }
        elif major == "video":
            content = {
                "object": "content",
                "type": "video",
                "delta": False,
                "index": 0,
                "video_url": f"data:{media_type};base64,{b64_data}",
            }
        else:
            content = {
                "object": "content",
                "type": "image",
                "delta": False,
                "index": 0,
                "image_url": f"data:{media_type};base64,{b64_data}",
            }
        content["msg_id"] = state["msg_id"]
        await self.stream.append(content)
        await self.stream.message_complete(
            msg_id=state["msg_id"],
            sequence=state["seq"],
            type="message",
            role="assistant",
            source_id=block_id,
            content=[{k: v for k, v in content.items() if k != "msg_id"}],
        )
        del self._data_blocks[block_id]

    async def _finalize_open_text_and_data(self) -> None:
        for block_id in list(self._text_blocks):
            await self._on_text_end(block_id)
        for block_id in list(self._data_blocks):
            await self._on_data_end(block_id)

    async def _on_reasoning_start(self, block_id: str) -> None:
        if not block_id:
            raise ValueError("block_id is required")
        msg_id = create_id("msg")
        seq = self._next_seq()
        self._reasoning_blocks[block_id] = {
            "msg_id": msg_id,
            "seq": seq,
            "text": "",
        }
        await self.stream.message_start(
            msg_id=msg_id,
            sequence=seq,
            type="reasoning",
            role="assistant",
            source_id=block_id,
        )

    def _accumulate_tool_data_block(
        self, event: EventBase, state: dict[str, Any]
    ) -> None:
        block_id = event.block_id
        media_type = getattr(event, "media_type", None)
        block_type = _media_type_to_block_type(media_type)
        blocks_dict: dict[str, Any] = state["output_data_blocks"]
        url = getattr(event, "url", None)
        b64 = getattr(event, "data", None)
        if block_id in blocks_dict:
            existing = blocks_dict[block_id]
            if b64:
                existing["source"]["data"] = (
                    existing["source"].get("data", "") + b64
                )
            return
        source: dict[str, Any] = {}
        if url:
            source = {
                "type": "url",
                "url": url,
                "media_type": media_type or "",
            }
        elif b64:
            source = {
                "type": "base64",
                "data": b64,
                "media_type": media_type or "",
            }
        blocks_dict[block_id] = {"type": block_type, "source": source}

    def _tool_result_payload(
        self,
        call_id: str,
        state: dict[str, Any],
        *,
        tool_state: Any = None,
    ) -> dict[str, Any]:
        blocks_dict: dict[str, Any] = state.get("output_data_blocks") or {}
        text_acc: str = state.get("output_text_acc") or ""
        if blocks_dict:
            output_blocks: list[dict[str, Any]] = list(blocks_dict.values())
            if text_acc:
                output_blocks.append({"type": "text", "text": text_acc})
            tool_output: Any = json.dumps(output_blocks, ensure_ascii=False)
        else:
            tool_output = text_acc
        data: dict[str, Any] = {
            "call_id": call_id,
            "name": state["name"],
            "output": tool_output,
        }
        if tool_state is not None:
            data["state"] = tool_state
        return data

    async def _emit_tool_result_content(
        self,
        call_id: str,
        state: dict[str, Any],
        *,
        delta: bool,
    ) -> None:
        data = self._tool_result_payload(call_id, state)
        await self.stream.append(
            {
                "object": "content",
                "type": "data",
                "delta": delta,
                "msg_id": state["output_msg_id"],
                "index": 0,
                "data": data,
            }
        )

    async def _on_hint_block(self, event: EventBase) -> None:
        block_id = getattr(event, "block_id", None) or ""
        if not block_id:
            raise ValueError("block_id is required")
        hint = getattr(event, "hint", None)
        if not isinstance(hint, str):
            raise ValueError(f"hint must be str, got {type(hint)!r}")

        await self._finalize_open_text_and_data()
        msg_id = create_id("msg")
        seq = self._next_seq()
        source = getattr(event, "source", None)
        comments = (getattr(event, "metadata", None) or {}).get(
            "artifact_comments"
        ) or []
        metadata: dict[str, Any] | None = {}
        if source is not None:
            metadata["source"] = source
        if comments:
            metadata["artifact_comments"] = list(comments)
        if not metadata:
            metadata = None
        content = [
            {
                "object": "content",
                "type": "text",
                "delta": False,
                "index": 0,
                "text": hint,
            }
        ]
        await self.stream.message_start(
            msg_id=msg_id,
            sequence=seq,
            type="hint",
            role="user",
            source_id=block_id,
            metadata=metadata,
        )
        await self.stream.text_end(msg_id=msg_id, index=0, text=hint)
        await self.stream.message_complete(
            msg_id=msg_id,
            sequence=seq,
            type="hint",
            role="user",
            source_id=block_id,
            content=content,
            metadata=metadata,
        )

    async def _on_exceed_max_iters(self, agent_name: str) -> None:
        await self._finalize_open_text_and_data()
        msg_id = create_id("msg")
        seq = self._next_seq()
        text = (
            f"[Warning] Agent '{agent_name}' has reached "
            f"the maximum number of iterations."
        )
        await self.stream.message_start(
            msg_id=msg_id,
            sequence=seq,
            type="message",
            role="assistant",
        )
        await self.stream.text_end(msg_id=msg_id, index=0, text=text)
        await self.stream.message_complete(
            msg_id=msg_id,
            sequence=seq,
            type="message",
            role="assistant",
            content=[
                {
                    "object": "content",
                    "type": "text",
                    "delta": False,
                    "index": 0,
                    "text": text,
                }
            ],
        )

    async def _finalize_open_messages(self) -> None:
        for call_id, state in list(self._tool_calls.items()):
            if state.get("output_msg_id") is not None:
                data = self._tool_result_payload(call_id, state)
                await self.stream.data_end(
                    msg_id=state["output_msg_id"], index=0, data=data
                )
                await self.stream.message_complete(
                    msg_id=state["output_msg_id"],
                    sequence=state["output_seq"],
                    type="plugin_call_output",
                    role="tool",
                    source_id=call_id,
                    content=[
                        {
                            "object": "content",
                            "type": "data",
                            "delta": False,
                            "data": data,
                        }
                    ],
                )
                state["output_msg_id"] = None
                state["output_seq"] = None
            elif (
                state.get("msg_id") is not None
                and "argument_fragments" in state
            ):
                arguments = "".join(state.get("argument_fragments") or [])
                data = {
                    "call_id": call_id,
                    "name": state["name"],
                    "arguments": arguments,
                }
                await self.stream.data_end(
                    msg_id=state["msg_id"], index=0, data=data
                )
                await self.stream.message_complete(
                    msg_id=state["msg_id"],
                    sequence=state["seq"],
                    type="plugin_call",
                    role="assistant",
                    source_id=call_id,
                    content=[
                        {
                            "object": "content",
                            "type": "data",
                            "delta": False,
                            "data": data,
                        }
                    ],
                )
                del state["argument_fragments"]

        for block_id, state in list(self._reasoning_blocks.items()):
            await self.stream.text_end(
                msg_id=state["msg_id"], index=0, text=state["text"]
            )
            await self.stream.message_complete(
                msg_id=state["msg_id"],
                sequence=state["seq"],
                type="reasoning",
                role="assistant",
                source_id=block_id,
                content=[
                    {
                        "object": "content",
                        "type": "text",
                        "delta": False,
                        "index": 0,
                        "text": state["text"],
                    }
                ],
            )
            del self._reasoning_blocks[block_id]

        await self._finalize_open_text_and_data()

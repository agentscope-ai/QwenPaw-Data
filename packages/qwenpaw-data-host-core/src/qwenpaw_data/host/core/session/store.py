"""Small JSON-backed session store for standalone QwenPaw Data."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.session._locking import file_lock as _session_file_lock
from qwenpaw_data.host.core.session._locking import write_atomic


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", name)[:120] or "_"


def _timestamp(value: Any = None) -> str:
    if isinstance(value, str) and value:
        text = value
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        except ValueError:
            return value
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _content_blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": json.dumps(content, ensure_ascii=False)}]

    blocks: list[dict] = []
    for item in content:
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue

        block_type = item.get("type")
        if block_type == "text":
            blocks.append({"type": "text", "text": str(item.get("text") or "")})
        elif block_type == "thinking":
            blocks.append(
                {"type": "thinking", "thinking": str(item.get("thinking") or "")},
            )
        elif block_type in {"tool_call", "tool_use"}:
            raw_input = item.get("raw_input") or item.get("input") or ""
            blocks.append(
                {
                    "type": "tool_use",
                    "id": item.get("id") or item.get("tool_call_id") or "",
                    "name": item.get("name") or item.get("tool_call_name") or "",
                    "input": _parse_tool_input(raw_input),
                    "raw_input": raw_input
                    if isinstance(raw_input, str)
                    else json.dumps(
                        raw_input,
                        ensure_ascii=False,
                    ),
                },
            )
        elif block_type == "tool_result":
            blocks.append(
                {
                    "type": "tool_result",
                    "id": item.get("id") or item.get("tool_call_id") or "",
                    "name": item.get("name") or item.get("tool_call_name") or "",
                    "output": _content_blocks(item.get("output") or []),
                },
            )
        else:
            blocks.append(item)
    return blocks


def _parse_tool_input(raw_input: Any) -> Any:
    if not isinstance(raw_input, str):
        return raw_input
    if not raw_input.strip():
        return {}
    try:
        return json.loads(raw_input)
    except json.JSONDecodeError:
        return raw_input


def _tool_output_blocks(event: dict) -> list[dict]:
    response = event.get("response")
    if isinstance(response, dict):
        return _content_blocks(response.get("content") or [])
    chunk = event.get("chunk")
    if isinstance(chunk, dict):
        return _content_blocks(chunk.get("content") or [])
    delta = event.get("delta")
    if delta is not None:
        return [{"type": "text", "text": str(delta)}]
    return []


def _response_metadata(event: dict) -> dict:
    response = event.get("response")
    if not isinstance(response, dict):
        return {}
    metadata = response.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _message_metadata(entry: dict, event: dict | None = None) -> dict:
    metadata = _response_metadata(event or {})
    metadata.update(
        {
            "graph_id": entry.get("graph_id"),
            "node_id": entry.get("node_id"),
        }
    )
    return metadata


def _memory_message_from_trace(entry: dict, sequence: int) -> dict | None:
    event = entry.get("event")
    if not isinstance(event, dict):
        return None

    if event.get("role") in {"user", "assistant", "system"}:
        message = {
            "id": event.get("id") or f"msg_{sequence}",
            "name": event.get("name") or event.get("role"),
            "role": event["role"],
            "content": _content_blocks(event.get("content") or []),
            "metadata": event.get("metadata") or {},
            "timestamp": _timestamp(event.get("created_at")),
        }
        message["metadata"].update(_message_metadata(entry))
        return message

    event_type = str(event.get("type") or "")
    if event_type == "ToolCallExecutionDeltaEvent":
        raw_input = event.get("delta") or ""
        return {
            "id": f"msg_tool_{event.get('tool_call_id') or sequence}",
            "name": "QwenPaw Data",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": event.get("tool_call_id") or "",
                    "name": event.get("tool_call_name") or "",
                    "input": _parse_tool_input(raw_input),
                    "raw_input": raw_input,
                },
            ],
            "metadata": _message_metadata(entry, event),
            "timestamp": _timestamp(event.get("created_at")),
        }

    if event_type == "ToolResultExecutionEndEvent":
        return {
            "id": f"msg_result_{event.get('tool_call_id') or sequence}",
            "name": "system",
            "role": "system",
            "content": [
                {
                    "type": "tool_result",
                    "id": event.get("tool_call_id") or "",
                    "name": event.get("tool_call_name") or "",
                    "output": _tool_output_blocks(event),
                },
            ],
            "metadata": _message_metadata(entry, event),
            "timestamp": _timestamp(event.get("created_at")),
        }

    return None


def _memory_content(state: dict) -> list:
    agent = state.setdefault("agent", {})
    memory = agent.setdefault("memory", {})
    content = memory.setdefault("content", [])
    if not isinstance(content, list):
        content = []
        memory["content"] = content
    return content


class JSONSessionStore:
    """Async-compatible JSON session store used by the default host."""

    def __init__(self, console_root: Path) -> None:
        self.console_root = Path(console_root).expanduser().resolve()

    def get_path(self, session_id: str, user_id: str = "") -> Path:
        safe_user = _sanitize(user_id or "default")
        safe_sid = _sanitize(session_id)
        return self.console_root / f"{safe_user}_{safe_sid}.json"

    @staticmethod
    def _write_atomic(path: Path, state: dict) -> None:
        write_atomic(path, state)

    async def get_session_state_dict(
        self,
        session_id: str,
        user_id: str = "",
    ) -> dict:
        path = self.get_path(session_id, user_id)
        return await asyncio.to_thread(self._read_locked, path)

    @staticmethod
    def _read_locked(path: Path) -> dict:
        with _session_file_lock(path):
            if not path.exists():
                return {}
            return json.loads(path.read_text(encoding="utf-8"))

    async def update_session_state(
        self,
        session_id: str,
        key: str,
        value: Any,
        *,
        user_id: str = "",
        create_if_not_exist: bool = False,
    ) -> None:
        path = self.get_path(session_id, user_id)
        await asyncio.to_thread(
            self._update_locked,
            path,
            key,
            value,
            create_if_not_exist,
        )

    def _update_locked(
        self,
        path: Path,
        key: str,
        value: Any,
        create_if_not_exist: bool,
    ) -> None:
        with _session_file_lock(path):
            if not path.exists() and not create_if_not_exist:
                raise FileNotFoundError(path)

            state = {}
            if path.exists():
                state = json.loads(path.read_text(encoding="utf-8"))

            target = state
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value

            self._write_atomic(path, state)

    async def append_trace_event(
        self,
        session_id: str,
        entry: dict,
        *,
        user_id: str = "",
        create_if_not_exist: bool = True,
    ) -> None:
        path = self.get_path(session_id, user_id)
        await asyncio.to_thread(
            self._append_trace_locked,
            path,
            entry,
            create_if_not_exist,
        )

    def _append_trace_locked(
        self,
        path: Path,
        entry: dict,
        create_if_not_exist: bool,
    ) -> None:
        with _session_file_lock(path):
            if not path.exists() and not create_if_not_exist:
                raise FileNotFoundError(path)

            state = {}
            if path.exists():
                state = json.loads(path.read_text(encoding="utf-8"))

            safe_entry = json.loads(
                json.dumps(entry, ensure_ascii=False, default=str),
            )
            content = _memory_content(state)
            message = _memory_message_from_trace(safe_entry, len(content) + 1)
            if message is None:
                return
            content.append([message, []])

            self._write_atomic(path, state)

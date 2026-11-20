# -*- coding: utf-8 -*-
"""Convert Console chat_events into extract_trace message blocks."""

from __future__ import annotations

import json
from typing import Any


def wrap_trace(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"messages": messages}


def console_messages_to_trace(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Console plugin_call / plugin_call_output rows to tool_use blocks."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("status") or "completed") not in ("completed", "complete", ""):
            continue
        msg_type = str(msg.get("type") or "message")
        role = str(msg.get("role") or "")
        if msg_type == "message":
            text = _text_of(msg)
            if role == "user" and text:
                out.append(_text_msg("user", text))
            elif role == "assistant" and text:
                out.append(_text_msg("assistant", text))
        elif msg_type == "hint":
            text = _text_of(msg)
            if text:
                out.append(_text_msg("user", f"[hint] {text}"))
        elif msg_type == "plugin_call":
            name, arguments, call_id = _plugin_call(msg)
            if name:
                out.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id or name,
                                "name": name,
                                "input": arguments,
                            }
                        ],
                    }
                )
        elif msg_type == "plugin_call_output":
            name, output, call_id, failed = _plugin_output(msg)
            if name or output:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "id": call_id or name,
                    "name": name,
                    "output": output,
                }
                if failed:
                    block["state"] = "error"
                out.append({"role": "tool", "content": [block]})
    return out


def ensure_user_input(
    messages: list[dict[str, Any]], user_input: str
) -> list[dict[str, Any]]:
    """Prepend chat.user_input when that turn is missing from events."""
    text = (user_input or "").strip()
    if not text:
        return list(messages)
    for msg in messages:
        if (msg.get("role") or "") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            joined = "\n".join(
                str(b.get("text") or "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if joined == text:
                return list(messages)
        elif isinstance(content, str) and content.strip() == text:
            return list(messages)
    return [_text_msg("user", text), *messages]


def _text_msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _text_of(msg: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text") or "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _plugin_call(msg: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    for block in msg.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "data":
            continue
        data = block.get("data")
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "")
        arguments = data.get("arguments")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                arguments = parsed if isinstance(parsed, dict) else {"raw": arguments}
            except (TypeError, ValueError):
                arguments = {"raw": arguments}
        elif not isinstance(arguments, dict):
            arguments = {}
        return name, arguments, str(data.get("call_id") or "")
    return "", {}, ""


def _plugin_output(msg: dict[str, Any]) -> tuple[str, str, str, bool]:
    failed = str(msg.get("status") or "").lower() in {"error", "failed"}
    for block in msg.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "data":
            continue
        data = block.get("data")
        if not isinstance(data, dict):
            continue
        output = data.get("output")
        if not isinstance(output, str):
            output = _text_of(msg)
        state = str(data.get("state") or data.get("exec_status") or "").lower()
        if state in {"error", "failed"}:
            failed = True
        return (
            str(data.get("name") or ""),
            output,
            str(data.get("call_id") or ""),
            failed,
        )
    return "", _text_of(msg), "", failed

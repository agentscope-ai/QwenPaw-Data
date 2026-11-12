# -*- coding: utf-8 -*-
"""Convert raw Console chat messages into fork replay history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from qwenpaw_data.host.core.algo.skill_evolution.settings import MAX_TRACE_CHARS

# Keep recent turns verbatim; older turns collapse into short digests.
_DEFAULT_TAIL_TURNS = 24
_DIGEST_USER_CHARS = 300
_DIGEST_ASSISTANT_CHARS = 200
_TOOL_ERROR_STATES = frozenset({"error", "failed"})
_TOOL_ERROR_PREVIEW_CHARS = 500


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    """One role/content turn for the evolution fork."""

    role: str
    content: str


def messages_to_history(
    messages: list[dict[str, Any]],
    *,
    chat_user_inputs: list[str] | None = None,
    max_chars: int = MAX_TRACE_CHARS,
    tail_turns: int = _DEFAULT_TAIL_TURNS,
) -> list[HistoryTurn]:
    """Convert Console message payloads into ordered history turns.

    Prepends each chat's ``user_input`` when that text is missing as a user
    turn. Oversized histories keep the recent ``tail_turns`` verbatim and
    collapse older turns into a short USER/ASSISTANT digest.
    """
    turns = _convert_messages(messages)
    if chat_user_inputs:
        turns = _ensure_user_inputs(turns, chat_user_inputs, messages)
    return digest_history(turns, max_chars=max_chars, tail_turns=tail_turns)


def digest_history(
    turns: list[HistoryTurn],
    *,
    max_chars: int = MAX_TRACE_CHARS,
    tail_turns: int = _DEFAULT_TAIL_TURNS,
) -> list[HistoryTurn]:
    """Keep the recent tail; digest older turns when over ``max_chars``."""
    if not turns:
        return []
    total = sum(len(t.content) for t in turns)
    if total <= max_chars or len(turns) <= tail_turns:
        return list(turns)

    keep_n = min(tail_turns, len(turns))
    # Avoid starting the verbatim tail mid tool-output-only stretch: pull a
    # user/assistant text turn to the front when possible.
    while keep_n < len(turns) and turns[-keep_n].role not in ("user", "assistant"):
        keep_n += 1
        if keep_n >= len(turns):
            return list(turns)

    keep = turns[-keep_n:]
    old = turns[:-keep_n]
    lines: list[str] = []
    for turn in old:
        text = turn.content.replace("\n", " ").strip()
        if not text:
            continue
        if turn.role == "user":
            lines.append(f"USER: {text[:_DIGEST_USER_CHARS]}")
        elif turn.role == "assistant":
            lines.append(f"ASSISTANT: {text[:_DIGEST_ASSISTANT_CHARS]}")
        elif turn.role == "tool":
            lines.append(f"TOOL: {text[:_DIGEST_ASSISTANT_CHARS]}")
    digest = HistoryTurn(
        role="user",
        content=(
            "[Earlier conversation digest — older turns summarised. "
            "Recent turns follow verbatim below.]\n" + "\n".join(lines)
        ),
    )
    return [digest, *keep]


def _convert_messages(messages: list[dict[str, Any]]) -> list[HistoryTurn]:
    turns: list[HistoryTurn] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("object") not in (None, "message"):
            # content-delta rows are not completed messages
            if msg.get("object") == "content":
                continue
        status = str(msg.get("status") or "completed")
        if status not in ("completed", "complete", ""):
            continue
        msg_type = str(msg.get("type") or "message")
        role = str(msg.get("role") or "")
        text = _message_text(msg)
        if msg_type == "message":
            if role == "user" and text:
                turns.append(HistoryTurn(role="user", content=text))
            elif role == "assistant" and text:
                turns.append(HistoryTurn(role="assistant", content=text))
        elif msg_type == "reasoning":
            if text:
                turns.append(
                    HistoryTurn(role="assistant", content=f"[reasoning] {text}")
                )
        elif msg_type == "hint":
            if text:
                turns.append(HistoryTurn(role="user", content=f"[hint] {text}"))
        elif msg_type == "plugin_call":
            name, arguments = _plugin_call_parts(msg)
            if name:
                arg_preview = arguments[:500] if arguments else ""
                turns.append(
                    HistoryTurn(
                        role="assistant",
                        content=f"[tool_call] {name}({arg_preview})",
                    )
                )
        elif msg_type == "plugin_call_output":
            name, output, state = _plugin_output_parts(msg)
            preview = (output or text).strip()
            if not preview or not _is_tool_error(state):
                continue
            label = name or "tool"
            turns.append(
                HistoryTurn(
                    role="tool",
                    content=(
                        f"[tool_error] {label}: {preview[:_TOOL_ERROR_PREVIEW_CHARS]}"
                    ),
                )
            )
    return turns


def _ensure_user_inputs(
    turns: list[HistoryTurn],
    chat_user_inputs: list[str],
    messages: list[dict[str, Any]],
) -> list[HistoryTurn]:
    """Ensure each chat's user_input appears as a user turn when missing."""
    # Group converted turns is hard after flatten; re-scan per chat instead.
    by_chat: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for msg in messages:
        cid = str(msg.get("_chat_id") or "")
        if not cid:
            continue
        if cid not in by_chat:
            by_chat[cid] = []
            order.append(cid)
        by_chat[cid].append(msg)

    if not order:
        # Fallback: prepend first missing user_input once.
        existing = {t.content.strip() for t in turns if t.role == "user"}
        out = list(turns)
        for ui in chat_user_inputs:
            text = (ui or "").strip()
            if text and text not in existing:
                out.insert(0, HistoryTurn(role="user", content=text))
                existing.add(text)
        return out

    rebuilt: list[HistoryTurn] = []
    for cid in order:
        batch = by_chat[cid]
        ui = ""
        for msg in batch:
            ui = str(msg.get("_chat_user_input") or ui)
        chunk = _convert_messages(batch)
        text = ui.strip()
        if text and not any(
            t.role == "user" and t.content.strip() == text for t in chunk
        ):
            rebuilt.append(HistoryTurn(role="user", content=text))
        rebuilt.extend(chunk)
    return rebuilt


def _message_text(msg: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype == "text":
            t = str(block.get("text") or "")
            if t:
                parts.append(t)
        elif btype == "data":
            data = block.get("data")
            if isinstance(data, dict):
                for key in ("output", "text", "arguments"):
                    val = data.get(key)
                    if isinstance(val, str) and val:
                        parts.append(val)
                        break
                else:
                    try:
                        parts.append(json.dumps(data, ensure_ascii=False)[:500])
                    except (TypeError, ValueError):
                        pass
    return "\n".join(parts).strip()


def _plugin_call_parts(msg: dict[str, Any]) -> tuple[str, str]:
    for block in msg.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "data":
            continue
        data = block.get("data")
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "")
        arguments = data.get("arguments")
        if isinstance(arguments, dict):
            try:
                arguments = json.dumps(arguments, ensure_ascii=False)
            except (TypeError, ValueError):
                arguments = str(arguments)
        return name, str(arguments or "")
    return "", ""


def _plugin_output_parts(msg: dict[str, Any]) -> tuple[str, str, str]:
    for block in msg.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "data":
            continue
        data = block.get("data")
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "")
        output = data.get("output")
        if not isinstance(output, str):
            output = _message_text(msg)
        return name, output, str(data.get("state") or "")
    return "", _message_text(msg), ""


def _is_tool_error(state: str) -> bool:
    return state.strip().lower() in _TOOL_ERROR_STATES


__all__ = [
    "HistoryTurn",
    "digest_history",
    "messages_to_history",
]

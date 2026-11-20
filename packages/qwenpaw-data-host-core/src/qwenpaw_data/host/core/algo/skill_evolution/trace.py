# -*- coding: utf-8 -*-
"""Load a finished chat's biztrace into evolution context."""

from __future__ import annotations

import json
from typing import Any

from qwenpaw_data.host.core.algo.skill_evolution.models import TraceContext
from qwenpaw_data.host.core.algo.skill_evolution.settings import MAX_TRACE_CHARS


def build_trace_context(
    *,
    chat_id: str,
    segments: list[dict[str, Any]],
    biz_events: list[dict[str, Any]],
    feedback_texts: list[str] | None = None,
    plan_edit_pending: bool = False,
    max_chars: int = MAX_TRACE_CHARS,
) -> TraceContext:
    skills_used = _extract_skills(segments, biz_events)
    segments_text = _format_segments(segments)
    events_text = _format_biz_events(biz_events)
    budget = max(1000, int(max_chars))
    half = budget // 2
    return TraceContext(
        chat_id=chat_id,
        segments_text=_clip(segments_text, half),
        biz_events_text=_clip(events_text, half),
        skills_used=skills_used,
        feedback_texts=list(feedback_texts or []),
        plan_edit_note="本 Chat 存在用户计划修改信号" if plan_edit_pending else "",
    )


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…(truncated)"


def _extract_skills(
    segments: list[dict[str, Any]], biz_events: list[dict[str, Any]]
) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        n = name.strip()
        if not n or n in seen:
            return
        seen.add(n)
        found.append(n)

    for seg in segments:
        for key in ("title", "behavior", "conclusion", "input"):
            val = seg.get(key)
            if isinstance(val, str):
                for token in _skillish_tokens(val):
                    add(token)
        art = seg.get("artifact")
        if isinstance(art, list):
            blob = json.dumps(art, ensure_ascii=False)
            for token in _skillish_tokens(blob):
                add(token)
    for ev in biz_events:
        for key in ("title", "summary", "caption", "name", "skill"):
            val = ev.get(key)
            if isinstance(val, str):
                for token in _skillish_tokens(val):
                    add(token)
        meta = ev.get("metadata") or ev.get("payload") or {}
        if isinstance(meta, dict):
            for key in ("skill", "skill_name", "skills"):
                val = meta.get(key)
                if isinstance(val, str):
                    add(val)
                elif isinstance(val, list):
                    for item in val:
                        if isinstance(item, str):
                            add(item)
    return found


def _skillish_tokens(text: str) -> list[str]:
    # Heuristic: kebab/snake tokens that look like skill names in prose.
    out: list[str] = []
    for raw in text.replace(",", " ").replace("、", " ").split():
        token = raw.strip("`'\"[](){}:;")
        if not token or len(token) < 3 or len(token) > 64:
            continue
        if "-" in token or "_" in token:
            out.append(token)
    return out


def _format_segments(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, seg in enumerate(segments, 1):
        title = str(seg.get("title") or f"segment-{i}")
        behavior = str(seg.get("behavior") or "").strip()
        conclusion = str(seg.get("conclusion") or "").strip()
        lines.append(f"{i}. {title}")
        if behavior:
            lines.append(f"   行为: {behavior}")
        if conclusion:
            lines.append(f"   结论: {conclusion}")
    return "\n".join(lines)


def _format_biz_events(events: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, ev in enumerate(events[:80], 1):
        title = str(
            ev.get("title")
            or ev.get("caption")
            or ev.get("summary")
            or ev.get("type")
            or f"event-{i}"
        )
        lines.append(f"{i}. {title}")
    return "\n".join(lines)


__all__ = ["build_trace_context"]

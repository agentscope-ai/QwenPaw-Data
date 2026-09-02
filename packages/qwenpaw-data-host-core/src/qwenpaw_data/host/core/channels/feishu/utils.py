# -*- coding: utf-8 -*-
"""Feishu channel helpers: text parsing, mention stripping, file typing."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional


def extract_json_key(content: Optional[str], *keys: str) -> Optional[str]:
    if not content:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    for k in keys:
        v = data.get(k) or data.get(k.replace("_", "").lower())
        if v:
            return str(v).strip()
    return None


def collect_text_parts(blocks: list[Any], parts: list[str]) -> None:
    """Recursively collect text from post/interactive blocks."""
    for block in blocks:
        if isinstance(block, dict):
            tag = block.get("tag") or ""
            # Elements that carry a text/content field, e.g. text / unescape / lark_md
            for field in ("text", "content"):
                val = block.get(field)
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())
                elif isinstance(val, dict):
                    inner = val.get("text") or val.get("content")
                    if isinstance(inner, str) and inner.strip():
                        parts.append(inner.strip())
            children = block.get("children") or block.get("elements") or []
            if isinstance(children, list):
                collect_text_parts(children, parts)
        elif isinstance(block, list):
            collect_text_parts(block, parts)


def extract_post_text(content: Optional[str]) -> Optional[str]:
    if not content:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    parts: list[str] = []
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())
    blocks = data.get("content") or []
    if not blocks:
        body = data.get("body")
        if isinstance(body, dict):
            blocks = body.get("content") or []
    if isinstance(blocks, list):
        collect_text_parts(blocks, parts)
    return " ".join(parts) if parts else None


def strip_mention_placeholders(text: str) -> str:
    """Strip @_user_N / @_all mention placeholders from Feishu text."""
    return re.sub(r"@_user_\d+|@_all", "", text)


def accumulated_text(handle: dict[str, Any], delta: str) -> str:
    """Accumulate delta text (full_text is stored in handle)."""
    acc = handle.get("full_text", "") + delta
    handle["full_text"] = acc
    return acc


def feishu_file_type(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    return {
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
        ".xls": "xls",
        ".xlsx": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
    }.get(suffix, "stream")

# -*- coding: utf-8 -*-
"""Settlement card subject identity for dedupe / replace."""
from __future__ import annotations

import re
from typing import Any

# Strip a single trailing parenthetical annotation from an entity display name.
_TRAILING_PAREN = re.compile(
    r"^(?P<head>.+?)\s*[（(][^）)]*[）)]\s*$"
)
# Table / datasource id: keep the token before the first paren annotation.
_LEADING_TOKEN = re.compile(
    r"^(?P<head>[^（(]+?)(?:\s*[（(].*)?$"
)


def canonical_name(raw: str) -> str:
    """Normalize an entity label to its subject name (drop trailing annotations)."""
    text = (raw or "").strip()
    if not text:
        return ""
    m = _TRAILING_PAREN.match(text)
    if m:
        return m.group("head").strip()
    return text


def canonical_datasource(raw: str) -> str:
    """Normalize a recommended datasource / table field for subject matching."""
    text = (raw or "").strip()
    if not text:
        return ""
    m = _LEADING_TOKEN.match(text)
    if m:
        return m.group("head").strip()
    return text


def normalize_item_fields(card_type: str, fields: dict[str, Any]) -> dict[str, str]:
    """Return a copy of fields with subject-bearing names normalized."""
    out = {str(k): str(v) if v is not None else "" for k, v in (fields or {}).items()}
    type_key = str(card_type)
    if type_key == "metric_caliber" and out.get("metric_name"):
        name = canonical_name(out["metric_name"])
        note = _annotation_text(out["metric_name"])
        out["metric_name"] = name
        if note:
            caliber = out.get("caliber") or ""
            if note not in caliber:
                out["caliber"] = f"{caliber}; {note}".strip("; ") if caliber else note
        if out.get("table"):
            out["table"] = canonical_datasource(out["table"])
    elif type_key == "dimension_def":
        if out.get("dimension_name"):
            out["dimension_name"] = canonical_name(out["dimension_name"])
        if out.get("table"):
            out["table"] = canonical_datasource(out["table"])
    elif type_key == "dataset_usage" and out.get("recommended_dataset"):
        out["recommended_dataset"] = canonical_datasource(out["recommended_dataset"])
    elif type_key == "column_meaning":
        if out.get("column_name"):
            out["column_name"] = canonical_name(out["column_name"])
        if out.get("table"):
            out["table"] = canonical_datasource(out["table"])
    return out


def _annotation_text(raw: str) -> str:
    text = (raw or "").strip()
    m = _TRAILING_PAREN.match(text)
    if not m:
        return ""
    # recover inner annotation
    inner = re.search(r"[（(]([^）)]*)[）)]\s*$", text)
    return (inner.group(1).strip() if inner else "")


def subject_key(card_type: str, fields: dict[str, Any]) -> str | None:
    """Stable subject key within a session; None if required identity fields missing."""
    type_key = str(card_type)
    f = fields or {}
    domain = str(f.get("domain") or "").strip()

    if type_key == "metric_caliber":
        name = canonical_name(str(f.get("metric_name") or ""))
        table = canonical_datasource(str(f.get("table") or ""))
        if not name:
            return None
        return f"metric:{domain}:{table}:{name}"

    if type_key == "dimension_def":
        name = canonical_name(str(f.get("dimension_name") or ""))
        table = canonical_datasource(str(f.get("table") or ""))
        if not name:
            return None
        return f"dimension:{domain}:{table}:{name}"

    if type_key == "column_meaning":
        col = canonical_name(str(f.get("column_name") or ""))
        table = canonical_datasource(str(f.get("table") or ""))
        if not col:
            return None
        return f"column:{domain}:{table}:{col}"

    if type_key == "dataset_usage":
        ds = canonical_datasource(str(f.get("recommended_dataset") or ""))
        if not ds:
            return None
        return f"dataset:{domain}:{ds}"

    return None

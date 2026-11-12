"""Develop's semantic writer rules, shared by snapshot and editor projections.

Ported from develop c4e072a7: semantic_incremental_write and cm_vocab.
Node properties, storage keys and authorization follow the current Schema.
"""

import logging
import re

log = logging.getLogger(__name__)


def dimension_role(value):
    text = str(value or "").strip()
    if not text:
        return None
    low = text.lower()
    if low in {"time", "categorical"}:
        return low
    if "时间" in text or "日期" in text or "time" in low or "date" in low:
        return "time"
    return "categorical"


def priority(value):
    if value is None or not str(value).strip():
        return "normal"
    return "high" if str(value).strip().upper() == "HIGH" else "normal"


def additivity(value):
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    return {
        "ADDITIVE": "additive",
        "SEMI_ADDITIVE": "semi_additive",
        "NON_ADDITIVE": "non_additive",
    }.get(text.upper().replace("-", "_"), text.lower())


_COL_WORD = r"[\w一-鿿]+"
_BARE_COL_RE = re.compile(rf"^({_COL_WORD})$")
_QUOTED_COL_RE = re.compile(rf"^([`'\"])({_COL_WORD})\1$")
_SELECT_COL_RE = re.compile(rf"select\s*\(\s*({_COL_WORD})", re.IGNORECASE)
_CASE_RE = re.compile(r"\bcase\b", re.IGNORECASE)
_THEN_TOKEN_RE = re.compile(r"\bthen\s+([^\s(),]+)", re.IGNORECASE)
_TOTAL_VALUE_TOKENS = ("全部", "总计", "总和", "total", "all")


def dimension_column(expr: str) -> str:
    """Extract develop's supported column forms; unsupported expressions skip the edge."""
    text = str(expr or "").strip()
    if not text:
        log.warning("Dimension binding has no expression; only anchors will be merged")
        return ""
    match = _BARE_COL_RE.fullmatch(text) or _QUOTED_COL_RE.fullmatch(text)
    if match:
        return match.groups()[-1]
    match = _SELECT_COL_RE.search(text)
    if match:
        return match.group(1)
    if _CASE_RE.search(text):
        match = _THEN_TOKEN_RE.search(text)
        if match:
            token = match.group(1)
            if token.startswith("`") and token.endswith("`") and len(token) > 2:
                inner = token[1:-1]
                if _BARE_COL_RE.fullmatch(inner):
                    return inner
            elif _BARE_COL_RE.fullmatch(token) and not token.isdigit():
                return token
    log.warning(
        "Dimension binding MAPS_TO_FIELD skipped: cannot extract column from expr=%r",
        expr,
    )
    return ""


def binding_properties(expr, examples):
    values = {"expr": expr.strip(), "value_example": list(examples)}
    for value in examples:
        text = str(value).strip().lower()
        if text == "全" or any(text.startswith(token) for token in _TOTAL_VALUE_TOKENS):
            values["total_value_marker"] = str(value).strip()
            break
    return values

# -*- coding: utf-8 -*-
"""Split unified diffs into selectable in-file edits and apply a subset."""

from __future__ import annotations

import re
from typing import Any

from qwenpaw_data.host.core.algo.skill_evolution.diffutil import apply_old_new_fuzzy

_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s@@(.*)$")
CONTEXT_LINES = 3


def parse_hunks(diff: str, *, patch_id: str, before: str = "") -> list[dict[str, Any]]:
    """Parse selectable edits from a unified diff.

    Each contiguous ``-``/``+`` change block becomes one edit (finer than a
    single ``@@`` hunk when difflib merges nearby changes). Stored under the
    historical key ``hunks`` for API compatibility; ids look like
    ``{patch_id}:e{n}``.
    """
    edits = _extract_edits(diff)
    out: list[dict[str, Any]] = []
    for index, edit in enumerate(edits):
        out.append(
            build_hunk(
                patch_id=patch_id,
                index=index,
                old_string=edit["old_string"],
                new_string=edit["new_string"],
                before=before,
            )
        )
    return out


def build_hunk(
    *,
    patch_id: str,
    index: int,
    old_string: str,
    new_string: str,
    before: str = "",
    reason: str = "",
    status: str = "pending",
) -> dict[str, Any]:
    """Build a selectable hunk with a context-rich display snippet."""
    loc = context_snippet(before, old_string, new_string)
    return {
        "hunk_id": f"{patch_id}:e{index}",
        "header": _edit_header(index, loc),
        "old_string": old_string,
        "new_string": new_string,
        "old_start": loc["old_start"],
        "old_count": loc["old_count"],
        "new_start": loc["new_start"],
        "new_count": loc["new_count"],
        "reason": reason,
        "diff": loc["diff"],
        "status": status,
    }


def context_snippet(
    before: str,
    old: str,
    new: str,
    *,
    context: int = CONTEXT_LINES,
) -> dict[str, Any]:
    """Build a unified hunk snippet with surrounding unchanged lines.

    Line numbers are 1-based. ``old_start``/``new_start`` cover the displayed
    window (context + change). ``change_old_start`` is the first changed line
    (or insert point) used in the review header.
    """
    before_lines = before.splitlines()
    old_lines = old.splitlines() if old else []
    new_lines = new.splitlines() if new else []

    if not old:
        return _insert_snippet(before_lines, new_lines, context=context)

    span = _locate_span(before, old)
    if span is None:
        return _stripped_snippet(old_lines, new_lines)

    start, end = span
    lead_from = max(0, start - context)
    trail_to = min(len(before_lines), end + context)
    leading = before_lines[lead_from:start]
    trailing = before_lines[end:trail_to]

    old_count = len(leading) + len(old_lines) + len(trailing)
    new_count = len(leading) + len(new_lines) + len(trailing)
    old_start = (lead_from + 1) if old_count else 0
    new_start = (lead_from + 1) if new_count else 0
    change_old_start = start + 1
    change_old_end = start + max(len(old_lines), 1)

    body = (
        [f" {line}" for line in leading]
        + [f"-{line}" for line in old_lines]
        + [f"+{line}" for line in new_lines]
        + [f" {line}" for line in trailing]
    )
    header = _unified_header(old_start, old_count, new_start, new_count)
    return {
        "diff": "\n".join([header, *body]),
        "old_start": old_start,
        "old_count": old_count,
        "new_start": new_start,
        "new_count": new_count,
        "change_old_start": change_old_start,
        "change_old_end": change_old_end,
    }


def ensure_patch_hunks(patch: dict[str, Any]) -> list[dict[str, Any]]:
    """Return patch['hunks'], parsing / wrapping context when missing."""
    before = str(patch.get("before") or "")
    raw = patch.get("hunks")
    if (
        isinstance(raw, list)
        and raw
        and all(
            isinstance(item, dict) and isinstance(item.get("old_string"), str)
            for item in raw
        )
    ):
        hunks = [h for h in raw if isinstance(h, dict)]
        if any(not _has_display_context(h) for h in hunks):
            hunks = _enrich_hunks(hunks, before=before)
            patch["hunks"] = hunks
        return hunks

    patch_id = str(patch.get("patch_id") or "patch")
    fallback_reason = str(patch.get("reason") or "").strip()
    hunks = parse_hunks(str(patch.get("diff") or ""), patch_id=patch_id, before=before)
    if not hunks and (patch.get("before") != patch.get("after")):
        hunks = [
            build_hunk(
                patch_id=patch_id,
                index=0,
                old_string=str(patch.get("before") or ""),
                new_string=str(patch.get("after") or ""),
                before=before,
                reason=fallback_reason,
            )
        ]
    elif fallback_reason:
        for hunk in hunks:
            hunk.setdefault("reason", fallback_reason)
    patch["hunks"] = hunks
    return hunks


def materialize_after_from_hunks(
    before: str,
    hunks: list[dict[str, Any]],
    selected_hunk_ids: set[str] | None,
) -> str:
    """Apply selected edits onto ``before`` (order preserved).

    ``selected_hunk_ids is None`` means apply all edits.
    """
    content = before
    for item in hunks:
        if not isinstance(item, dict):
            continue
        hunk_id = str(item.get("hunk_id") or "")
        if selected_hunk_ids is not None and hunk_id not in selected_hunk_ids:
            continue
        old = item.get("old_string")
        new = item.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            recovered = _strings_from_snippet(str(item.get("diff") or ""))
            if recovered is None:
                continue
            old, new = recovered
        if old == new:
            continue
        if old == "":
            content = new if content == "" else content + new
            continue
        content = apply_old_new_fuzzy(content, old, new)
    return content


def _enrich_hunks(hunks: list[dict[str, Any]], *, before: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, item in enumerate(hunks):
        if _has_display_context(item):
            out.append(item)
            continue
        old = str(item.get("old_string") or "")
        new = str(item.get("new_string") or "")
        loc = context_snippet(before, old, new)
        enriched = dict(item)
        enriched["diff"] = loc["diff"]
        enriched["old_start"] = loc["old_start"]
        enriched["old_count"] = loc["old_count"]
        enriched["new_start"] = loc["new_start"]
        enriched["new_count"] = loc["new_count"]
        header = str(item.get("header") or "")
        if "·" not in header and "L" not in header:
            enriched["header"] = _edit_header(index, loc)
        out.append(enriched)
    return out


def _has_display_context(hunk: dict[str, Any]) -> bool:
    diff = str(hunk.get("diff") or "")
    if any(line.startswith(" ") for line in diff.split("\n") if line):
        return True
    start = hunk.get("old_start")
    return isinstance(start, int) and start > 1


def _insert_snippet(
    before_lines: list[str],
    new_lines: list[str],
    *,
    context: int,
) -> dict[str, Any]:
    if not before_lines:
        header = _unified_header(0, 0, 1 if new_lines else 0, len(new_lines))
        body = [f"+{line}" for line in new_lines]
        return {
            "diff": "\n".join([header, *body]),
            "old_start": 0,
            "old_count": 0,
            "new_start": 1 if new_lines else 0,
            "new_count": len(new_lines),
            "change_old_start": 1,
            "change_old_end": 1,
        }
    lead_from = max(0, len(before_lines) - context)
    leading = before_lines[lead_from:]
    old_count = len(leading)
    new_count = len(leading) + len(new_lines)
    old_start = lead_from + 1
    new_start = lead_from + 1
    body = [f" {line}" for line in leading] + [f"+{line}" for line in new_lines]
    header = _unified_header(old_start, old_count, new_start, new_count)
    insert_at = len(before_lines) + 1
    return {
        "diff": "\n".join([header, *body]),
        "old_start": old_start,
        "old_count": old_count,
        "new_start": new_start,
        "new_count": new_count,
        "change_old_start": insert_at,
        "change_old_end": insert_at,
    }


def _stripped_snippet(old_lines: list[str], new_lines: list[str]) -> dict[str, Any]:
    body = [f"-{line}" for line in old_lines] + [f"+{line}" for line in new_lines]
    old_count = len(old_lines)
    new_count = len(new_lines)
    old_start = 1 if old_count else 0
    new_start = 1 if new_count else 0
    header = _unified_header(old_start, old_count, new_start, new_count)
    return {
        "diff": "\n".join([header, *body]),
        "old_start": old_start,
        "old_count": old_count,
        "new_start": new_start,
        "new_count": new_count,
        "change_old_start": old_start or 1,
        "change_old_end": (old_start + old_count - 1) if old_count else 1,
    }


def _locate_span(content: str, needle: str) -> tuple[int, int] | None:
    if not needle:
        return None
    idx = content.find(needle)
    if idx < 0:
        return None
    start = content.count("\n", 0, idx)
    n_lines = len(needle.splitlines()) or 1
    return start, start + n_lines


def _edit_header(index: int, loc: dict[str, Any]) -> str:
    label = f"edit {index + 1}"
    if loc.get("old_count") == 0 and loc.get("old_start") == 0:
        return f"{label} · new file"
    start = int(loc.get("change_old_start") or loc.get("old_start") or 0)
    end = int(loc.get("change_old_end") or start)
    if start <= 0:
        return label
    if start == end:
        return f"{label} · L{start}"
    return f"{label} · L{start}–{end}"


def _unified_header(
    old_start: int, old_count: int, new_start: int, new_count: int
) -> str:
    return f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"


def _extract_edits(diff: str) -> list[dict[str, str]]:
    if not diff or not diff.strip():
        return []
    lines = diff.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]

    edits: list[dict[str, str]] = []
    i = 0
    while i < len(lines):
        if not _HUNK_RE.match(lines[i]):
            i += 1
            continue
        i += 1
        while i < len(lines) and not lines[i].startswith("@@"):
            if lines[i].startswith("--- ") or lines[i].startswith("diff "):
                break
            if lines[i].startswith("\\"):
                i += 1
                continue
            if lines[i].startswith("-") or lines[i].startswith("+"):
                old_parts: list[str] = []
                new_parts: list[str] = []
                while i < len(lines) and (
                    lines[i].startswith("-") or lines[i].startswith("+")
                ):
                    raw = lines[i]
                    if raw.startswith("-"):
                        old_parts.append(raw[1:])
                    else:
                        new_parts.append(raw[1:])
                    i += 1
                old = "\n".join(old_parts)
                new = "\n".join(new_parts)
                if old != new:
                    edits.append({"old_string": old, "new_string": new})
                continue
            i += 1
    return edits


def _strings_from_snippet(snippet: str) -> tuple[str, str] | None:
    if not snippet.strip():
        return None
    old_parts: list[str] = []
    new_parts: list[str] = []
    for line in snippet.replace("\r\n", "\n").split("\n"):
        if line.startswith("@@") or line.startswith("\\"):
            continue
        if line.startswith("-"):
            old_parts.append(line[1:])
        elif line.startswith("+"):
            new_parts.append(line[1:])
    if not old_parts and not new_parts:
        return None
    return "\n".join(old_parts), "\n".join(new_parts)


__all__ = [
    "CONTEXT_LINES",
    "build_hunk",
    "context_snippet",
    "ensure_patch_hunks",
    "materialize_after_from_hunks",
    "parse_hunks",
]

# -*- coding: utf-8 -*-
"""Diff helpers and Hermes-style fuzzy find/replace for skill patches."""

from __future__ import annotations

import difflib
import re
from typing import Callable


def unified_diff(before: str, after: str, *, path: str) -> str:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    if before and not before.endswith("\n"):
        before_lines = before.splitlines(keepends=True)
        if before_lines and not before_lines[-1].endswith("\n"):
            before_lines[-1] = before_lines[-1] + "\n"
    if after and not after.endswith("\n"):
        after_lines = after.splitlines(keepends=True)
        if after_lines and not after_lines[-1].endswith("\n"):
            after_lines[-1] = after_lines[-1] + "\n"
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="\n",
        )
    )


def apply_old_new(content: str, old_string: str, new_string: str) -> str:
    """Exact unique replace (legacy helper)."""
    if old_string not in content:
        raise ValueError("old_string not found in current skill file")
    if content.count(old_string) != 1:
        raise ValueError("old_string matches multiple times in skill file")
    return content.replace(old_string, new_string, 1)


def fuzzy_find_and_replace(
    content: str,
    old_string: str,
    new_string: str,
) -> tuple[str, str, str, str | None, str | None]:
    """Find and replace with increasingly fuzzy strategies.

    Returns
    -------
    ``(new_content, resolved_old, resolved_new, strategy, error)``

    On success ``error`` is ``None`` and ``resolved_old`` / ``resolved_new``
    are the actual file slice replaced and the (possibly re-indented)
    replacement. On failure ``new_content`` is unchanged and ``error`` explains why.
    """
    if not old_string:
        return content, "", new_string, None, "old_string cannot be empty"
    if not old_string.strip():
        return (
            content,
            "",
            new_string,
            None,
            "old_string is only whitespace — provide non-blank text to match",
        )
    if old_string == new_string:
        return content, "", new_string, None, "old_string and new_string are identical"

    strategies: list[tuple[str, Callable[[str, str], list[tuple[int, int]]]]] = [
        ("exact", _strategy_exact),
        ("whitespace_normalized", _strategy_whitespace_normalized),
        ("indentation_flexible", _strategy_indentation_flexible),
        ("block_anchor", _strategy_block_anchor),
    ]
    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)
        if not matches:
            continue
        if len(matches) > 1:
            return (
                content,
                "",
                new_string,
                None,
                f"Found {len(matches)} matches for old_string; provide more context",
            )
        start, end = matches[0]
        resolved_old = content[start:end]
        resolved_new = (
            new_string
            if strategy_name == "exact"
            else _reindent_replacement(resolved_old, old_string, new_string)
        )
        new_content = content[:start] + resolved_new + content[end:]
        return new_content, resolved_old, resolved_new, strategy_name, None

    return (
        content,
        "",
        new_string,
        None,
        "Could not find a match for old_string in the file",
    )


def apply_old_new_fuzzy(content: str, old_string: str, new_string: str) -> str:
    """Like :func:`apply_old_new` but uses fuzzy matching; raises on failure."""
    new_content, _old, _new, _strategy, error = fuzzy_find_and_replace(
        content, old_string, new_string
    )
    if error:
        raise ValueError(error)
    return new_content


def _strategy_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + len(pattern)
    return matches


def _normalize_ws(text: str) -> str:
    """Trim trailing spaces per line, unify CRLF, collapse blank-line runs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= 1:
                out.append("")
        else:
            blank_run = 0
            out.append(line)
    return "\n".join(out)


def _strategy_whitespace_normalized(
    content: str, pattern: str
) -> list[tuple[int, int]]:
    norm_content = _normalize_ws(content)
    norm_pattern = _normalize_ws(pattern)
    if norm_pattern == pattern and norm_content == content:
        # Still try space/tab collapse.
        pass
    matches_norm = _strategy_exact(norm_content, norm_pattern)
    if not matches_norm:
        # Collapse horizontal whitespace runs as a second pass.
        def collapse(s: str) -> str:
            s = s.replace("\r\n", "\n").replace("\r", "\n")
            return re.sub(r"[ \t]+", " ", s)

        norm_content = collapse(content)
        norm_pattern = collapse(pattern)
        matches_norm = _strategy_exact(norm_content, norm_pattern)
        if not matches_norm:
            return []
        return _map_norm_positions(content, norm_content, matches_norm)
    return _map_norm_positions(content, norm_content, matches_norm)


def _strategy_indentation_flexible(content: str, pattern: str) -> list[tuple[int, int]]:
    content_lines = content.split("\n")
    pattern_lines = pattern.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not pattern_lines:
        return []
    stripped_pattern = "\n".join(line.lstrip(" \t") for line in pattern_lines)
    stripped_content_lines = [line.lstrip(" \t") for line in content_lines]
    matches: list[tuple[int, int]] = []
    n = len(pattern_lines)
    for i in range(len(content_lines) - n + 1):
        window = "\n".join(stripped_content_lines[i : i + n])
        if window == stripped_pattern:
            start, end = _line_span_offsets(content_lines, i, i + n, len(content))
            matches.append((start, end))
    return matches


def _strategy_block_anchor(content: str, pattern: str) -> list[tuple[int, int]]:
    """Match first+last non-empty lines to a unique window of similar size."""
    pattern_lines = pattern.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    non_empty = [ln for ln in pattern_lines if ln.strip()]
    if len(non_empty) < 2:
        return []
    first = non_empty[0].strip()
    last = non_empty[-1].strip()
    content_lines = content.split("\n")
    target_len = len(pattern_lines)
    # Allow small size drift (±2 lines).
    lo = max(2, target_len - 2)
    hi = target_len + 2
    candidates: list[tuple[int, int]] = []
    for i, line in enumerate(content_lines):
        if line.strip() != first:
            continue
        for length in range(lo, hi + 1):
            j = i + length - 1
            if j >= len(content_lines):
                break
            if content_lines[j].strip() != last:
                continue
            start, end = _line_span_offsets(content_lines, i, j + 1, len(content))
            candidates.append((start, end))
    # Prefer unique candidate; if many, only accept when a single window has
    # the exact target length.
    if len(candidates) == 1:
        return candidates
    exact = [
        c for c in candidates if content[c[0] : c[1]].count("\n") + 1 == target_len
    ]
    if len(exact) == 1:
        return exact
    return candidates if len(candidates) > 1 else []


def _line_span_offsets(
    lines: list[str], start_line: int, end_line: int, content_len: int
) -> tuple[int, int]:
    start = 0
    for i in range(start_line):
        start += len(lines[i]) + 1
    end = start
    for i in range(start_line, end_line):
        end += len(lines[i])
        if i < end_line - 1:
            end += 1
    if end_line >= len(lines) and content_len > 0 and not content_len == end:
        # Last line without trailing newline.
        end = min(end, content_len)
    return start, min(end, content_len)


def _map_norm_positions(
    original: str,
    normalized: str,
    matches: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Map positions from a whitespace-normalized string back to original.

    Builds a monotonic map by walking both strings; best-effort when the
    normalization only strips/collapses whitespace.
    """
    if original == normalized:
        return matches
    # Map each normalized index to the earliest original index.
    map_norm_to_orig: list[int] = []
    oi = 0
    for ni, ch in enumerate(normalized):
        while oi < len(original) and original[oi] != ch:
            # Skip chars removed by normalization (CR, trailing spaces, extras).
            oi += 1
        if oi >= len(original):
            map_norm_to_orig.append(len(original))
            continue
        map_norm_to_orig.append(oi)
        oi += 1
    map_norm_to_orig.append(min(oi, len(original)))

    out: list[tuple[int, int]] = []
    for ns, ne in matches:
        if ns >= len(map_norm_to_orig) or ne >= len(map_norm_to_orig):
            continue
        out.append((map_norm_to_orig[ns], map_norm_to_orig[ne]))
    return out


def _leading_whitespace(line: str) -> str:
    i = 0
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1
    return line[:i]


def _first_meaningful_line(text: str) -> str | None:
    for line in text.split("\n"):
        if line.strip():
            return line
    return None


def _reindent_replacement(file_region: str, old_string: str, new_string: str) -> str:
    if not new_string:
        return new_string
    old_first = _first_meaningful_line(old_string)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string
    old_indent = _leading_whitespace(old_first)
    file_indent = _leading_whitespace(file_first)
    if old_indent == file_indent:
        return new_string
    out_lines: list[str] = []
    for line in new_string.split("\n"):
        if not line.strip():
            out_lines.append(line)
            continue
        line_indent = _leading_whitespace(line)
        if line_indent.startswith(old_indent):
            out_lines.append(file_indent + line[len(old_indent) :])
        else:
            out_lines.append(file_indent + line.lstrip(" \t"))
    return "\n".join(out_lines)


__all__ = [
    "apply_old_new",
    "apply_old_new_fuzzy",
    "fuzzy_find_and_replace",
    "unified_diff",
]

# -*- coding: utf-8 -*-
"""Channel-aware rendering of the number-color spans segment fields carry.

The BizTrace extractor prompt (``algo/biztrace/prompts.py``) tells the model to
wrap key numbers in one of three Tailwind spans:

    <span class="text-blue-600 font-bold">…</span>   key value
    <span class="text-red-500 font-bold">…</span>    down / negative
    <span class="text-green-600 font-bold">…</span>  up / positive

Those classes only render under a Tailwind-aware HTML renderer. Every IM channel
we ship (feishu / dingtalk / wecom / wechat) embeds segment bodies as markdown
or plain text, where a raw ``<span>`` either gets stripped (no color) or leaks
as literal text. This module rewrites the three known spans into each channel's
native color syntax *before* the body is placed in a card, so the coloring the
prompt asks for actually reaches the user.

Only the three prompt-sanctioned spans are translated; unknown spans are left
untouched for the markdown channels (the renderer strips them, and a stray tag
stays visible as a signal that the prompt contract was violated). For plain
text no span renders usefully, so every span is stripped to its inner text.
"""
from __future__ import annotations

import re

# Match any <span class="…">…</span> the model emits around a key number. The
# prompt fixes the class verbatim, but stay tolerant of attribute order /
# extra whitespace in case the model drifts.
_SPAN_RE = re.compile(r'<span\s+class="([^"]*)">(.*?)</span>', re.DOTALL)

# Tailwind class token → semantic tone.
_TONE_BY_TOKEN: dict[str, str] = {
    "text-blue-600": "key",
    "text-red-500": "down",
    "text-green-600": "up",
}

# Feishu markdown <font color> accepts a fixed named palette; blue / red /
# green are all members (the lark SDK itself ships ``<font color='grey'>``),
# so the key/down/up tones map onto them cleanly.
#
# DingTalk is the awkward one: the segment card uses a StandardCard
# ``markdown`` content element, whose renderer STRIPS ``<font color="...">``
# entirely (hex confirmed not to render — only the ``**bold**`` inside
# survives). Hex ``<font color>`` only works in DingTalk's *robot markdown
# message*, a different renderer. The StandardCard markdown exposes only
# gray-level design tokens (``common_levelN_base_color``) — no red / green /
# blue — so there is no honest inline color to ship. Fall back to bold-only
# for DingTalk rather than emitting a dead ``<font>`` tag that pretends to
# color. (Real color would need a different card content type, e.g.
# richtext spans — out of scope.)
_FEISHU_COLOR: dict[str, str] = {"key": "blue", "down": "red", "up": "green"}


def _tone_for(class_attr: str) -> str | None:
    for token, tone in _TONE_BY_TOKEN.items():
        if token in class_attr:
            return tone
    return None


def render_segment_spans(body: str, *, target: str) -> str:
    """Rewrite number-color ``<span>``s in ``body`` for the ``target`` channel.

    Parameters
    ----------
    body:
        A segment ``input`` / ``behavior`` / ``conclusion`` string.
    target:
        ``"feishu"``  → ``<font color="blue|red|green">**…**</font>``
        ``"dingtalk"`` → ``**…**`` (StandardCard markdown strips ``<font>``;
        bold is the only emphasis that survives — see module docstring)
        ``"plain"``   → inner text only (every span stripped — plain-text IM)
    """
    if "<span" not in body:
        return body

    if target == "plain":
        # No span renders usefully in plain text; strip all, keep inner text.
        return _SPAN_RE.sub(lambda m: m.group(2), body)

    def _replace(m: re.Match[str]) -> str:
        class_attr, inner = m.group(1), m.group(2)
        tone = _tone_for(class_attr)
        if tone is None:
            # Unknown span — leave it; the markdown renderer strips it and a
            # contract violation stays visible rather than silently dropping.
            return m.group(0)
        if target == "feishu":
            color = _FEISHU_COLOR[tone]
            return f'<font color="{color}">**{inner}**</font>'
        # DingTalk StandardCard markdown: <font color> is stripped, so ship
        # bold-only — no dead font tag.
        return f"**{inner}**"

    return _SPAN_RE.sub(_replace, body)

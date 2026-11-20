# -*- coding: utf-8 -*-
"""render_segment_spans: channel-native rewrite of segment number-color spans."""

from __future__ import annotations

from qwenpaw_data.host.core.channels.segment_markup import render_segment_spans

_BODY = (
    '日活 <span class="text-blue-600 font-bold">1,234</span>，'
    '环比 <span class="text-red-500 font-bold">-3.2%</span>，'
    '收入 <span class="text-green-600 font-bold">+5.6%</span>。'
)


def test_feishu_uses_named_font_colors() -> None:
    out = render_segment_spans(_BODY, target="feishu")
    assert '<font color="blue">**1,234**</font>' in out
    assert '<font color="red">**-3.2%**</font>' in out
    assert '<font color="green">**+5.6%**</font>' in out
    assert "<span" not in out


def test_dingtalk_bold_only_no_font_tag() -> None:
    # StandardCard markdown strips <font color>; bold is the only emphasis that
    # survives, so we ship **…** and no dead font tag.
    out = render_segment_spans(_BODY, target="dingtalk")
    assert "**1,234**" in out
    assert "**-3.2%**" in out
    assert "**+5.6%**" in out
    assert "<span" not in out
    assert "<font" not in out


def test_plain_strips_spans_keeps_inner() -> None:
    out = render_segment_spans(_BODY, target="plain")
    assert "<span" not in out
    assert "<font" not in out
    assert "1,234" in out and "-3.2%" in out and "+5.6%" in out


def test_no_span_short_circuits() -> None:
    body = "普通文本，无 span。"
    for target in ("feishu", "dingtalk", "plain"):
        assert render_segment_spans(body, target=target) == body


def test_unknown_span_left_untouched_for_markdown_targets() -> None:
    body = 'x <span class="text-purple-400 font-bold">y</span> z'
    for target in ("feishu", "dingtalk"):
        assert render_segment_spans(body, target=target) == body


def test_unknown_span_stripped_for_plain() -> None:
    body = 'x <span class="text-purple-400 font-bold">y</span> z'
    assert render_segment_spans(body, target="plain") == "x y z"


def test_class_order_tolerant() -> None:
    body = '<span class="font-bold text-red-500">-1%</span>'
    assert '<font color="red">**-1%**</font>' == render_segment_spans(
        body, target="feishu"
    )

# -*- coding: utf-8 -*-
# pylint: disable=missing-function-docstring
"""测试 gold_parser 的图转文工具（多模态）。"""

from __future__ import annotations

from pathlib import Path

from gold_parser.image_reader import GoldImageReader
from stub_agent_runner import StubAgentRunner


def test_describe_returns_runner_text_and_passes_image():
    canned = "图类型：对比表。双百应用 17/100。结论：缺口大。"
    runner = StubAgentRunner(responses={"看这张来自金标报告的图": canned})
    reader = GoldImageReader(runner)
    img = Path("/tmp/fake-img.png")

    desc = reader.describe(
        img, section_title="10月目标进展", section_summary="action："
    )

    assert desc == canned
    assert len(runner._calls) == 1
    call = runner._calls[0]
    assert call["images"] == [img]
    assert "10月目标进展" in call["prompt"]
    assert "action：" in call["prompt"]


def test_describe_returns_empty_on_runner_failure():
    class _BoomRunner:
        def ask(self, prompt, *, images=None, **_):
            raise RuntimeError("boom")

    reader = GoldImageReader(_BoomRunner())
    assert reader.describe(Path("/tmp/x.png")) == ""

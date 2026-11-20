# -*- coding: utf-8 -*-
"""图转文工具：把金标报告里的图片转成文字描述。

它不是 Skill，而是切分器在多模态解析时调用的程序化工具。描述（数据点 / 结论 /
分析发现）会内联进章节的 ``raw_text``，让后续纯文本处理也把图里的内容当成金标
答案的一部分。原始 ``image_path`` 保留在 ``ReportSection.image_paths`` 上向下
透传，便于 Agent 在反推问题与金标答案时还能看到原图。

真正的视觉理解委托给构造时注入的 agent runner（需提供
``ask(prompt, images=..., internal=...)``），线上实现与测试替身共用这一入口。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_IMAGE_READER_PROMPT = """请看这张来自金标报告的图，提取其中的数据点/结论/分析发现，输出一段文本供并回金标正文。

要求：
1. 识别图类型（漏斗 / 趋势 / 对比表 / 结构拆解 / 截图 / 其它）
2. 把图里可见的关键数字、百分比、口径原样记下（如「双百应用 17/100」「高调用 54 个」）
3. 用一句话概括图的结论（卡点在哪、归因是什么、结构怎么拆）
4. 不要写方法论步骤；图里没有的数字不要补，看不清的标「（图中未识别）」

上下文：section title={title}，section summary={summary}

仅输出一段文本（不是 YAML、不是列表）。"""


class GoldImageReader:
    """Convert one gold-report image into a text description via the agent."""

    def __init__(self, agent_runner: Any) -> None:
        self._runner = agent_runner

    def describe(
        self,
        image_path: Path | str,
        *,
        section_title: str = "",
        section_summary: str = "",
    ) -> str:
        """Return a text description of the image, or "" on failure."""
        prompt = _IMAGE_READER_PROMPT.format(
            title=section_title, summary=section_summary
        )
        try:
            return self._runner.ask(prompt, images=[Path(image_path)], internal=True)
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "image-reader failed for %s; skipping",
                image_path,
                exc_info=True,
            )
            return ""

# -*- coding: utf-8 -*-
"""金标解析的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ReportSectionType(str, Enum):
    """Kind of deliverable a section represents (drives downstream rubric)."""

    CONCLUSION = "conclusion"  # 业务结论
    STRUCTURE = "structure"  # 结构化拆解（漏斗/分板块）
    DATA = "data"  # 数据支撑
    ACTION = "action"  # 行动建议
    NARRATIVE = "narrative"  # 描述性段落（通常为噪声）


@dataclass
class ReportSubsection:
    """A heading-scoped content block under a top-level report section."""

    title: str
    level: int
    content: str
    order: int = 0
    children: list["ReportSubsection"] = field(default_factory=list)
    image_paths: list[Path] = field(default_factory=list)
    image_descriptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "level": self.level,
            "content": self.content,
            "children": [c.to_dict() for c in self.children],
            "order": self.order,
            "image_paths": [str(p) for p in self.image_paths],
            "image_descriptions": list(self.image_descriptions),
        }


@dataclass
class ReportSection:
    """A top-level gold-report block with optional nested subsections."""

    section_id: str
    title: str
    level: int  # heading level (1=H1, 2=H2, ...)
    content_summary: str
    raw_text: str
    # These legacy fields are intentionally left empty by the plain-text
    # parser. Downstream reasoning should read raw_text/subsections instead of
    # regex-extracted data/action/noise buckets.
    data_points: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    section_types: list[ReportSectionType] = field(default_factory=list)
    order: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    subsections: list[ReportSubsection] = field(default_factory=list)
    # Multimodal: inline images in this section, plus the vision-derived text
    # descriptions (one per image, same order). raw_text already inlines the
    # descriptions at each image's position; these lists keep the raw image
    # paths for downstream carry-through (e.g. attaching to agent prompts).
    image_paths: list[Path] = field(default_factory=list)
    image_descriptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "level": self.level,
            "content_summary": self.content_summary,
            "content": self.raw_text,
            "subsections": [s.to_dict() for s in self.subsections],
            "order": self.order,
            "metadata": dict(self.metadata),
            "image_paths": [str(p) for p in self.image_paths],
            "image_descriptions": list(self.image_descriptions),
        }


@dataclass
class GoldReport:
    """A parsed gold report: metadata + its sections."""

    report_id: str
    source_path: Path
    title: str
    sections: list[ReportSection]
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "source_path": str(self.source_path),
            "title": self.title,
            "sections": [s.to_dict() for s in self.sections],
        }

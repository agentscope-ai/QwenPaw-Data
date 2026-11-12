# -*- coding: utf-8 -*-
"""金标报告解析与章节切分。"""

from __future__ import annotations

from .image_reader import GoldImageReader
from .models import ReportSection, ReportSectionType, GoldReport
from .section_splitter import ReportSectionSplitter

__all__ = [
    "ReportSection",
    "ReportSectionType",
    "GoldReport",
    "ReportSectionSplitter",
    "GoldImageReader",
]

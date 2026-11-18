#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告复现工作流的唯一入口。

实现拆在同目录的 ``rr`` 包里。本文件只负责让 ``python report_cli.py``
和既有测试继续从这里导入。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from rr.baseline import _KERNEL
from rr.cli import _SUBCOMMANDS, build_parser, main
from rr.common import (
    CHAT_ID_MAX_LENGTH,
    COMPARISON_FILE,
    EXIT_ERROR,
    EXIT_OK,
    IRREPRODUCIBLE_TAG,
    MANIFEST_FILE,
    MANIFEST_SCHEMA_VERSION,
    PRODUCT_DIR,
    PROVENANCE_RECOMPUTED,
    PROVENANCE_REFERENCE,
)
from rr.schedule import _infer_parameter_templates, _infer_period_spec

__all__ = [
    "CHAT_ID_MAX_LENGTH",
    "COMPARISON_FILE",
    "EXIT_ERROR",
    "EXIT_OK",
    "IRREPRODUCIBLE_TAG",
    "MANIFEST_FILE",
    "MANIFEST_SCHEMA_VERSION",
    "PRODUCT_DIR",
    "PROVENANCE_RECOMPUTED",
    "PROVENANCE_REFERENCE",
    "_SUBCOMMANDS",
    "_infer_parameter_templates",
    "_infer_period_spec",
    "build_parser",
    "main",
]

if __name__ == "__main__":
    sys.exit(main())

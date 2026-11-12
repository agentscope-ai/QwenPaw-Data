# -*- coding: utf-8 -*-
"""Skill 测试的导入准备。

``gold_parser`` 随 report-reproduction Skill 以文件形式分发，不是可安装的成员包，
因此把它所在目录交给解释器，测试直接 ``import gold_parser...``。同目录下的
``stub_agent_runner`` 也按同样方式取用。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SKILL_SCRIPTS = (
    _TESTS_DIR.parent / "skills" / "workflows" / "report-reproduction" / "scripts"
)

for _entry in (_SKILL_SCRIPTS, _TESTS_DIR):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

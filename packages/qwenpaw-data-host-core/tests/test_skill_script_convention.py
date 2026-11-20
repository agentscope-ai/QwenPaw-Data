# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from pathlib import Path

from qwenpaw_data.host.core.utils.skill import discover_builtin_skills

_CWD_RELATIVE_INVOKE = re.compile(r"^\s*(?:python3?|bash)\s+scripts/", re.MULTILINE)
_PROMPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "qwenpaw_data"
    / "host"
    / "core"
    / "prompts"
)


def test_host_prompts_forbid_invented_workspace_root_scripts() -> None:
    agent = (_PROMPTS_DIR / "AGENT.md").read_text(encoding="utf-8")
    assert "<skill-dir>/scripts/" in agent
    assert "不要编造" in agent


def test_builtin_skills_do_not_invoke_workspace_root_scripts() -> None:
    offenders: list[str] = []
    for skill in discover_builtin_skills():
        text = (skill.src_dir / "SKILL.md").read_text(encoding="utf-8")
        if _CWD_RELATIVE_INVOKE.search(text):
            offenders.append(skill.name)
    assert offenders == []

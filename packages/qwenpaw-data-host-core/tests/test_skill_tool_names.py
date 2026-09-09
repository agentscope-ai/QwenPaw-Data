# -*- coding: utf-8 -*-
"""Guard against planning skill docs naming tools that do not exist.

`analysis-plan-builder/SKILL.md` once told the model to call
`update_subtask_state`; the registered tool is `update_subtask`. The model
then burned turns on a tool that could never resolve.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from qwenpaw_data.host.core.orchestration.tools import PLAN_TOOL_NAMES

_SKILLS_ROOT = (
    Path(__file__).resolve().parents[2] / "qwenpaw-data-skills" / "skills"
)
_BACKTICKED = re.compile(r"`([a-z][a-z0-9_]*)`")


def _planning_skill_docs() -> list[Path]:
    return sorted((_SKILLS_ROOT / "planning").rglob("SKILL.md"))


def test_planning_skills_are_discoverable() -> None:
    assert _planning_skill_docs(), f"no planning SKILL.md under {_SKILLS_ROOT}"


@pytest.mark.parametrize("doc", _planning_skill_docs(), ids=lambda p: p.parent.name)
def test_planning_skill_tool_names_resolve(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    drifted = {
        token
        for token in _BACKTICKED.findall(text)
        for name in PLAN_TOOL_NAMES
        if token.startswith(name) and token != name
    }
    assert not drifted, (
        f"{doc} references tool names that do not exist: {sorted(drifted)}; "
        f"registered orchestration tools are {sorted(PLAN_TOOL_NAMES)}"
    )

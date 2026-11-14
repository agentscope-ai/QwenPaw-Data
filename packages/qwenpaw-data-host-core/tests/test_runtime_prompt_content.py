# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from qwenpaw_data.host.core import prompts as prompts_pkg


_PROMPT_DIR = Path(prompts_pkg.__file__).resolve().parent


def test_analysis_environment_formats_and_contains_landing_rules() -> None:
    text = (_PROMPT_DIR / "ANALYSIS_ENVIRONMENT.md").read_text(encoding="utf-8")
    rendered = text.format(
        session_id="session-a",
        workspace_dir="/workspace",
        artifact_dir="/workspace/artifacts/session-a",
        workspace_mode="local workspace（宿主侧执行）",
    )
    assert "plan.yaml" in rendered
    assert "data/raw/" in rendered
    assert "result.yaml" in rendered
    assert "<交付物名>_v1.<ext>" in rendered
    assert "{" not in rendered


def test_agent_prompt_absorbs_runtime_policy() -> None:
    text = (_PROMPT_DIR / "AGENT.md").read_text(encoding="utf-8")
    assert "## 人机交互" in text
    assert "执行期规范" not in text
    assert "### 1：" in text
    assert "### 2：" in text
    assert "第2类" in text
    assert "Type 1" not in text
    assert "Type 2" not in text
    assert "ask_user_question" in text
    assert "未完成" in text
    assert "不执行、不改" in text
    assert "交付前检查" in text
    assert "2f 报告复现" in text
    assert "report-reproduction" in text
    assert "runtime-guide" not in text
    assert "interaction-strategy" not in text
    assert "bi-semantic-layer-guide" not in text

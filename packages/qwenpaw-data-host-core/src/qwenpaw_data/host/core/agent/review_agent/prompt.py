# -*- coding: utf-8 -*-
"""System prompt middleware for the review agent."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agentscope.middleware import MiddlewareBase

from qwenpaw_data.host.core.settings import get_settings

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from qwenpaw_data.host.core.runtime.context import RunContext

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


class ReviewPromptMiddleware(MiddlewareBase):
    """Packaged AGENT.md + REVIEW_ENVIRONMENT.md with placeholders filled."""

    def __init__(self, *, run_context: RunContext) -> None:
        self._run_context = run_context

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        _ = agent
        paths = self._run_context.paths
        workspace = self._run_context.workspace
        modes = {
            "fc-sandbox": "FC sandbox（远端执行，OSS 持久化）",
            "acs-sandbox": "ACS sandbox（远端执行，OSS 持久化）",
            "docker": "docker workspace（容器内执行）",
            "local": "local workspace（宿主侧执行）",
        }
        env = {
            "session_id": paths.session_id,
            "workspace_dir": workspace.workdir,
            "artifact_dir": paths.agent_artifact_dir(workspace),
            "workspace_mode": modes[get_settings().workspace.backend],
        }
        host = "\n\n".join(
            [
                (_PROMPT_DIR / "AGENT.md").read_text(encoding="utf-8").strip(),
                (_PROMPT_DIR / "REVIEW_ENVIRONMENT.md")
                .read_text(encoding="utf-8")
                .strip()
                .format(**env),
            ],
        )
        extra = current_prompt.strip()
        return f"{host}\n\n{extra}" if extra else host

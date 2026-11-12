# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agentscope.middleware import MiddlewareBase
from agentscope.state import TaskContext

from qwenpaw_data.host.core import prompts as prompts_pkg
from qwenpaw_data.host.core.integrations.cm.client import fetch_global_memories
from qwenpaw_data.host.core.settings import get_settings

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from qwenpaw_data.host.core.runtime.context import RunContext

_PROMPT_DIR = Path(prompts_pkg.__file__).resolve().parent


class GlobalMemoryPromptMiddleware(MiddlewareBase):
    """Load user memory once per run and prepend its formatted prompt."""

    def __init__(self, *, run_context: RunContext) -> None:
        self._run_context = run_context
        self._prompt: str | None = None

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        if self._prompt is None:
            self._prompt = await self._load_prompt()
        if not self._prompt:
            return current_prompt
        return f"{self._prompt}\n\n{current_prompt}" if current_prompt else self._prompt

    async def _load_prompt(self) -> str:
        content = await fetch_global_memories(
            self._run_context.request_context.get("access_token"),
            chat_id=self._run_context.chat_id,
        )
        if content is None:
            return ""
        sections = []
        profile = content["profile"]
        if profile:
            lines = [
                f"- {label}：{profile[field]}"
                for field, label in (
                    ("display_name", "姓名"),
                    ("description", "简介"),
                    ("locale", "语言"),
                    ("timezone", "时区"),
                )
                if profile.get(field)
            ]
            if lines:
                sections.append("### 用户资料\n\n" + "\n".join(lines))
        for field, title in (
            ("principles", "长期原则（必须遵守）"),
            ("preferences", "长期偏好（默认遵循，可被当前明确指令覆盖）"),
        ):
            if content[field]:
                lines = [f"- {memory['description']}" for memory in content[field]]
                sections.append(f"### {title}\n\n" + "\n".join(lines))
        if not sections:
            return ""
        return "\n\n".join(
            [
                "## Global Memory",
                "以下是当前用户在当前 workspace 下的长期上下文，遵循 Host 基础规则。",
                *sections,
            ]
        )


class QwenPawDataPromptMiddleware(MiddlewareBase):
    """Host markdown + AgentScope skill/workspace instructions."""

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
                (_PROMPT_DIR / "ANALYSIS_ENVIRONMENT.md")
                .read_text(encoding="utf-8")
                .strip()
                .format(**env),
            ],
        )
        extra = current_prompt.strip()
        return f"{host}\n\n{extra}" if extra else host


class DatasourcePromptMiddleware(MiddlewareBase):
    """Append the session-bound datasource id and type."""

    def __init__(self, *, run_context: RunContext) -> None:
        self._run_context = run_context

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        _ = agent
        request_context = self._run_context.request_context
        datasource_id = str(request_context.get("datasource_id") or "").strip()
        datasource_type = str(request_context.get("datasource_type") or "").strip()
        if datasource_id:
            lines = ["## 当前数据源", f"- datasource_id：`{datasource_id}`"]
            if datasource_type:
                lines.append(f"- 数据源类型：`{datasource_type}`")
            lines.append(
                "本会话已绑定该数据源；调用 `execute_read` 等 SQL 工具时默认传入此 "
                "`datasource_id`。仅当用户明确指定其他数据源，或所需表经图谱确认不在"
                "该数据源时才更换（更换前用 `ask_user_question` 确认）。"
            )
            block = "\n".join(lines)
        elif datasource_type:
            block = f"当前数据源类型：`{datasource_type}`。"
        else:
            return current_prompt
        return f"{current_prompt}\n\n{block}" if current_prompt else block


class PlanPromptMiddleware(MiddlewareBase):
    """Append the latest Plan at every model reasoning boundary."""

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        context = agent.state.tasks_context
        if not isinstance(context, TaskContext):
            raise TypeError("PlanPromptMiddleware requires TaskContext")
        hint = self.format_plan_hint(context)
        if not hint:
            return current_prompt
        return f"{current_prompt}\n\n{hint}"

    def format_plan_hint(self, context: TaskContext) -> str:
        if not context.tasks:
            return ""

        task_lines = []
        for task in context.tasks:
            dependencies = ", ".join(task.blocked_by) if task.blocked_by else "-"
            task_lines.append(
                f"- {task.id} [{task.state}] {task.subject}; blocked_by={dependencies}",
            )

        in_progress = [task.id for task in context.tasks if task.state == "in_progress"]
        unfinished = [task.id for task in context.tasks if task.state != "completed"]
        return "\n".join(
            [
                "<current_plan>",
                "Tasks:",
                "\n".join(task_lines),
                f"In progress: {', '.join(in_progress) if in_progress else '-'}",
                f"Unfinished: {', '.join(unfinished) if unfinished else '-'}",
                "Task dependencies are informational and do not block execution.",
                "</current_plan>",
            ],
        )

# -*- coding: utf-8 -*-
"""Review one requirement with an isolated worker agent."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from agentscope.agent import Agent
from agentscope.agent._config import ReActConfig
from agentscope.message import Msg, UserMsg
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import ToolChunk
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qwenpaw_data.host.core.domain.model_usage import ModelUsageSource
from qwenpaw_data.host.core.providers.factory import build_model

from .base import ReviewToolBase


class _ReviewRequirementParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(
        pattern=r"^[A-Za-z0-9_-]+$",
        description="需求编号，例如 R1。",
    )
    requirement: str = Field(min_length=1, description="该需求的完整内容。")
    start_sequence: int = Field(ge=1, description="该需求的起始轮次序号。")
    end_sequence: int = Field(ge=1, description="该需求的结束轮次序号。")

    @model_validator(mode="after")
    def _ordered(self) -> _ReviewRequirementParams:
        if self.end_sequence < self.start_sequence:
            raise ValueError("end_sequence must be >= start_sequence")
        return self


class ReviewRequirementTool(ReviewToolBase):
    """Run a fresh, requirement-scoped reviewer and write one stable note."""

    name = "review_requirement"
    description = (
        "审核一条需求并产出走查笔记。每次调用创建独立审核 agent，"
        "笔记写入 walkthrough-notes-{requirement_id}.md。"
    )
    is_concurrency_safe = True
    is_read_only = False

    def __init__(self, run_context: Any) -> None:
        super().__init__(run_context)
        self.input_schema = _ReviewRequirementParams.model_json_schema()

    async def call(self, **kwargs: Any) -> AsyncGenerator[ToolChunk, None]:
        try:
            params = _ReviewRequirementParams.model_validate(kwargs)
            note_path = self._note_path(params.requirement_id)
            note_path.parent.mkdir(parents=True, exist_ok=True)
            worker = await self._build_worker()

            summary = ""
            async for event_or_msg in worker.reply_stream(
                UserMsg(
                    name="review-request",
                    content=self._worker_input(params, note_path),
                ),
                yield_final_msg=True,
            ):
                if isinstance(event_or_msg, Msg):
                    summary = event_or_msg.get_text_content() or summary

            yield self._ok(
                {
                    "status": "completed",
                    "requirement_id": params.requirement_id,
                    "note_path": note_path.name,
                    "rounds_reviewed": params.end_sequence - params.start_sequence + 1,
                    "summary": summary,
                }
            )
        except Exception as exc:
            yield self._err(exc)

    async def _build_worker(self) -> Agent:
        from qwenpaw_data.host.core.runtime.builder import ReviewAgentBuilder

        toolkit = await ReviewAgentBuilder().build_toolkit(
            run_context=self._run_context,
            exclude={"review_requirement"},
        )
        return Agent(
            name="requirement-reviewer",
            system_prompt=(
                "你是单需求审核代理。第一个动作必须调用 Skill 工具加载 "
                "review-requirement，并严格执行。只审核输入给出的需求和轮次，"
                "按 skill 中的笔记模板把走查笔记写入输入指定的文件。完成后"
                "回复一段审核小结：需求结论、发现的问题数量与等级、笔记文件名。"
            ),
            model=build_model(
                self._run_context.user_runtime_config.default,
                usage=self._run_context.model_usage(ModelUsageSource.REVIEW_AGENT),
            ),
            toolkit=toolkit,
            state=AgentState(
                permission_context=PermissionContext(mode=PermissionMode.BYPASS)
            ),
            offloader=self._run_context.workspace,
            react_config=ReActConfig(
                max_iters=self._run_context.user_runtime_config.react_max_iters,
                interruption_raise_cancelled_error=True,
            ),
        )

    def _note_path(self, requirement_id: str) -> Path:
        return (
            Path(self._run_context.paths.artifact_dir)
            / f"walkthrough-notes-{requirement_id}.md"
        )

    @staticmethod
    def _worker_input(params: _ReviewRequirementParams, note_path: Path) -> str:
        return (
            f"需求编号：{params.requirement_id}\n"
            f"需求内容：{params.requirement}\n"
            f"必须审核的轮次：第 {params.start_sequence} 轮到第 "
            f"{params.end_sequence} 轮（含首尾）\n"
            f"走查笔记写入：{note_path}"
        )

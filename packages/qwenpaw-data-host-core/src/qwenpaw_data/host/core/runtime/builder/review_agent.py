# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from agentscope.agent._config import ContextConfig, ReActConfig
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from qwenpaw_data.host.core.agent import review_agent as review_agent_pkg
from qwenpaw_data.host.core.agent.middleware import (
    CompressTriggerMiddleware,
    TracingMiddleware,
    PlanPromptMiddleware,
    QueryLogMiddleware,
    SteerMiddleware,
    ToolConfirmationMiddleware,
    ToolTimeoutMiddleware,
    TraceForwardMiddleware,
)
from qwenpaw_data.host.core.agent.review_agent.agent import ReviewAgent
from qwenpaw_data.host.core.agent.review_agent.prompt import ReviewPromptMiddleware
from qwenpaw_data.host.core.agent.review_agent.tools import build_review_tools
from qwenpaw_data.host.core.agent.tools.plan_tools import build_plan_tools
from qwenpaw_data.host.core.domain.model_usage import ModelUsageSource
from qwenpaw_data.host.core.providers.factory import build_model
from qwenpaw_data.host.core.repositories.skill_store import SkillStoreRepo
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.runtime.envelope import Envelope

_REVIEW_SKILL_NAMES = ("review-audit", "review-requirement")
_REVIEW_SKILL_SRC_DIR = Path(review_agent_pkg.__file__).resolve().parent / "skill"
_BLOCKED_REVIEW_TOOL_NAMES = frozenset({"Bash", "PowerShell"})


class ReviewAgentBuilder:
    """Build the ReviewAgent for a review run."""

    AGENT_ID = "review"

    @classmethod
    def matches(cls, agent_id: str) -> bool:
        return agent_id == cls.AGENT_ID

    def prepare_workspace(self, store: SkillStoreRepo) -> None:
        installed = {item.name for item in store.list_skills()}
        for name in _REVIEW_SKILL_NAMES:
            if name not in installed:
                store.import_skill_dir(_REVIEW_SKILL_SRC_DIR / name)

    async def build_toolkit(
        self,
        *,
        run_context: RunContext,
        exclude: set[str] | frozenset[str] = frozenset(),
    ) -> Toolkit:
        workspace = run_context.workspace
        file_tools = [
            tool
            for tool in await workspace.list_tools()
            if tool.name not in _BLOCKED_REVIEW_TOOL_NAMES
        ]
        review_tools = [
            tool for tool in build_review_tools(run_context) if tool.name not in exclude
        ]
        return Toolkit(
            tools=[*file_tools, *build_plan_tools(), *review_tools],
            mcps=[],
            skills_or_loaders=list(await workspace.list_skills()),
        )

    async def build(
        self,
        *,
        run_context: RunContext,
        envelope: Envelope,
        state: AgentState,
    ) -> ReviewAgent:
        toolkit = await self.build_toolkit(run_context=run_context)
        return ReviewAgent(
            name="qwenpaw-data-reviewer",
            system_prompt="",
            model=build_model(
                run_context.user_runtime_config.default,
                usage=run_context.model_usage(ModelUsageSource.REVIEW_AGENT),
            ),
            toolkit=toolkit,
            middlewares=[
                TracingMiddleware(run_context=run_context),
                SteerMiddleware(run_context=run_context),
                CompressTriggerMiddleware(run_context=run_context),
                QueryLogMiddleware(run_context=run_context),
                ToolConfirmationMiddleware(run_context=run_context),
                ToolTimeoutMiddleware(run_context=run_context),
                TraceForwardMiddleware(
                    run_context=run_context,
                    envelope=envelope,
                ),
                ReviewPromptMiddleware(run_context=run_context),
                PlanPromptMiddleware(),
            ],
            state=state,
            offloader=run_context.workspace,
            context_config=ContextConfig(
                reserve_ratio=run_context.user_runtime_config.context_reserve_ratio,
                tool_result_limit=run_context.user_runtime_config.tool_result_limit,
            ),
            # Re-raise CancelledError on stop so ChatRuntime records "canceled".
            react_config=ReActConfig(
                max_iters=run_context.user_runtime_config.react_max_iters,
                interruption_raise_cancelled_error=True,
            ),
            request_context=run_context.request_context,
        )

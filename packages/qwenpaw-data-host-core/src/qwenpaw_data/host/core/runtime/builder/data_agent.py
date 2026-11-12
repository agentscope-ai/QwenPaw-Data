# -*- coding: utf-8 -*-
from __future__ import annotations

from agentscope.agent._config import ContextConfig, ReActConfig
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.agent.middleware import (
    CompressTriggerMiddleware,
    ConfirmedSettlementPromptMiddleware,
    ContextUsageMiddleware,
    QwenPawDataPromptMiddleware,
    DatasourcePromptMiddleware,
    FollowUpMiddleware,
    GlobalMemoryPromptMiddleware,
    TracingMiddleware,
    PlanPromptMiddleware,
    QueryLogMiddleware,
    SettlementDetectionMiddleware,
    SkillEvolutionDetectionMiddleware,
    SqlArtifactMiddleware,
    SteerMiddleware,
    ToolConfirmationMiddleware,
    ToolTimeoutMiddleware,
    TraceForwardMiddleware,
)
from qwenpaw_data.host.core.agent.tools import (
    AskUserQuestionTool,
    CronJobTool,
)
from qwenpaw_data.host.core.agent.tools.plan_tools import build_plan_tools
from qwenpaw_data.host.core.domain.model_usage import ModelUsageSource
from qwenpaw_data.host.core.providers.factory import build_model
from qwenpaw_data.host.core.repositories.skill_store import SkillStoreRepo
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.runtime.envelope import Envelope
from qwenpaw_data.host.core.utils.mcp import bind_mcp_tool_timeout


class DataAgentBuilder:
    """Build the production QwenPawDataAgent for a Console run."""

    def prepare_workspace(self, store: SkillStoreRepo) -> None:
        store.ensure_ready()

    async def build_toolkit(self, *, run_context: RunContext) -> Toolkit:
        workspace = run_context.workspace
        disabled = run_context.disabled_skills
        agent_id = run_context.paths.agent_id
        skills = await workspace.list_skills(agent_id=agent_id)
        config = run_context.user_runtime_config
        tool_timeout = (
            config.tool_timeout_seconds if config.tool_timeout_enabled else None
        )
        return Toolkit(
            tools=[
                *await workspace.list_tools(),
                *build_plan_tools(),
                AskUserQuestionTool(),
                CronJobTool(run_context),
            ],
            mcps=[
                bind_mcp_tool_timeout(client, tool_timeout)
                for client in await workspace.list_mcps(
                    agent_id=agent_id, session_id=run_context.session_id
                )
            ],
            skills_or_loaders=[skill for skill in skills if skill.name not in disabled],
        )

    async def build(
        self,
        *,
        run_context: RunContext,
        envelope: Envelope,
        state: AgentState,
    ) -> QwenPawDataAgent:
        workspace = run_context.workspace
        toolkit = await self.build_toolkit(run_context=run_context)
        user_runtime_config = run_context.user_runtime_config
        return QwenPawDataAgent(
            name="qwenpaw-data",
            system_prompt="",
            model=build_model(
                user_runtime_config.default,
                usage=run_context.model_usage(ModelUsageSource.DATA_AGENT),
            ),
            toolkit=toolkit,
            middlewares=[
                TracingMiddleware(run_context=run_context),
                SteerMiddleware(run_context=run_context),
                CompressTriggerMiddleware(run_context=run_context),
                QueryLogMiddleware(run_context=run_context),
                ToolConfirmationMiddleware(run_context=run_context),
                ToolTimeoutMiddleware(run_context=run_context),
                SqlArtifactMiddleware(run_context=run_context),
                GlobalMemoryPromptMiddleware(run_context=run_context),
                QwenPawDataPromptMiddleware(run_context=run_context),
                DatasourcePromptMiddleware(run_context=run_context),
                ContextUsageMiddleware(envelope=envelope),
                TraceForwardMiddleware(
                    run_context=run_context,
                    envelope=envelope,
                ),
                FollowUpMiddleware(
                    run_context=run_context,
                    envelope=envelope,
                ),
                SettlementDetectionMiddleware(
                    run_context=run_context,
                    envelope=envelope,
                ),
                SkillEvolutionDetectionMiddleware(
                    run_context=run_context,
                    envelope=envelope,
                ),
                ConfirmedSettlementPromptMiddleware(
                    run_context=run_context,
                    envelope=envelope,
                ),
                PlanPromptMiddleware(),
            ],
            state=state,
            offloader=workspace,
            context_config=ContextConfig(
                reserve_ratio=user_runtime_config.context_reserve_ratio,
                tool_result_limit=user_runtime_config.tool_result_limit,
            ),
            # Re-raise CancelledError on stop so ChatRuntime records "canceled".
            react_config=ReActConfig(
                max_iters=user_runtime_config.react_max_iters,
                interruption_raise_cancelled_error=True,
            ),
            request_context=run_context.request_context,
        )

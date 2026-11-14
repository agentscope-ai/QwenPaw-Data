"""QwenPawData AgentScope agent shared by production runtimes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from agentscope.agent import Agent
from agentscope.agent._config import (
    ContextConfig,
    InjectionConfig,
    ModelConfig,
    ReActConfig,
)
from agentscope.event import ReplyStartEvent, AgentEvent
from agentscope.message import AssistantMsg, Msg, TextBlock, UserMsg
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState, TaskContext
from agentscope.tool import Toolkit
from agentscope.workspace import Offloader


class QwenPawDataAgent(Agent):
    """QwenPawData agent shared by production runtimes."""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        model: ChatModelBase,
        toolkit: Toolkit,
        middlewares: list[MiddlewareBase] | None = None,
        state: AgentState | None = None,
        offloader: Offloader | None = None,
        model_config: ModelConfig = ModelConfig(),
        context_config: ContextConfig = ContextConfig(),
        *,
        react_config: ReActConfig,
        request_context: dict[str, Any] | None = None,
    ) -> None:
        self._request_context = dict(request_context or {})
        super().__init__(
            name=name,
            system_prompt=system_prompt,
            model=model,
            toolkit=toolkit,
            middlewares=middlewares or [],
            state=state
            if state is not None
            else AgentState(
                permission_context=PermissionContext(mode=PermissionMode.BYPASS),
            ),
            offloader=offloader,
            model_config=model_config,
            context_config=context_config,
            react_config=react_config,
            injection_config=InjectionConfig(timezone="Asia/Shanghai"),
        )

    def get_plan(self) -> TaskContext:
        return self.state.tasks_context

    def replace_plan(
        self,
        plan: TaskContext,
        *,
        reason: str | None = None,
    ) -> TaskContext:
        """Replace the current Plan. Concurrent with Plan tools: last write wins."""
        self.state.tasks_context = plan
        self.state.context.append(self._plan_changed_message(reason))
        return self.state.tasks_context

    @staticmethod
    def _plan_changed_message(reason: str | None = None) -> UserMsg:
        lines = [
            "[用户更新了当前计划]",
            "当前计划已被完整替换，请依据最新的 system plan hint 继续。",
        ]
        if reason:
            lines.append(f"修改原因：{reason}")
        return UserMsg(name="user", content="\n".join(lines))

    def set_request_context(self, request_context: dict[str, Any] | None) -> None:
        self._request_context = dict(request_context or {})

    async def reply(self, inputs: Any = None) -> Msg:
        msg = await self._collect_reply(self.reply_stream(inputs))
        return self._finalize_reply(msg)

    async def _collect_reply(
        self,
        events: AsyncGenerator[AgentEvent, None],
    ) -> Msg:
        final_msg: Msg | None = None
        current_msg: Msg | None = None

        async for event in events:
            if isinstance(event, ReplyStartEvent):
                current_msg = AssistantMsg(
                    name=event.name,
                    content=[],
                    id=event.reply_id,
                )
                final_msg = current_msg
            elif current_msg is not None and hasattr(event, "reply_id"):
                current_msg.append_event(event)

        if final_msg is None:
            raise RuntimeError("QwenPawDataAgent did not produce a final message.")
        return final_msg

    def _finalize_reply(self, msg: Msg) -> Msg:
        return msg

    async def handle_interrupt(
        self,
        msg: Msg | list[Msg] | None = None,
        **kwargs: Any,
    ) -> Msg:
        _ = (msg, kwargs)
        return Msg(
            name=self.name,
            content=[TextBlock(type="text", text="已中断。有什么需要调整的吗？")],
            role="assistant",
            metadata={"_is_interrupted": True},
        )

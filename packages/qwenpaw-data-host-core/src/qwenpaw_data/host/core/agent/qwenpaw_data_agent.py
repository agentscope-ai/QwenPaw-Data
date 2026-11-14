"""QwenPawData AgentScope agent shared by production runtimes."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from pathlib import Path, PurePath
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

from ..artifact_paths import model_visible_path
from ..mcp_cm import inject_datasource_metadata
from ..orchestration import RuntimeStateManager
from ..orchestration.task_graph import SOP
from ..permission import ConfirmationHandler
from .dag_execution import DAGExecution
from .middleware.tool_timeout import ToolTimeoutMiddleware


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
        react_config: ReActConfig | None = None,
        request_context: dict[str, Any] | None = None,
        runtime_state: RuntimeStateManager | None = None,
        mode: str | None = None,
        session_id: str = "default",
        workspace_dir: PurePath | str | None = None,
        artifact_dir: PurePath | str | None = None,
        session_trace_writer: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> None:
        self._request_context = dict(request_context or {})
        self._dag = None
        self._cm_mcp_tool_prefixes = set(
            getattr(toolkit, "_qwenpaw_data_cm_mcp_tool_prefixes", set())
        )
        if runtime_state is not None or mode is not None:
            self._runtime_state = runtime_state or RuntimeStateManager()
            workdir = model_visible_path(
                workspace_dir or getattr(offloader, "workdir", None) or Path.cwd()
            )
            self._dag = DAGExecution(
                self._runtime_state,
                mode=mode or "agent",
                session_id=session_id,
                workspace_dir=workdir,
                artifact_dir=model_visible_path(
                    artifact_dir or workdir / "artifacts" / session_id
                ),
                session_trace_writer=session_trace_writer,
                confirmation_handler=confirmation_handler,
            )
            middlewares = [ToolTimeoutMiddleware(), *(middlewares or [])]
        elif session_trace_writer is not None or confirmation_handler is not None:
            raise ValueError("Standalone callbacks require runtime_state or mode")
        super().__init__(
            name=name,
            system_prompt=system_prompt,
            model=model,
            toolkit=toolkit,
            middlewares=[
                *(middlewares or []),
                *(self._dag.middlewares if self._dag else []),
            ],
            state=state
            if state is not None
            else AgentState(
                permission_context=PermissionContext(
                    mode=PermissionMode.DONT_ASK if self._dag else PermissionMode.BYPASS
                ),
            ),
            offloader=offloader,
            model_config=model_config,
            context_config=context_config,
            react_config=react_config
            or ReActConfig(
                max_iters=100,
                interruption_raise_cancelled_error=True,
            ),
            injection_config=InjectionConfig(timezone="Asia/Shanghai"),
        )
        if self._dag:
            self._dag.set_mode(self, self._dag.mode)

    def get_plan(self) -> TaskContext | SOP:
        if self._dag:
            return self._dag.get_plan()
        return self.state.tasks_context

    def replace_plan(
        self,
        plan: TaskContext | SOP | dict | str,
        *,
        reason: str | None = None,
    ) -> TaskContext | Awaitable[SOP]:
        """Replace the current Plan. Concurrent with Plan tools: last write wins."""
        if self._dag:
            return self._dag.replace_plan(self, plan, reason)
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

    @property
    def plan_notebook(self) -> RuntimeStateManager:
        if not self._dag:
            raise RuntimeError(
                "This agent uses service TaskContext, not a standalone DAG"
            )
        return self._dag.runtime

    @property
    def session_id(self) -> str:
        if not self._dag:
            raise AttributeError("Service session identity belongs to RunContext")
        return self._dag.session_id

    def set_mode(self, mode: str) -> None:
        if not self._dag:
            raise RuntimeError("Mode switching requires a standalone DAG")
        self._dag.set_mode(self, mode)

    def get_plan_states(self) -> dict[str, str]:
        return {node.id: node.state for node in self.plan_notebook._graph_nodes()}

    async def execute_sop(
        self,
        sop: SOP | dict | str,
        *,
        stream: bool = False,
    ) -> Msg | AsyncGenerator[AgentEvent, None]:
        if not self._dag:
            raise RuntimeError("SOP loading requires a standalone DAG")

        async def events():
            await self._dag.load(sop)
            async with aclosing(
                self.reply_stream(
                    UserMsg(
                        name="user",
                        content="按已加载计划的依赖顺序执行，并记录每个节点的结果。",
                    )
                )
            ) as source:
                async for event in source:
                    yield event

        if stream:
            return events()
        return self._finalize_reply(await self._collect_reply(events()))

    async def reply_stream(
        self, inputs: Any = None
    ) -> AsyncGenerator[AgentEvent, None]:
        source = (
            self._dag.reply_stream(super().reply_stream, inputs)
            if self._dag
            else super().reply_stream(inputs)
        )
        async with aclosing(source) as events:
            async for event in events:
                yield event

    async def _execute_tool_call(self, tool_call, kept_rules=None):
        if self._dag:
            self._inject_datasource_metadata(tool_call)
        async with aclosing(
            super()._execute_tool_call(tool_call, kept_rules)
        ) as events:
            async for event in events:
                yield event

    def _inject_datasource_metadata(self, tool_call: Any) -> None:
        inject_datasource_metadata(
            tool_call,
            request_context=self._request_context,
            cm_mcp_tool_prefixes=self._cm_mcp_tool_prefixes,
        )

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
        if self._dag and self._dag.mode == "agent":
            rs = self._dag.runtime
            return self.set_execution_metadata(
                msg,
                graph_id=rs.current_graph_id or rs.last_finished_graph_id,
            )
        return msg

    def set_execution_metadata(self, msg: Msg, *, graph_id: str | None) -> Msg:
        if not self._dag:
            raise RuntimeError("Execution metadata requires a standalone DAG")
        return self._dag.finalize(msg, graph_id)

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

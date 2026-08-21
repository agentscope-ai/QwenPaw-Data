# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, Literal

from agentscope.event import AgentEvent
from agentscope.message import Msg
from agentscope.permission import PermissionMode
from agentscope.state import AgentState

from .agent import QwenPawDataAgent
from .agent.toolkit import build_qwenpaw_data_toolkit
from .model import build_model_from_env
from .orchestration.dag_store import DAGStore
from .orchestration import DefaultGraphToHint, RuntimeStateManager
from .orchestration.task_graph import SOP
from .paths import Paths, resolve_qwenpaw_data_home
from .permission import (
    ConfirmationHandler,
    build_permission_context,
    resolve_permission_mode,
)
from .session import JSONSessionStore
from .utils.ids import create_session_id
from .utils.msg import user_msg
from .utils.workspace import create_docker_workspace, create_local_workspace

QWENPAW_DATA_AGENT_NAME = "qwenpaw-data"
logger = logging.getLogger(__name__)


class QwenPawDataHost:
    """QwenPaw Data 运行句柄。"""

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        model: Any = None,
        workspace: Any = None,
        workspace_type: Literal["local", "docker"] = "docker",
        session_id: str | None = None,
        request_context: dict[str, Any] | None = None,
        permission_mode: PermissionMode | str | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> None:
        self.home = resolve_qwenpaw_data_home(home)
        self.session_id = session_id or create_session_id()
        self.request_context = dict(request_context or {})
        self.model = model or build_model_from_env()
        self.workspace = workspace
        self.workspace_type = workspace_type
        self.permission_mode = resolve_permission_mode(
            workspace_type,
            permission_mode,
        )
        self.confirmation_handler = confirmation_handler
        if workspace is None and workspace_type == "local":
            logger.warning(
                "local workspace explicitly selected: agent shell commands run "
                "with the host user's privileges and are not sandboxed; "
                "permission mode is %s",
                self.permission_mode.value,
            )
            if self.permission_mode is PermissionMode.BYPASS:
                logger.critical(
                    "local workspace permission checks are explicitly bypassed; "
                    "all agent tool calls can run with host-user privileges",
                )
        self.dag_store = DAGStore(self.paths.dag_root)
        self._agent: QwenPawDataAgent | None = None

    # ------------------------------------------------------------------
    # 配置 / 路径 / 会话访问
    # ------------------------------------------------------------------

    @property
    def paths(self) -> Paths:
        """已绑定 ``(home, session_id)`` 的路径视图。"""
        return Paths(self.home, self.session_id)

    @property
    def session_store(self) -> JSONSessionStore:
        """当前 QwenPaw Data 实例的 session 状态存储。"""
        return JSONSessionStore(self.paths.console_root)

    async def plan(
        self,
        prompt: str,
        *,
        request_context: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> Msg | AsyncGenerator[AgentEvent, None]:
        """由自然语言 prompt 生成一份 :class:`SOP`（仅规划，不执行）。"""
        agent = await self._get_agent(mode="plan", request_context=request_context)
        input_msg = user_msg(prompt)
        if stream:
            return agent.reply_stream(input_msg)

        msg = await agent.reply(input_msg)
        plan = agent.get_plan()
        msg.metadata["plan"] = plan.model_dump(mode="json")
        return msg

    async def execute(
        self,
        sop: SOP | dict | str,
        *,
        request_context: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> Msg | AsyncGenerator[AgentEvent, None]:
        """执行一份 SOP（或其 dict / YAML 形式）直至完成。"""
        agent = await self._get_agent(mode="agent", request_context=request_context)
        return await agent.execute_sop(sop, stream=stream)

    async def run(
        self,
        prompt: str,
        *,
        request_context: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> Msg | AsyncGenerator[AgentEvent, None]:
        """端到端执行：由 prompt 直接规划并执行直至完成。"""
        agent = await self._get_agent(mode="agent", request_context=request_context)
        input_msg = user_msg(prompt)
        if stream:
            return agent.reply_stream(input_msg)

        return await agent.reply(input_msg)

    # ------------------------------------------------------------------
    # 内部 helper
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """释放 workspace 资源（Docker 模式下停止并移除容器）。

        local workspace 无 close 钩子时为 no-op；重复调用安全。
        """
        workspace = self.workspace
        if workspace is None:
            return
        close = getattr(workspace, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _workspace(self) -> Any:
        if self.workspace is None:
            if self.workspace_type == "docker":
                self.workspace = create_docker_workspace(self.paths)
            else:
                self.workspace = create_local_workspace(self.paths)
        initialize = getattr(self.workspace, "initialize", None)
        if callable(initialize):
            result = initialize()
            if inspect.isawaitable(result):
                await result
        return self.workspace

    async def _get_agent(
        self,
        *,
        mode: str,
        request_context: dict[str, Any] | None = None,
    ) -> Any:
        effective_context = self._request_context(request_context)
        if self._agent is not None:
            self._agent.set_mode(mode)
            self._agent.set_request_context(effective_context)
            return self._agent

        paths = self.paths

        ws = await self._workspace()

        rs = RuntimeStateManager(
            graph_to_hint=DefaultGraphToHint(),
            path_resolver=paths.artifact_context.resolve_path,
        )
        agent_ref: dict[str, Any] = {}
        toolkit = await build_qwenpaw_data_toolkit(
            rs,
            workspace=ws,
            parent_agent_getter=lambda: agent_ref.get("agent"),
            workspace_dir=paths.workspace,
            artifacts_root=paths.artifacts_root,
            session_id_getter=lambda: self.session_id,
        )
        session_store = self.session_store
        permission_context = build_permission_context(
            mode=self.permission_mode,
            workdir=getattr(ws, "workdir", paths.workspace),
        )

        async def append_session_trace(entry: dict[str, Any]) -> None:
            await session_store.append_trace_event(self.session_id, entry)

        agent = QwenPawDataAgent(
            name=QWENPAW_DATA_AGENT_NAME,
            system_prompt="",
            model=self.model,
            toolkit=toolkit,
            state=AgentState(permission_context=permission_context),
            offloader=ws,
            runtime_state=rs,
            request_context=effective_context,
            mode=mode,
            session_id=self.session_id,
            session_trace_writer=append_session_trace,
            confirmation_handler=self.confirmation_handler,
        )
        agent_ref["agent"] = agent
        rs.configure_dag_store(
            self.dag_store,
            session_id=self.session_id,
        )
        self._agent = agent
        return self._agent

    def _request_context(
        self,
        request_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(self.request_context)
        if request_context:
            merged.update(request_context)
        return merged

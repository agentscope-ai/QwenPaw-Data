# -*- coding: utf-8 -*-
"""AgentScope 2.0 ``spawn_subagent`` tool for DataPaw."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ReActConfig
from agentscope.event import (
    ConfirmResult,
    ReplyEndEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from agentscope.message import Msg, TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionMode,
)
from agentscope.skill import Skill, SkillLoaderBase
from agentscope.state import AgentState
from agentscope.tool import ToolBase, ToolChunk, Toolkit

from ..mcp_cm import inject_datasource_metadata
from ..orchestration.state import RuntimeStateManager

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 6000
STREAM_POLL_SECONDS = 1.0
MAX_ITERS = 50
MAX_CONCURRENT = 4
MAX_TOTAL_SPAWNS = 20
SUPPORTED_ROLES = ("data_fetcher",)

DATA_FETCHER_TOOL_NAMES = frozenset({
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
})
ROLE_SKILL_NAMES = {
    "data_fetcher": frozenset({"fetch-data"}),
}


def _text_chunk(
    text: str,
    *,
    state: ToolResultState = ToolResultState.RUNNING,
    is_last: bool = False,
    metadata: dict[str, Any] | None = None,
) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(type="text", text=text)],
        state=state,
        is_last=is_last,
        metadata=metadata or {},
    )


def _as_path(value: Any) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser()


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _text_from_msg(msg: Any) -> str:
    if msg is None:
        return ""
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if isinstance(block, dict):
            text = block.get("text") or block.get("thinking")
            if text:
                parts.append(str(text))
            continue
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def _trace_append(trace_log: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    if trace_log and trace_log[-1].get("type") == entry.get("type"):
        trace_log[-1] = entry
    else:
        trace_log.append(entry)


def _build_environment_section(
    *,
    workspace_dir: Path | None,
    artifacts_root: Path | None,
    session_id: str | None,
    graph_id: str | None,
    node_id: str | None,
) -> str:
    workspace_text = str(workspace_dir) if workspace_dir else "(unknown)"
    artifacts_text = str(artifacts_root) if artifacts_root else "(unknown)"
    if session_id and graph_id and node_id:
        artifact_dir = f"artifacts/{session_id}/{graph_id}/{node_id}/"
        file_ref = f"{session_id}/{graph_id}/{node_id}/<filename>"
        artifact_scope = "当前节点"
    elif session_id:
        artifact_dir = f"artifacts/{session_id}/"
        file_ref = f"{session_id}/<filename>"
        artifact_scope = "当前会话"
    else:
        artifact_dir = "artifacts/<session_id>/"
        file_ref = "<session_id>/<filename>"
        artifact_scope = "当前会话"

    return (
        "## DataPaw 分析环境\n"
        f"- agent workspace: `{workspace_text}`\n"
        f"- artifacts 根目录: `{artifacts_text}`\n"
        "- 工具的相对路径以 agent workspace 为根。\n"
        f"- {artifact_scope}产物必须保存到 `{artifact_dir}`；"
        f"最终摘要中的文件引用使用 `{file_ref}`，不要带 `artifacts/` 前缀。\n\n"
    )


def _build_sub_prompt(
    task: str,
    context: str,
    upstream_outputs: dict[str, str],
    builtin_tool_names: Sequence[str],
    mcp_tool_names: Sequence[str],
    *,
    workspace_dir: Path | None = None,
    artifacts_root: Path | None = None,
    session_id: str | None = None,
    graph_id: str | None = None,
    node_id: str | None = None,
) -> str:
    upstream_section = (
        "\n".join(f"- {nid}: {out}" for nid, out in upstream_outputs.items())
        if upstream_outputs
        else "无（根节点）"
    )
    builtin_list = ", ".join(builtin_tool_names) if builtin_tool_names else "无"
    mcp_list = ", ".join(mcp_tool_names) if mcp_tool_names else "无"
    mcp_hint = (
        "以上是你的全部可用工具。语义层 / MCP 工具可查询数据或执行 SQL，请直接调用。\n"
        if mcp_tool_names
        else "以上是你的全部可用工具。当前没有语义层 / MCP 工具可用。\n"
    )

    return (
        "你是一个任务执行器。精确完成指定任务。\n\n"
        f"## 你的任务\n{task}\n\n"
        f"{_build_environment_section(workspace_dir=workspace_dir, artifacts_root=artifacts_root, session_id=session_id, graph_id=graph_id, node_id=node_id)}"
        "## 可用工具\n"
        f"内置工具：{builtin_list}\n"
        f"语义层 / MCP 工具：{mcp_list}\n"
        f"{mcp_hint}\n"
        f"## 上下文\n{context or '无额外上下文。'}\n\n"
        f"## 上游信息\n{upstream_section}\n\n"
        "## 规则\n"
        "- 完整执行任务。\n"
        "- 直接使用上述可用工具，不要探索或猜测是否存在其他工具。\n"
        "- 任务执行过程中，需要调用工具时，优先在同一条 assistant 消息里"
        "先写 1 句简短进度说明，再附带 tool_use；不要输出长篇分析或计划。\n"
        "- 只有在任务全部完成后，才输出一次结构化文字总结作为最终回答。\n"
        "- 遇到无法解决的错误时描述问题并停止。\n\n"
        "## 输出要求\n"
        "任务完成后，你的最终回答必须是结构化摘要，格式：\n"
        "- **结论**：说明任务结果（做了什么、数据概况、关键发现）\n"
        "- **产出文件**：列出所有生成的文件路径\n"
        "- **异常**：如有问题或部分失败，在此说明；无异常则省略\n"
    )


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", "") or "")


def _skills_for_role(skills: Iterable[Any], role: str) -> list[Any]:
    allowed = ROLE_SKILL_NAMES.get(role, frozenset())
    selected: list[Any] = []
    for skill in skills:
        if isinstance(skill, str):
            name = Path(skill).name
            skill_dir = skill
        else:
            name = str(getattr(skill, "name", "") or "")
            skill_dir = str(getattr(skill, "dir", "") or "")
        if name not in allowed and Path(skill_dir).name not in allowed:
            continue
        if isinstance(skill, (str, Skill, SkillLoaderBase)):
            selected.append(skill)
        elif skill_dir:
            selected.append(skill_dir)
    return selected


async def _collect_mcp_tool_names(mcps: Sequence[Any]) -> list[str]:
    names: list[str] = []
    for client in mcps:
        try:
            tools = await client.list_tools()
        except Exception:
            logger.warning(
                "spawn_subagent: failed to list MCP tools from %r; "
                "its tools will be unavailable to sub-agents",
                getattr(client, "name", repr(client)),
                exc_info=True,
            )
            continue
        names.extend(_tool_name(tool) for tool in tools if _tool_name(tool))
    return names


class _SubAgent(Agent):
    """Agent variant that preserves DataPaw CM datasource injection."""

    def __init__(
        self,
        *,
        request_context: dict[str, Any],
        cm_mcp_tool_prefixes: Iterable[str],
        **kwargs: Any,
    ) -> None:
        self._request_context = dict(request_context)
        self._cm_mcp_tool_prefixes = set(cm_mcp_tool_prefixes)
        super().__init__(**kwargs)

    async def _execute_tool_call(
        self,
        tool_call: Any,
        kept_rules: list[Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        inject_datasource_metadata(
            tool_call,
            request_context=self._request_context,
            cm_mcp_tool_prefixes=self._cm_mcp_tool_prefixes,
        )
        async for item in super()._execute_tool_call(tool_call, kept_rules):
            yield item


class SpawnSubagent(ToolBase):
    """Spawn an in-process role-scoped sub-agent."""

    name: str = "spawn_subagent"
    description: str = (
        "Spawn an in-process sub-agent to execute one specific data task. "
        "The current in-progress DAG node is detected automatically."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The specific task instruction for the sub-agent.",
            },
            "role": {
                "type": "string",
                "default": "data_fetcher",
                "description": "Sub-agent role. Currently supported: data_fetcher.",
            },
            "context": {
                "type": "string",
                "default": "",
                "description": "Optional upstream results, constraints, or context.",
            },
        },
        "required": ["task"],
    }
    is_state_injected: bool = False
    is_concurrency_safe: bool = True
    is_read_only: bool = False
    is_external_tool: bool = False
    is_mcp: bool = False
    mcp_name: str | None = None

    def __init__(
        self,
        *,
        runtime_state: RuntimeStateManager,
        workspace: Any,
        workspace_tools: Sequence[Any],
        workspace_mcps: Sequence[Any],
        workspace_skills: Sequence[Any],
        parent_agent_getter: Callable[[], Any] | None = None,
        workspace_dir: Path | str | None = None,
        artifacts_root: Path | str | None = None,
        session_id_getter: Callable[[], str | None] | None = None,
        request_context_getter: Callable[[], dict[str, Any]] | None = None,
        cm_mcp_tool_prefixes: Iterable[str] = (),
    ) -> None:
        self._rs = runtime_state
        self._workspace = workspace
        self._workspace_tools = list(workspace_tools)
        self._workspace_mcps = list(workspace_mcps)
        self._workspace_skills = list(workspace_skills)
        self._parent_agent_getter = parent_agent_getter
        self._workspace_dir = _as_path(workspace_dir)
        self._artifacts_root = _as_path(artifacts_root)
        self._session_id_getter = session_id_getter or (lambda: None)
        self._request_context_getter = request_context_getter
        self._cm_mcp_tool_prefixes = set(cm_mcp_tool_prefixes)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._count_lock = asyncio.Lock()
        self._spawn_count = 0

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="spawn_subagent is allowed for DataPaw agent mode.",
        )

    async def __call__(  # type: ignore[override]
        self,
        task: str,
        role: str = "data_fetcher",
        context: str = "",
    ) -> AsyncGenerator[ToolChunk, None]:
        if role not in SUPPORTED_ROLES:
            yield _text_chunk(
                f"不支持的 role: '{role}'。当前支持: {SUPPORTED_ROLES}",
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        async with self._count_lock:
            if self._spawn_count >= MAX_TOTAL_SPAWNS:
                yield _text_chunk(
                    f"已达 spawn 上限 ({MAX_TOTAL_SPAWNS})",
                    state=ToolResultState.ERROR,
                    is_last=True,
                )
                return
            self._spawn_count += 1

        async with self._semaphore:
            async for chunk in self._run_subagent(
                task=task,
                role=role,
                context=context,
            ):
                yield chunk

    async def _run_subagent(
        self,
        *,
        task: str,
        role: str,
        context: str,
    ) -> AsyncGenerator[ToolChunk, None]:
        parent_agent = self._parent_agent()
        if parent_agent is None:
            yield _text_chunk(
                "Sub-agent 创建失败: parent agent is not bound",
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        model = getattr(parent_agent, "model", None)
        if model is None:
            yield _text_chunk(
                "Sub-agent 创建失败: parent model is not available",
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        current_node = self._rs.get_current_in_progress_node()
        node_id = current_node.id if current_node else None
        graph_id = self._rs.current_graph_id
        upstream_outputs = (
            self._rs.get_upstream_outputs(node_id)
            if node_id is not None
            else {}
        )
        session_id = _as_text(self._session_id_getter())
        workspace_tools = self._tools_for_role(role)
        workspace_mcps = list(self._workspace_mcps)
        workspace_skills = _skills_for_role(self._workspace_skills, role)
        mcp_tool_names = await _collect_mcp_tool_names(workspace_mcps)

        sub_toolkit = Toolkit(
            tools=workspace_tools,
            mcps=workspace_mcps,
            skills_or_loaders=workspace_skills,
        )
        sys_prompt = _build_sub_prompt(
            task,
            context,
            upstream_outputs,
            builtin_tool_names=[tool.name for tool in workspace_tools],
            mcp_tool_names=mcp_tool_names,
            workspace_dir=self._workspace_dir,
            artifacts_root=self._artifacts_root,
            session_id=session_id,
            graph_id=graph_id,
            node_id=node_id,
        )
        agent_name = (
            f"subagent-{role}-{node_id or 'free'}-{uuid.uuid4().hex[:4]}"
        )

        try:
            parent_permission_context = getattr(
                getattr(parent_agent, "state", None),
                "permission_context",
                None,
            )
            if parent_permission_context is None:
                parent_permission_context = PermissionContext(
                    mode=PermissionMode.DONT_ASK,
                )
            else:
                parent_permission_context = parent_permission_context.model_copy(
                    deep=True,
                )
            sub_agent = _SubAgent(
                name=agent_name,
                system_prompt=sys_prompt,
                model=model,
                toolkit=sub_toolkit,
                state=AgentState(
                    permission_context=parent_permission_context,
                ),
                offloader=getattr(parent_agent, "offloader", self._workspace),
                react_config=ReActConfig(max_iters=MAX_ITERS),
                request_context=self._request_context(parent_agent),
                cm_mcp_tool_prefixes=self._cm_mcp_tool_prefixes,
            )
        except Exception as exc:
            logger.warning("spawn_subagent: failed to create sub-agent", exc_info=True)
            yield _text_chunk(
                f"Sub-agent 创建失败: {exc}",
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        task_msg = Msg(
            name="user",
            content=[TextBlock(type="text", text=task)],
            role="user",
        )
        async for chunk in self._stream_subagent(
            sub_agent=sub_agent,
            task_msg=task_msg,
            agent_name=agent_name,
            graph_id=graph_id,
            node_id=node_id,
        ):
            yield chunk

    def _parent_agent(self) -> Any | None:
        if self._parent_agent_getter is None:
            return None
        try:
            return self._parent_agent_getter()
        except Exception:
            logger.debug("spawn_subagent: parent agent getter failed", exc_info=True)
            return None

    def _request_context(self, parent_agent: Any) -> dict[str, Any]:
        if self._request_context_getter is not None:
            return dict(self._request_context_getter() or {})
        return dict(getattr(parent_agent, "_request_context", {}) or {})

    def _tools_for_role(self, role: str) -> list[Any]:
        if role != "data_fetcher":
            return []
        return [
            tool
            for tool in self._workspace_tools
            if _tool_name(tool) in DATA_FETCHER_TOOL_NAMES
        ]

    async def _stream_subagent(
        self,
        *,
        sub_agent: Agent,
        task_msg: Msg,
        agent_name: str,
        graph_id: str | None,
        node_id: str | None,
    ) -> AsyncGenerator[ToolChunk, None]:
        trace_log: list[dict[str, Any]] = []
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_names: dict[str, str] = {}
        tool_inputs: dict[str, str] = {}
        tool_outputs: dict[str, str] = {}

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TIMEOUT_SECONDS
        event_task: asyncio.Task[Any] | None = None
        pending_input: Msg | UserConfirmResultEvent = task_msg
        event_iter: Any = None

        try:
            while True:
                confirmation: RequireUserConfirmEvent | None = None
                event_iter = sub_agent.reply_stream(inputs=pending_input)
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()

                    if event_task is None:
                        event_task = asyncio.create_task(event_iter.__anext__())

                    done, _ = await asyncio.wait(
                        {event_task},
                        timeout=min(STREAM_POLL_SECONDS, remaining),
                    )
                    if not done:
                        continue

                    try:
                        event = event_task.result()
                    except StopAsyncIteration:
                        break
                    finally:
                        event_task = None

                    if isinstance(event, RequireUserConfirmEvent):
                        confirmation = event

                    chunk = self._event_to_chunk(
                        event,
                        trace_log=trace_log,
                        text_parts=text_parts,
                        thinking_parts=thinking_parts,
                        tool_names=tool_names,
                        tool_inputs=tool_inputs,
                        tool_outputs=tool_outputs,
                    )
                    if chunk is not None:
                        yield chunk

                if confirmation is None:
                    break
                logger.info(
                    "spawn_subagent: denying unattended permission request",
                )
                pending_input = UserConfirmResultEvent(
                    reply_id=confirmation.reply_id,
                    confirm_results=[
                        ConfirmResult(confirmed=False, tool_call=tool_call)
                        for tool_call in confirmation.tool_calls
                    ],
                )
        except asyncio.TimeoutError:
            if event_task is not None and not event_task.done():
                event_task.cancel()
                with suppress(asyncio.CancelledError):
                    await event_task
            if event_iter is not None:
                await self._close_event_iter(event_iter)
            yield _text_chunk(
                f"Sub-agent 超时（{TIMEOUT_SECONDS}s）",
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return
        except Exception as exc:
            if event_task is not None and not event_task.done():
                event_task.cancel()
                with suppress(asyncio.CancelledError):
                    await event_task
            if event_iter is not None:
                await self._close_event_iter(event_iter)
            logger.warning("spawn_subagent: sub-agent execution failed", exc_info=True)
            yield _text_chunk(
                f"Sub-agent 异常: {exc}",
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        summary = "".join(text_parts).strip() or "".join(thinking_parts).strip()
        if not summary:
            summary = "(no output)"
        trace_meta = {
            "type": "subagent_trace",
            "agent_name": agent_name,
            "graph_id": graph_id,
            "node_id": node_id,
            "entries": trace_log,
        }
        yield _text_chunk(
            summary,
            is_last=True,
            metadata={
                "subagent_event": "summary",
                "subagent_trace": trace_meta,
            },
        )

    def _event_to_chunk(
        self,
        event: Any,
        *,
        trace_log: list[dict[str, Any]],
        text_parts: list[str],
        thinking_parts: list[str],
        tool_names: dict[str, str],
        tool_inputs: dict[str, str],
        tool_outputs: dict[str, str],
    ) -> ToolChunk | None:
        if isinstance(event, TextBlockDeltaEvent):
            text_parts.append(event.delta)
            text = "".join(text_parts)
            _trace_append(trace_log, {"type": "thinking", "text": text})
            return _text_chunk(
                event.delta,
                is_last=False,
                metadata={"subagent_event": "thinking"},
            )

        if isinstance(event, ThinkingBlockDeltaEvent):
            thinking_parts.append(event.delta)
            text = "".join(thinking_parts)
            _trace_append(trace_log, {"type": "thinking", "text": text})
            return _text_chunk(
                event.delta,
                is_last=False,
                metadata={"subagent_event": "thinking"},
            )

        if isinstance(event, ToolCallStartEvent):
            tool_names[event.tool_call_id] = event.tool_call_name
            tool_inputs[event.tool_call_id] = ""
            _trace_append(
                trace_log,
                {
                    "type": "tool_call",
                    "name": event.tool_call_name,
                    "input": "",
                },
            )
            return None

        if isinstance(event, ToolCallDeltaEvent):
            name = tool_names.get(event.tool_call_id, "tool")
            tool_inputs[event.tool_call_id] = (
                tool_inputs.get(event.tool_call_id, "") + event.delta
            )
            _trace_append(
                trace_log,
                {
                    "type": "tool_call",
                    "name": name,
                    "input": tool_inputs[event.tool_call_id],
                },
            )
            return None

        if isinstance(event, ToolCallEndEvent):
            name = tool_names.get(event.tool_call_id, "tool")
            input_text = tool_inputs.get(event.tool_call_id, "")
            return _text_chunk(
                f"[tool_call] {name}({input_text})",
                is_last=False,
                metadata={
                    "subagent_event": "tool_call",
                    "subagent_tool_name": name,
                },
            )

        if isinstance(event, ToolResultTextDeltaEvent):
            name = tool_names.get(event.tool_call_id, "tool")
            tool_outputs[event.tool_call_id] = (
                tool_outputs.get(event.tool_call_id, "") + event.delta
            )
            _trace_append(
                trace_log,
                {
                    "type": "tool_result",
                    "name": name,
                    "output": tool_outputs[event.tool_call_id],
                },
            )
            return _text_chunk(
                event.delta,
                is_last=False,
                metadata={
                    "subagent_event": "tool_result",
                    "subagent_tool_name": name,
                },
            )

        if isinstance(event, ToolResultDataDeltaEvent):
            name = tool_names.get(event.tool_call_id, "tool")
            _trace_append(
                trace_log,
                {"type": "tool_result", "name": name, "output": "[data]"},
            )
            return None

        if isinstance(event, (ToolResultEndEvent, ReplyEndEvent)):
            return None

        if isinstance(event, Msg):
            text = _text_from_msg(event)
            if text:
                text_parts.append(text)
                _trace_append(
                    trace_log,
                    {"type": "thinking", "text": "".join(text_parts)},
                )
            return None

        return None

    @staticmethod
    async def _close_event_iter(event_iter: Any) -> None:
        aclose = getattr(event_iter, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:
            logger.debug("spawn_subagent: failed to close event iterator", exc_info=True)

# -*- coding: utf-8 -*-
"""AgentScope 2.0 ``spawn_subagent`` tool for QwenPaw Data."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from copy import deepcopy
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from contextlib import aclosing
from pathlib import Path, PurePath
from typing import Any

from agentscope.agent import Agent, ReActConfig
from agentscope.agent._config import ContextConfig, ModelConfig
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
from agentscope.middleware import MiddlewareBase
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionMode,
)
from agentscope.skill import Skill, SkillLoaderBase
from agentscope.state import AgentState
from agentscope.tool import ToolBase, ToolChunk, Toolkit

from ..artifact_paths import model_visible_path
from ..mcp_cm import inject_datasource_metadata
from ..runtime.context import RunContext
from ..domain.tool_confirmation import ToolConfirmation
from .middleware.prompt import DatasourcePromptMiddleware
from .middleware.sql_artifact import SqlArtifactMiddleware
from .middleware.tool_timeout import ToolTimeoutMiddleware
from .tools.plan_tools import PLAN_TOOL_NAMES
from .subagent_roles import load_roles
from .subagent_result import SubmitResult

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 6000
STREAM_POLL_SECONDS = 1.0
MAX_ITERS = 50
MAX_CONCURRENT = 4
MAX_TOTAL_SPAWNS = 20
_RESERVED_CHILD_TOOLS = PLAN_TOOL_NAMES | {
    "spawn_subagent",
    "submit_result",
    "ask_user_question",
    "CronJob",
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
    workspace_dir: PurePath | None,
    artifacts_root: PurePath | None,
    session_id: str | None,
) -> str:
    workspace_text = (
        str(model_visible_path(workspace_dir)) if workspace_dir else "(unknown)"
    )
    artifacts_text = (
        str(model_visible_path(artifacts_root)) if artifacts_root else "(unknown)"
    )
    if session_id:
        artifact_dir = str(model_visible_path(artifacts_text) / session_id)
        file_ref = "<filename>"
        artifact_scope = "当前会话"
    else:
        artifact_dir = str(model_visible_path(artifacts_text) / "<session_id>")
        file_ref = "<filename>"
        artifact_scope = "当前会话"

    return (
        "## QwenPaw Data 分析环境\n"
        f"- 模型可见 workspace 绝对路径: `{workspace_text}`\n"
        f"- 模型可见 artifacts 根目录: `{artifacts_text}`\n"
        "- 工具的相对路径以 agent workspace 为根。\n"
        f"- {artifact_scope}产物必须保存到 `{artifact_dir}`；"
        f"最终摘要中的文件路径使用 `{file_ref}`，相对当前 session artifacts 根，"
        "不要带 workspace、`artifacts` 或 session_id 前缀。\n\n"
        "- 禁止读取或改写其他 Session 的文件；新产物不得覆盖已有文件。\n"
    )


def _build_sub_prompt(
    task: str,
    context: str,
    upstream_outputs: dict[str, str],
    builtin_tool_names: Sequence[str],
    mcp_tool_names: Sequence[str],
    *,
    workspace_dir: PurePath | None = None,
    artifacts_root: PurePath | None = None,
    session_id: str | None = None,
    role_body: str = "",
    structured: bool = False,
) -> str:
    upstream_section = (
        "\n".join(f"- {nid}: {out}" for nid, out in upstream_outputs.items())
        if upstream_outputs
        else "无已完成的直接前置任务。"
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
        f"{role_body}\n\n"
        f"## 你的任务\n{task}\n\n"
        f"{_build_environment_section(workspace_dir=workspace_dir, artifacts_root=artifacts_root, session_id=session_id)}"
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
        "- 遇到无法解决的错误时描述问题并停止。\n\n"
        "## 输出要求\n"
        + (
            "完成后必须调用一次 submit_result，提供 summary 和相对当前会话的 artifacts；"
            "仅回复文字不算交付。"
            if structured
            else "任务完成后，你的最终回答必须是结构化摘要，格式：\n"
            "- **结论**：说明任务结果（做了什么、数据概况、关键发现）\n"
            "- **产出文件**：列出所有生成的文件路径\n"
            "- **异常**：如有问题或部分失败，在此说明；无异常则省略\n"
        )
    )


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", "") or "")


def _skill_name(skill: Any) -> str:
    if isinstance(skill, str):
        return Path(skill).name
    return _tool_name(skill) or Path(str(getattr(skill, "dir", ""))).name


def _skill_inputs(skills: Iterable[Any]) -> list[Any]:
    selected = []
    for skill in skills:
        if isinstance(skill, (str, Skill, SkillLoaderBase)):
            selected.append(skill)
        elif getattr(skill, "dir", None):
            selected.append(str(skill.dir))
    return selected


def _pick(items, names, *, kind, strict, key=_tool_name):
    if names is None:
        return list(items)
    available = {key(item): item for item in items}
    if strict and (missing := [name for name in names if name not in available]):
        raise ValueError(f"unavailable {kind}: {', '.join(missing)}")
    return [available[name] for name in names if name in available]


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
    """Agent variant that preserves QwenPaw Data CM datasource injection."""

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


class _UnattendedToolConfirmation(MiddlewareBase):
    """Apply the parent's approval policy without silently approving child tools.

    Child call IDs are not surfaced as interactive approval requests. Return a
    denial so the parent can perform the operation through its normal gate.
    """

    def __init__(self, run_context: RunContext) -> None:
        self._gate = ToolConfirmation(
            run_context.session_factory,
            run_context.chat_id,
            level=run_context.user_runtime_config.tool_confirmation_level,
        )

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        name = input_kwargs["tool_call"].name
        if self._gate.decide(name) != "allow":
            yield ToolConfirmation.denied_response(
                name,
                reason="requires approval; ask the parent agent to execute this operation",
            )
            return
        async for item in next_handler(**input_kwargs):
            yield item


class SpawnSubagent(ToolBase):
    """Spawn an in-process role-scoped sub-agent."""

    name: str = "spawn_subagent"
    description: str = (
        "Spawn an in-process sub-agent to execute one specific data task. "
        "Uses the current session's tools, permissions and TaskContext. "
        "Does not create or advance plan tasks."
    )
    is_state_injected: bool = False
    is_concurrency_safe: bool = True
    is_read_only: bool = False
    is_external_tool: bool = False
    is_mcp: bool = False
    mcp_name: str | None = None

    def __init__(
        self,
        *,
        run_context: RunContext,
        workspace_tools: Sequence[Any],
        workspace_mcps: Sequence[Any],
        workspace_skills: Sequence[Any],
        parent_agent_getter: Callable[[], Any] | None = None,
        cm_mcp_tool_prefixes: Iterable[str] = (),
    ) -> None:
        super().__init__()
        self._roles = load_roles(run_context.paths)
        self.description += (
            " Use type, description and prompt. Available roles:\n"
            + "\n".join(
                f"- {spec.name}: {spec.description}" for spec in self._roles.values()
            )
        )
        self.input_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "type": {"type": "string", "enum": list(self._roles)},
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Short task label",
                },
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Self-contained task and context",
                },
                "task": {
                    "type": "string",
                    "description": "Legacy OSS task (instead of type/description/prompt)",
                },
                "role": {
                    "type": "string",
                    "enum": list(self._roles),
                    "description": "Legacy OSS role",
                },
                "context": {"type": "string", "description": "Legacy OSS context"},
            },
        }
        self._ctx = run_context
        self._workspace = run_context.workspace
        self._workspace_tools = list(workspace_tools)
        self._workspace_mcps = list(workspace_mcps)
        self._workspace_skills = list(workspace_skills)
        self._parent_agent_getter = parent_agent_getter
        self._cm_mcp_tool_prefixes = set(cm_mcp_tool_prefixes)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._count_lock = asyncio.Lock()
        self._spawn_count = 0

    def bind_parent(self, agent: Agent) -> None:
        self._parent_agent_getter = lambda: agent

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="spawn_subagent is available in this Host run.",
        )

    async def __call__(  # type: ignore[override]
        self,
        task: str | None = None,
        role: str | None = None,
        context: str | None = None,
        *,
        type: str | None = None,
        description: str | None = None,
        prompt: str | None = None,
    ) -> AsyncGenerator[ToolChunk, None]:
        # Advertise the internal wire contract. Older persisted OSS calls still replay.
        native = any(v is not None for v in (type, description, prompt))
        if native:
            if any(v is not None for v in (task, role, context)) or not all(
                isinstance(v, str) and v.strip() for v in (type, description, prompt)
            ):
                yield _text_chunk(
                    "expected type, description and prompt",
                    state=ToolResultState.ERROR,
                    is_last=True,
                )
                return
            role, task = type, prompt
        else:
            role = "data_fetcher" if role is None else role
        if (
            not isinstance(task, str)
            or not task.strip()
            or not isinstance(role, str)
            or role not in self._roles
        ):
            yield _text_chunk(
                f"缺少任务或不支持的 role: '{role}'。当前支持: {tuple(self._roles)}",
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
            async with aclosing(
                self._run_subagent(
                    task=task,
                    role=role,
                    context=context or "",
                    structured=native or role != "data_fetcher",
                ),
            ) as stream:
                async for chunk in stream:
                    yield chunk

    async def _run_subagent(
        self,
        *,
        task: str,
        role: str,
        context: str,
        structured: bool = False,
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

        # Read a snapshot of the one authoritative plan; never schedule or
        # mutate it. Multiple active tasks are legal, so do not guess a node.
        plan = parent_agent.state.tasks_context.model_copy(deep=True)
        active = [item for item in plan.tasks if item.state == "in_progress"]
        current_node = active[0] if len(active) == 1 else None
        node_id = current_node.id if current_node else None
        graph_id = None  # Kept only as a nullable historical trace field.
        upstream_outputs = (
            {
                item.id: str(item.metadata.get("summary") or item.description)
                for item in plan.tasks
                if item.id in current_node.blocked_by and item.state == "completed"
            }
            if current_node
            else {}
        )
        session_id = self._ctx.session_id
        spec = self._roles[role]
        eligible_tools = [
            t
            for t in self._workspace_tools
            if _tool_name(t) not in _RESERVED_CHILD_TOOLS
        ]
        requested_tools = spec.tools
        # AgentScope's native Windows workspace exposes PowerShell instead of
        # Bash. The shipped presets can still use their file tools there. Do
        # not substitute a different shell or relax an authored role's policy.
        if role in {"report", "data_fetcher"} and requested_tools is not None:
            available = {_tool_name(t) for t in eligible_tools}
            if "PowerShell" in available and "Bash" not in available:
                requested_tools = tuple(n for n in requested_tools if n != "Bash")
        try:
            workspace_tools = _pick(
                eligible_tools,
                requested_tools,
                kind="tools",
                strict=structured,
            )
            workspace_mcps = _pick(
                self._workspace_mcps, spec.mcp, kind="MCP servers", strict=structured
            )
            workspace_skills = _skill_inputs(
                _pick(
                    self._workspace_skills,
                    spec.skills,
                    kind="skills",
                    key=_skill_name,
                    strict=structured,
                )
            )
        except ValueError as exc:
            yield _text_chunk(
                json.dumps({"status": "error", "summary": str(exc), "artifacts": []}),
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return
        submit = SubmitResult() if structured else None
        if submit is not None:
            workspace_tools.append(submit)
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
            workspace_dir=model_visible_path(self._workspace.workdir),
            artifacts_root=model_visible_path(
                self._ctx.paths.agent_artifact_dir(self._workspace)
            ).parent,
            session_id=session_id,
            role_body=spec.body,
            structured=structured,
        )
        agent_name = f"subagent-{role}-{node_id or 'free'}-{uuid.uuid4().hex[:4]}"

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
            middlewares: list[MiddlewareBase] = [
                _UnattendedToolConfirmation(self._ctx),
                ToolTimeoutMiddleware(run_context=self._ctx),
                SqlArtifactMiddleware(run_context=self._ctx),
                DatasourcePromptMiddleware(run_context=self._ctx),
            ]
            sub_agent = _SubAgent(
                name=agent_name,
                system_prompt=sys_prompt,
                model=model,
                toolkit=sub_toolkit,
                middlewares=middlewares,
                state=AgentState(
                    permission_context=parent_permission_context,
                ),
                offloader=getattr(parent_agent, "offloader", self._workspace),
                context_config=deepcopy(
                    getattr(parent_agent, "context_config", ContextConfig())
                ),
                model_config=deepcopy(
                    getattr(parent_agent, "model_config", ModelConfig())
                ),
                react_config=ReActConfig(
                    max_iters=min(
                        MAX_ITERS, self._ctx.user_runtime_config.react_max_iters
                    ),
                    interruption_raise_cancelled_error=True,
                ),
                request_context=dict(self._ctx.request_context),
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
        async with aclosing(
            self._stream_subagent(
                sub_agent=sub_agent,
                task_msg=task_msg,
                agent_name=agent_name,
                graph_id=graph_id,
                node_id=node_id,
                submit=submit,
            ),
        ) as stream:
            async for chunk in stream:
                yield chunk

    def _parent_agent(self) -> Any | None:
        if self._parent_agent_getter is None:
            return None
        try:
            return self._parent_agent_getter()
        except Exception:
            logger.debug("spawn_subagent: parent agent getter failed", exc_info=True)
            return None

    async def _stream_subagent(
        self,
        *,
        sub_agent: Agent,
        task_msg: Msg,
        agent_name: str,
        graph_id: str | None,
        node_id: str | None,
        submit: SubmitResult | None = None,
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
        error_chunk: ToolChunk | None = None
        delivered = False

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
                    # The SDK concatenates streamed text into the parent's tool
                    # result. Keep native delivery a single JSON document; retain
                    # progress in the trace and legacy streaming on the OSS path.
                    if chunk is not None and submit is None:
                        yield chunk
                    if (
                        submit is not None
                        and submit.delivery is not None
                        and isinstance(event, ToolResultEndEvent)
                        and event.state == ToolResultState.SUCCESS
                        and tool_names.get(event.tool_call_id) == submit.name
                    ):
                        delivered = True
                        break

                if confirmation is None or delivered:
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
            error_chunk = _text_chunk(
                f"Sub-agent 超时（{TIMEOUT_SECONDS}s）",
                state=ToolResultState.ERROR,
                is_last=True,
            )
        except Exception as exc:
            logger.warning("spawn_subagent: sub-agent execution failed", exc_info=True)
            error_chunk = _text_chunk(
                f"Sub-agent 异常: {exc}",
                state=ToolResultState.ERROR,
                is_last=True,
            )
        finally:
            # Cancellation and generator closure must also drain the child before
            # releasing the concurrency slot or returning a terminal response.
            if event_task is not None:
                if not event_task.done():
                    event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
            if event_iter is not None:
                await self._close_event_iter(event_iter)

        if error_chunk is not None:
            if submit is None:
                yield error_chunk
            else:
                yield _text_chunk(
                    json.dumps(
                        {
                            "status": "error",
                            "summary": error_chunk.content[0].text,
                            "artifacts": [],
                        },
                        ensure_ascii=False,
                    ),
                    state=ToolResultState.ERROR,
                    is_last=True,
                )
            return

        summary = "".join(text_parts).strip() or "".join(thinking_parts).strip()
        if not summary:
            summary = "(no output)"
        state = ToolResultState.SUCCESS
        if submit is not None:
            if not delivered or submit.delivery is None:
                payload = {
                    "status": "error",
                    "summary": "No result submitted",
                    "artifacts": [],
                }
                state = ToolResultState.ERROR
            else:
                payload = {"status": "completed", **submit.delivery.model_dump()}
            summary = json.dumps(payload, ensure_ascii=False)
        trace_meta = {
            "type": "subagent_trace",
            "agent_name": agent_name,
            "graph_id": graph_id,
            "node_id": node_id,
            "entries": trace_log,
        }
        yield _text_chunk(
            summary,
            state=state,
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
            logger.debug(
                "spawn_subagent: failed to close event iterator", exc_info=True
            )

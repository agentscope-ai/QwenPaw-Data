# -*- coding: utf-8 -*-
"""``cron_job``: let the agent manage the caller's own scheduled runs.

The REST router (``api/routers/cron.py``) exposes the same operations; this
tool reuses the identical store + ``CronManager`` pair so both entry points
stay consistent. Jobs are always scoped to the calling user.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk
from pydantic import BaseModel, ConfigDict, Field

from ...api.models.cron import CronJobWrite, ScheduleSpec

logger = logging.getLogger(__name__)

CRON_JOB_TOOL_NAME = "cron_job"

# Strong references so a detached run is not garbage-collected mid-flight.
_RUNNING: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class CronToolServices:
    """The collaborators this tool shares with the cron REST router."""

    cron: Any
    cron_manager: Any
    sessions: Any


class _CronJobParams(BaseModel):
    # Reject unexpected tool input rather than silently ignoring it.
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "list",
        "get",
        "create",
        "replace",
        "delete",
        "pause",
        "resume",
        "run",
    ] = Field(description="要执行的操作，见工具说明。")
    job_id: str | None = Field(
        default=None,
        description="任务 id。get / replace / delete / pause / resume / run 必填。",
    )
    name: str | None = Field(
        default=None,
        description="任务名称。create / replace 必填。",
    )
    enabled: bool = Field(
        default=True,
        description="是否启用调度。create / replace 可用，默认 true。",
    )
    message: str | None = Field(
        default=None,
        description="到点或 run 时作为用户输入交给 Agent。create / replace 必填。",
    )
    datasource_id: str | None = Field(
        default=None,
        description="执行时使用的数据源。不填则用当前会话的数据源。",
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "复用的会话 id。可选；不填则每次触发都新建会话。"
            "复用时执行侧会等该会话当前轮结束后再开新 chat。"
        ),
    )
    schedule_type: Literal["cron", "once"] | None = Field(
        default=None,
        description="调度种类。create / replace 必填：cron 周期，once 单次。",
    )
    cron_expression: str | None = Field(
        default=None,
        description="周期表达式，5 段（分 时 日 月 周）。schedule_type=cron 时必填。",
    )
    run_at: datetime | None = Field(
        default=None,
        description="单次执行的 ISO 时间。schedule_type=once 时必填。",
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        description="调度时区。默认 Asia/Shanghai。",
    )


def _require_job_id(params: _CronJobParams) -> str:
    job_id = (params.job_id or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    return job_id


class CronJobTool(ToolBase):
    """Manage the calling user's scheduled analysis runs."""

    name = CRON_JOB_TOOL_NAME
    description = (
        "管理当前用户的定时任务，只能看到和修改这个用户自己的任务。"
        "到点或手动 run 时，会开一场会话，把 message 当作用户输入交给 Agent；"
        "本工具立刻返回，不等那次会话跑完。"
        "action 必填："
        "list 列出全部任务，不需要其他参数；"
        "get 查看一条，必填 job_id；"
        "create 新建并写入调度器，必填 name、message、schedule_type，"
        "周期任务再填 cron_expression，单次任务再填 run_at，"
        "datasource_id 不填则沿用当前会话的数据源，enabled 默认 true，"
        "返回新建任务（含 id）；"
        "replace 用新内容整单覆盖已有任务，必填 job_id 以及 create 所需的全部字段；"
        "delete 从库和调度器删除，必填 job_id，删掉不能恢复；"
        "pause 暂停调度（任务保留，enabled 变 false），必填 job_id；"
        "resume 恢复调度，必填 job_id；"
        "run 立刻按该任务当前的 message 跑一次，不改调度时间，必填 job_id。"
        "用户要「创建并马上跑一次」：先 create，再用返回的 id 调一次 run。"
    )
    is_concurrency_safe = False
    is_read_only = False
    is_external_tool = False
    is_state_injected = False
    is_mcp = False
    mcp_name = None

    def __init__(
        self,
        services_getter: Callable[[], CronToolServices],
        *,
        request_context_getter: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._services_getter = services_getter
        self._request_context_getter = request_context_getter
        self.input_schema = _CronJobParams.model_json_schema()

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        _ = (tool_input, context)
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} is always allowed.",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            params = _CronJobParams.model_validate(kwargs)
            result = await self._dispatch(params)
        except Exception as exc:
            logger.warning(
                "cron_job tool failed: action=%s error_type=%s",
                kwargs.get("action"),
                type(exc).__name__,
            )
            return ToolChunk(
                content=[TextBlock(type="text", text=f"CronJobError: {exc}")],
                state=ToolResultState.ERROR,
            )
        return ToolChunk(
            content=[
                TextBlock(
                    type="text",
                    text=json.dumps(result, ensure_ascii=False, default=str),
                ),
            ],
        )

    def _context(self) -> dict[str, Any]:
        if self._request_context_getter is None:
            return {}
        return self._request_context_getter() or {}

    def _user_id(self) -> str:
        user_id = str(self._context().get("user_id") or "").strip()
        if not user_id:
            raise ValueError("cron jobs require a caller identity")
        return user_id

    async def _dispatch(self, params: _CronJobParams) -> dict[str, Any]:
        services = self._services_getter()
        cron = services.cron
        manager = services.cron_manager
        user_id = self._user_id()

        if params.action == "list":
            jobs = await cron.list(user_id)
            return {"jobs": jobs, "count": len(jobs)}
        if params.action == "get":
            return {"job": await cron.get(user_id, _require_job_id(params))}
        if params.action == "create":
            body = await self._prepare_write(services, params)
            job = await cron.create(user_id, body)
            manager.sync(job)
            return {"job": job}
        if params.action == "replace":
            job_id = _require_job_id(params)
            body = await self._prepare_write(services, params)
            job = await cron.replace(user_id, job_id, body)
            manager.sync(job)
            return {"job": job}
        if params.action == "delete":
            job_id = _require_job_id(params)
            await cron.delete(user_id, job_id)
            manager.remove(job_id)
            return {"ok": True, "deleted": job_id}
        if params.action in {"pause", "resume"}:
            job = await cron.set_enabled(
                user_id,
                _require_job_id(params),
                params.action == "resume",
            )
            manager.sync(job)
            return {"job": job}

        job = await cron.get(user_id, _require_job_id(params))
        # Detached like the REST run endpoint: the fired chat outlives this call.
        task = asyncio.create_task(
            manager.run(job),
            name=f"cron-run-{job['id']}",
        )
        _RUNNING.add(task)
        task.add_done_callback(_RUNNING.discard)
        return {"ok": True, "job": job}

    async def _prepare_write(
        self,
        services: CronToolServices,
        params: _CronJobParams,
    ) -> CronJobWrite:
        if params.schedule_type is None:
            raise ValueError("schedule_type is required")
        context = self._context()
        datasource_id = (params.datasource_id or "").strip() or str(
            context.get("datasource_id") or "",
        ).strip()
        if not datasource_id:
            raise ValueError("datasource_id is required")
        body = CronJobWrite(
            name=params.name or "",
            enabled=params.enabled,
            message=params.message or "",
            datasource_id=datasource_id,
            session_id=params.session_id,
            schedule=ScheduleSpec(
                type=params.schedule_type,
                cron=params.cron_expression,
                run_at=params.run_at,
                timezone=params.timezone,
            ),
        )
        if body.session_id:
            # Mirrors the router's check so a job cannot pin a missing session.
            await services.sessions.get(body.session_id)
        return body

# -*- coding: utf-8 -*-
"""CronJob Host tool: current user's jobs via repo; run is picked up from DB."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw_data.host.core.api.models.cron import CronJobWrite, ScheduleSpec
from qwenpaw_data.host.core.cron.files import JobFiles
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.repositories.channel_binding import ChannelBindingRepository
from qwenpaw_data.host.core.repositories.cron import CronJobRepository

if TYPE_CHECKING:
    from qwenpaw_data.host.core.runtime.context import RunContext


class _CronJobParams(BaseModel):
    # Reject non-expected tool input parameters
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "create",
        "list",
        "list_targets",
        "get",
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
        description="到点或 run 时作为用户输入。create / replace 必填。",
    )
    datasource_id: str | None = Field(
        default=None,
        description="执行时使用的数据源。create / replace 不填则用当前会话数据源。",
    )
    channel: str | None = Field(
        default=None,
        description="推送端。create / replace 不填则 console；list_targets 必填。",
    )
    session_policy: Literal["reuse", "new_each_run"] = Field(
        default="reuse",
        description="reuse：任务自有一个会话，第一次触发才建；new_each_run：每次触发新建会话。",
    )
    attachment_filenames: list[str] | None = Field(
        default=None,
        description=(
            "从当前对话拷到任务的附件文件名（uploads/{session_id}/ 下的文件名，不含目录）。"
            "可选，必须是当前 Session 已上传的文件。"
        ),
    )
    target_external_key: str | None = Field(
        default=None,
        description="IM 推送目标。非 console 时必填，先用 list_targets 查。",
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


class CronJobTool(ToolBase):
    name = "CronJob"
    description = (
        "管理当前用户的定时任务。只能看到和改这个用户的任务。"
        "创建的定时任务会在另一个会话开启。"
        "到点或手动 run 时，按任务的 channel 把 message "
        "当作用户输入交给 Agent。本工具立刻返回，不等那次会话跑完。"
        "session_policy 默认 reuse（首次触发才建会话）；new_each_run 每次新建。"
        "不填 channel 就是 console；IM（feishu / dingtalk / wecom / wechat）"
        "先 list_targets 再带上 target_external_key。"
        "attachment_filenames 可选，按文件名拷当前对话附件到任务，每次触发都会带上；"
        "用户要求带上附件时必填，文件名取自用户消息里的附件路径。"
        "action 必填："
        "list 列出全部任务，不用其他参数；"
        "list_targets 列出该 channel 可推送的群/人，必填 channel；"
        "get 查看一条，必填 job_id；"
        "create 新建任务，必填 name、message、schedule_type，"
        "周期再填 cron_expression，单次再填 run_at；"
        "datasource_id 可选（不填则用当前会话数据源），enabled 默认 true，"
        "返回新建任务（含 id）；"
        "replace 用新内容整单覆盖已有任务，"
        "必填 job_id 以及 create 所需的全部字段；不改已回写的 session_id；"
        "delete 从库删除，必填 job_id，删掉不能恢复；"
        "pause 暂停，到点不再跑，任务还在，enabled 变为 false，必填 job_id；"
        "resume 恢复调度，必填 job_id；"
        "run 立刻按该任务当前的 message 开一场会话，不改调度时间，必填 job_id。"
        "timezone 默认 Asia/Shanghai。"
        "用户要「创建并马上跑一次」：先 create，再用返回的 id 再调一次 run。"
    )
    is_concurrency_safe = False
    is_read_only = False

    def __init__(self, run_context: RunContext) -> None:
        super().__init__()
        self._run_context = run_context
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
            return ToolChunk(
                content=[
                    TextBlock(text=json.dumps(result, ensure_ascii=False, default=str))
                ],
            )
        except Exception as exc:
            return ToolChunk(
                content=[TextBlock(text=f"CronJobError: {exc}")],
                state=ToolResultState.ERROR,
            )

    async def _dispatch(self, params: _CronJobParams) -> Any:
        async with get_session_factory()() as db:
            repo = CronJobRepository(db, self._run_context.identity)
            if params.action == "list":
                jobs = await repo.list()
                result: Any = {"jobs": jobs, "count": len(jobs)}
            elif params.action == "list_targets":
                result = await self._list_targets(db, params)
            elif params.action == "get":
                result = {"job": await repo.get(self._require_job_id(params))}
            elif params.action == "create":
                job = await repo.create(await self._prepare_write(db, params))
                job = await self._copy_attachments(db, repo, job["id"], params) or job
                result = {"job": job}
            elif params.action == "replace":
                job_id = self._require_job_id(params)
                job = await repo.replace(job_id, await self._prepare_write(db, params))
                job = await self._copy_attachments(db, repo, job_id, params) or job
                result = {"job": job}
            elif params.action == "delete":
                job_id = self._require_job_id(params)
                await repo.delete(job_id)
                result = {"ok": True, "deleted": job_id}
            elif params.action == "pause":
                result = {
                    "job": await repo.set_enabled(self._require_job_id(params), False)
                }
            elif params.action == "resume":
                result = {
                    "job": await repo.set_enabled(self._require_job_id(params), True)
                }
            else:
                result = {
                    "ok": True,
                    "job": await repo.run_now(self._require_job_id(params)),
                }
            await db.commit()
        return result

    async def _prepare_write(self, db: Any, params: _CronJobParams) -> CronJobWrite:
        if params.schedule_type is None:
            raise ValueError("schedule_type is required")
        datasource_id = (params.datasource_id or "").strip() or (
            str(self._run_context.request_context.get("datasource_id") or "").strip()
        )
        if not datasource_id:
            raise ValueError("datasource_id is required")
        body = CronJobWrite(
            name=params.name or "",
            enabled=params.enabled,
            message=params.message or "",
            datasource_id=datasource_id,
            channel=(params.channel or "").strip() or "console",
            session_policy=params.session_policy,
            target_external_key=params.target_external_key,
            source_chat_id=self._run_context.chat_id,
            schedule=ScheduleSpec(
                type=params.schedule_type,
                cron=params.cron_expression,
                run_at=params.run_at,
                timezone=params.timezone,
            ),
        )
        if body.channel != "console":
            from qwenpaw_data.host.core.cron.manager import CronManager

            await (
                CronManager.require().channel_manager.is_channel_eligible_for_cron_job(
                    self._run_context.identity, body
                )
            )
        return body

    async def _list_targets(self, db: Any, params: _CronJobParams) -> dict[str, Any]:
        channel = (params.channel or "").strip()
        if not channel:
            raise ValueError("channel is required")
        targets = await ChannelBindingRepository(db).list_by_channel(
            self._run_context.identity, channel
        )
        public = [
            {
                "external_key": t["external_key"],
                "target_type": t["target_type"],
                "display_name": t["display_name"],
                "last_active_at": t["last_active_at"],
            }
            for t in targets
        ]
        return {"targets": public, "count": len(public)}

    async def _copy_attachments(
        self,
        db: Any,
        repo: CronJobRepository,
        job_id: str,
        params: _CronJobParams,
    ) -> dict[str, Any] | None:
        names = [
            item.strip() for item in (params.attachment_filenames or []) if item.strip()
        ]
        if not names:
            return None
        await JobFiles(self._run_context.identity, job_id).copy_from_session(
            db,
            session_id=self._run_context.session_id,
            filenames=names,
        )
        return await repo.get(job_id)

    @staticmethod
    def _require_job_id(params: _CronJobParams) -> str:
        job_id = (params.job_id or "").strip()
        if not job_id:
            raise ValueError("job_id is required")
        return job_id

"""单数据源探查状态详情：懒创建 6 阶段行（系统写）+ 返回各阶段状态与推导总状态。

- 越权/不存在：目标数据源不在 principal 的 tenant/workspace → 404（不泄露存在性，SPEC §13.6）。
- 懒创建：首次访问补齐 6 行 NOT_STARTED，tenant/workspace 从数据源派生、created_by 取
  principal.user_id；系统写，不经 require_admin（SPEC §13.7）。
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from qwenpaw_data_auth import Principal

from context_manager.heartbeat import is_fresh, lock_scope
from profiling.models.probe import (
    OVERALL_COMPLETED,
    STAGES,
    STATUS_COMPLETED,
    STATUS_NOT_STARTED,
    STATUS_PROBING,
    STATUS_REVIEWING,
    STATUS_SKIPPED,
    TASK_RUNNING,
    DatasourceStatusResponse,
    StageStatusItem,
    derive_overall,
)
from profiling.repositories import probe_stage_repo as stage_repo
from profiling.repositories import probe_task_repo as task_repo
from profiling.repositories import staging_repo
from profiling.services import promote as promote_svc
from profiling.stages import StageDef
from semantic_config.errors import BadRequestError, NotFoundNoBody
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import datasource_repo as ds_repo


async def get_status(
    db: aiosqlite.Connection, datasource_id: str, principal: Principal
) -> DatasourceStatusResponse:
    ds = await ds_repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if ds is None:
        # 数据源不存在或不属于当前租户/工作空间 → 404，不泄露存在性。
        raise NotFoundNoBody()

    # 懒创建（系统写）：归属从数据源派生，created_by 取当前用户。
    await stage_repo.ensure_stages(
        db,
        datasource_id,
        tenant_id=ds["tenant_id"],
        workspace_id=ds["workspace_id"],
        created_by=principal.user_id,
    )

    rows = await stage_repo.list_by_datasource(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    by_stage = {row["stage"]: row for row in rows}

    items: list[StageStatusItem] = []
    for stage in STAGES:
        row = by_stage.get(stage)
        items.append(
            StageStatusItem(
                stage=stage,
                status=row["status"] if row else STATUS_NOT_STARTED,
                current_task_id=row["current_task_id"] if row else None,
            )
        )
    overall = derive_overall([it.status for it in items])
    return DatasourceStatusResponse(
        datasource_id=datasource_id, overall_status=overall, stages=items
    )


async def _require_ds(db, datasource_id, principal):
    ds = await ds_repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if ds is None:
        raise NotFoundNoBody()
    return ds


async def _sync_if_all_terminal(
    db: aiosqlite.Connection,
    datasource_id: str,
    principal: Principal,
    runtime: Any,
) -> dict | None:
    """全部阶段终态且至少一个 COMPLETED 后，将正式表发布到权威 MG。

    全部 SKIPPED 不触发：没有任何 promote 落表，而 payload 的 drop_semantic_first
    会清空图里可能由其它路径写入的语义数据。
    先提交正式表和阶段状态，再用独立短会话组装快照；图发布复用新版 weaving，
    不持有控制库事务。发布失败只记录在返回值里，不回滚已提交的建模结果。
    """
    statuses = await stage_repo.status_map(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    resolved = [statuses.get(s, STATUS_NOT_STARTED) for s in STAGES]
    if derive_overall(resolved) != OVERALL_COMPLETED:
        return None
    if not any(s == STATUS_COMPLETED for s in resolved):
        return None
    if runtime is None:
        return {
            "synced": False,
            "status": "SKIPPED",
            "error_msg": "graph runtime unavailable",
        }

    import uuid

    from context_manager.metadata import SemanticImportRequest
    from context_manager.auth import WriteOwner, get_authorization
    from semantic_config.engine import get_session_factory
    from semantic_config.services import weave_assembler

    # SQL commit failure must fail the request, not become a downstream graph error.
    await db.commit()
    task_id = f"SYNC_{uuid.uuid4().hex[:12]}"
    try:
        scope = runtime.issue_graph_write_scope(
            get_authorization(),
            owner=WriteOwner.WORKSPACE,
            zone="metadata",
        )
        service = runtime.metadata_weaving(scope)
        async with get_session_factory()() as snapshot_db:
            payload = await weave_assembler.assemble(
                snapshot_db,
                datasource_id,
                task_id,
                principal.tenant_id,
                principal.active_workspace_id,
            )
        warnings = []

        async def before_commit(plan):
            warnings.extend(
                warning for warning in plan.warnings if warning not in warnings
            )

        await service.import_snapshot(
            SemanticImportRequest.model_validate(payload),
            scope,
            before_commit=before_commit,
        )
        status = "DEGRADED" if warnings else "SUCCESS"
        error_msg = "; ".join(warnings)[:1000] or None
        return {
            "synced": True,
            "task_id": task_id,
            "status": status,
            "error_msg": error_msg,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "synced": True,
            "task_id": task_id,
            "status": "FAILED",
            "error_msg": str(exc)[:1000],
        }


async def skip(
    db: aiosqlite.Connection,
    datasource_id: str,
    stage: str,
    principal: Principal,
    *,
    runtime: Any = None,
) -> dict:
    """在同一事务中取消当前执行并跳过阶段。"""
    require_admin(principal)
    ds = await _require_ds(db, datasource_id, principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    await lock_scope(db, f"probe:{tid}:{wid}:{datasource_id}:{stage}")
    await stage_repo.ensure_stages(
        db,
        datasource_id,
        tenant_id=ds["tenant_id"],
        workspace_id=ds["workspace_id"],
        created_by=principal.user_id,
    )
    row = await stage_repo.find_one(db, datasource_id, stage, tid, wid)
    current = row["status"] if row else STATUS_NOT_STARTED
    if current not in (STATUS_NOT_STARTED, STATUS_PROBING, STATUS_REVIEWING):
        raise BadRequestError("当前阶段状态不允许跳过")
    latest = await task_repo.find_latest(db, datasource_id, stage, tid, wid)
    fresh_other = (
        latest is not None
        and latest["status"] == TASK_RUNNING
        and is_fresh(latest["heartbeat_at"])
        and (row is None or row["current_task_id"] != latest["task_id"])
    )
    if fresh_other:
        return {
            "stage": stage,
            "status": current,
            "task_id": latest["task_id"],
        }
    stopped_task_id = None
    if (
        latest is not None
        and latest["status"] == TASK_RUNNING
        and row is not None
        and row["current_task_id"] == latest["task_id"]
        and await task_repo.finish_if_running(db, latest["task_id"], "STOPPED")
    ):
        stopped_task_id = latest["task_id"]
    wrote = await stage_repo.update_status(
        db,
        datasource_id,
        stage,
        STATUS_SKIPPED,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
        expected_status=current,
        expected_task_id=row["current_task_id"] if row is not None else None,
    )
    if not wrote:
        fresh = await stage_repo.find_one(db, datasource_id, stage, tid, wid)
        return {
            "stage": stage,
            "status": fresh["status"] if fresh is not None else current,
            "task_id": fresh["current_task_id"] if fresh is not None else None,
        }
    await db.commit()
    if stopped_task_id is not None:
        from profiling.services import orchestrator

        token = orchestrator._TOKENS.get((tid, wid, datasource_id, stage))
        if token is not None and token.task_id == stopped_task_id:
            token.cancelled = True
    result: dict = {"stage": stage, "status": STATUS_SKIPPED, "task_id": None}
    sync = await _sync_if_all_terminal(db, datasource_id, principal, runtime)
    if sync is not None:
        result["graph_sync"] = sync
    return result


async def complete(
    db: aiosqlite.Connection,
    datasource_id: str,
    stage: str,
    principal: Principal,
    *,
    runtime: Any = None,
) -> dict:
    """完成阶段：校验完成约束 + promote（口径阶段不 promote），置 COMPLETED（SPEC §6.4）。

    当本次 complete 使全部阶段进入终态时，提交正式表并通过新版 weaving 发布到权威 MG
    （发布失败不回滚阶段状态）。
    """
    require_admin(principal)
    ds = await _require_ds(db, datasource_id, principal)
    tid, wid = ds["tenant_id"], ds["workspace_id"]
    await lock_scope(db, f"probe:{tid}:{wid}:{datasource_id}:{stage}")
    if await task_repo.has_fresh_running(
        db,
        datasource_id=datasource_id,
        stage=stage,
        tenant_id=tid,
        workspace_id=wid,
    ):
        raise BadRequestError("该阶段已有探查任务运行中")
    await stage_repo.ensure_stages(
        db,
        datasource_id,
        tenant_id=ds["tenant_id"],
        workspace_id=ds["workspace_id"],
        created_by=principal.user_id,
    )
    result = await promote_svc.complete(db, datasource_id, stage, principal)

    sync = await _sync_if_all_terminal(db, datasource_id, principal, runtime)
    if sync is not None:
        result["graph_sync"] = sync
    return result


async def list_results(
    db: aiosqlite.Connection,
    sdef: StageDef,
    datasource_id: str,
    principal: Principal,
    page: int,
    size: int,
    filter_val: str | None = None,
) -> Page[dict]:
    await _require_ds(db, datasource_id, principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    fcol = sdef.filter_col if filter_val else None
    total = await staging_repo.count(
        db, sdef.table, datasource_id, tid, wid, filter_col=fcol, filter_val=filter_val
    )
    rows = await staging_repo.find_page(
        db,
        sdef.table,
        datasource_id,
        tid,
        wid,
        size,
        offset(page, size),
        filter_col=fcol,
        filter_val=filter_val,
    )
    return Page(
        records=[staging_repo.to_public_dict(r) for r in rows],
        total=total,
        page=page,
        size=size,
    )

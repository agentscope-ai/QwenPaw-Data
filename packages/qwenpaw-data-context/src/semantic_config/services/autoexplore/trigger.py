"""自动探查触发：库内条件认领后返回，后台运行。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from semantic_config.repositories import explore_repo
from semantic_config.services.autoexplore import pipeline
from semantic_config.services.autoexplore.pipeline import _ExploreRun

log = logging.getLogger("semantic_config.autoexplore.trigger")

_ACTIVE_TASKS: dict[str, asyncio.Task] = {}


async def _new_session():
    """后台 worker 自建会话（请求会话随响应提交关闭，不可复用）。"""
    from semantic_config.engine import get_session_factory

    return get_session_factory()()


async def schedule_explore(
    *,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    graph_access: Any,
    force_refresh: bool = False,
) -> dict:
    """受理一次探查。新鲜心跳拒绝第二次。"""
    run_id = pipeline.generate_run_id()
    async with await _new_session() as db:
        scope = await explore_repo.ensure_scope(
            db, tenant_id, workspace_id, datasource_id
        )
        if not scope["enabled"]:
            await db.commit()
            return {"status": "disabled"}
        epoch = await explore_repo.claim_run(db, tenant_id, workspace_id, datasource_id)
        if epoch is None:
            await db.rollback()
            return {"status": "already_running"}
        await db.commit()
    execution = _ExploreRun(tenant_id, workspace_id, datasource_id, int(epoch))
    task = asyncio.create_task(
        _explore_worker(
            run_id=run_id,
            datasource_id=datasource_id,
            graph_access=graph_access,
            force_refresh=force_refresh,
            execution=execution,
        )
    )
    _ACTIVE_TASKS[run_id] = task
    task.add_done_callback(lambda _: _ACTIVE_TASKS.pop(run_id, None))
    return {"status": "accepted", "run_id": run_id}


async def _explore_worker(
    *, run_id, datasource_id, graph_access, force_refresh, execution
) -> None:
    try:
        service, scope = graph_access
        async with await _new_session() as db:
            result = await service.explore(
                db,
                datasource_id,
                scope,
                force_refresh=force_refresh,
                run_id=run_id,
                execution=execution,
            )
            log.info(
                "autoexplore finished run=%s status=%s", result.run_id, result.status
            )
    except Exception:
        log.exception("autoexplore worker failed run=%s", run_id)


async def maybe_trigger_first_weave_explore(
    db,
    *,
    datasource_id: str,
    task_id: str,
    status: str,
    tenant_id: str,
    workspace_id: str,
    graph_access: Any,
) -> str | None:
    """weave submit 单写入点钩子：命中首织完成 → 异步触发；返回 run_id 或 None。

    判定（§2.1/§2.2）：
    1. 本次 status ∈ {SUCCESS, DEGRADED}（Q7）；
    2. 同 ds 历史中无其他已完成（SUCCESS/DEGRADED）任务；
    3. explore_scope 行尚不存在（存在即已初始化，二次编织不重触发）。
    """
    if status not in explore_repo.FIRST_WEAVE_STATUS:
        return None
    predecessors = await explore_repo.count_first_weave_predecessors(
        db, tenant_id, workspace_id, datasource_id, exclude_task_id=task_id
    )
    if predecessors > 0:
        return None
    if (
        await explore_repo.get_scope(db, tenant_id, workspace_id, datasource_id)
        is not None
    ):
        return None
    await db.commit()
    accepted = await schedule_explore(
        datasource_id=datasource_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        graph_access=graph_access,
    )
    if accepted["status"] != "accepted":
        return None
    log.info(
        "autoexplore scheduled by first weave ds=%s task=%s run=%s",
        datasource_id,
        task_id,
        accepted["run_id"],
    )
    return accepted["run_id"]

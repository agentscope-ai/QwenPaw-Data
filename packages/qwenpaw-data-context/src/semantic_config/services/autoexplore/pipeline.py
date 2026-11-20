"""mg-088 物理层自动探查：10 步管线编排（spec §3）。

::

    ① 读 explore_scope（无则自动生成默认行） → enabled=0 直接 disabled
    ② PhysicalMappingService 解析关系库连接配置  [失败 → failed 留痕]
    ③ extract_metadata（TTL 缓存；手动触发 force_refresh）
    ④ scope 过滤（schema 白名单 → include → exclude）
    ⑤ 两段全名拼装（ODPS 补 project. 前缀）+ table_limit 截断（字典序稳定）
    ⑥ 人工优先列级跳过（编辑器层 table_column 存在性 = 人工标记）
    ⑦ 结构指纹比对 → 未变表跳过（不写图、不采样）
    ⑧ sample_values 抽样（预算/注入防护/静默降级）
    ⑨ 组装行 → upsert_rows 双路径直写（不走 import 全链；50 表/批；写预算 600s）
    ⑩ explore_state / explore_scope 状态回写（run_id = EXPLORE_<ts>_<rand>）

边界（spec §4）：profiling 零 import；host 零改；图契约零新标签/边/属性；
不回写编辑器层；图读写通过注入的服务完成，不导入 context_manager。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextlib import asynccontextmanager

from context_manager.heartbeat import Heartbeat
from semantic_config.db import _tz
from semantic_config.repositories import explore_repo
from semantic_config.services.autoexplore.assembly import (
    assemble_table_rows,
    split_manual_columns,
)
from semantic_config.services.autoexplore.fingerprint import table_fingerprint
from semantic_config.services.autoexplore.sampling import (
    connector_can_sample,
    sample_columns,
)
from semantic_config.services.autoexplore.scope_filter import (
    apply_table_limit,
    filter_manifest_tables,
    full_table_name,
    sampling_enabled,
    split_patterns,
)
from semantic_config.tables import physical_table, table_column

log = logging.getLogger("semantic_config.autoexplore.pipeline")

#: 大库限流（§3.8）：整轮墙钟预算（含采样）/ 采样预算（含在上）/ 写批大小
RUN_BUDGET_SECS = 600
SAMPLE_BUDGET_SECS = 300
TABLE_BATCH_SIZE = 50

_RAND_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def generate_run_id() -> str:
    """``EXPLORE_<YYYYmmddHHMMSS>_<4rand>``（仿 weave _generate_task_id，§3.9）。"""
    ts = datetime.now(_tz()).strftime("%Y%m%d%H%M%S")
    rand = "".join(random.choice(_RAND_CHARS) for _ in range(4))
    return f"EXPLORE_{ts}_{rand}"


@dataclass
class ExploreResult:
    status: str  # ok | partial | failed | disabled
    run_id: str
    counts: dict = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------- #
# 可注入缝隙（重依赖懒加载；测试 monkeypatch 点）
# ---------------------------------------------------------------------- #
def _resolve_connector(access, datasource_id: str, *, force_refresh: bool):
    service, scope = access
    return service.resolve_source(datasource_id, scope, force_refresh=force_refresh)


def _extract(connector, schemas, *, datasource_id: str, force_refresh: bool):
    """Read physical metadata through the scoped discovery source."""
    return connector.extract_metadata(schemas)


def _write_graph(access, sheet: str, rows: list[dict], *, datasource_id: str) -> int:
    service, scope = access
    return service.write_rows(sheet, rows, datasource_id, scope)


def _normalize_type(raw: str) -> str:
    from semantic_config.models.datasource_config import resolve_type

    return resolve_type(raw).value


async def _manual_column_keys(
    db: AsyncSession, tenant_id: str, workspace_id: str, datasource_id: str
) -> set[tuple[str, str]]:
    """编辑器层 table_column 在场集 (两段全名, 列名) —— 人工优先标记（§3.5）。

    table_column 不存 table_name，表名经 join physical_table（table_id）反解。
    """
    res = await db.execute(
        select(physical_table.c.table_name, table_column.c.column_name)
        .select_from(
            table_column.join(
                physical_table,
                and_(
                    physical_table.c.id == table_column.c.table_id,
                    physical_table.c.tenant_id == table_column.c.tenant_id,
                    physical_table.c.workspace_id == table_column.c.workspace_id,
                    physical_table.c.is_deleted == 0,
                ),
            )
        )
        .where(
            table_column.c.tenant_id == tenant_id,
            table_column.c.workspace_id == workspace_id,
            table_column.c.datasource_id == datasource_id,
            table_column.c.is_deleted == 0,
        )
    )
    return {(r["table_name"], r["column_name"]) for r in res.mappings().all()}


async def _fail(
    db: AsyncSession,
    tenant_id: str,
    workspace_id: str,
    datasource_id: str,
    run_id: str,
    counts: dict,
    exc: Exception,
) -> ExploreResult:
    msg = str(exc)[:1000]
    log.warning("autoexplore failed ds=%s run=%s: %s", datasource_id, run_id, msg)
    return ExploreResult(status="failed", run_id=run_id, counts=counts, error=msg)


# ---------------------------------------------------------------------- #
# 主管线
# ---------------------------------------------------------------------- #
class ExploreLost(RuntimeError):
    """Another round owns this datasource, so this worker must not finish it."""


class _ExploreRun:
    def __init__(
        self, tenant_id: str, workspace_id: str, datasource_id: str, epoch: int
    ):
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        self.datasource_id = datasource_id
        self.epoch = epoch
        self.execution_id = str(epoch)

    async def guard(self, db) -> None:
        """Hold this running round until the caller's transaction commits."""
        if (
            await explore_repo.touch_run(
                db, self.tenant_id, self.workspace_id, self.datasource_id, self.epoch
            )
            != 1
        ):
            raise ExploreLost(self.datasource_id)

    @asynccontextmanager
    async def running(self, session_factory):
        async def touch() -> None:
            session = session_factory()
            try:
                await explore_repo.touch_run(
                    session,
                    self.tenant_id,
                    self.workspace_id,
                    self.datasource_id,
                    self.epoch,
                )
                await session.commit()
            finally:
                await session.close()

        heartbeat = Heartbeat(touch)
        heartbeat.start()
        try:
            yield
        finally:
            await heartbeat.stop()


async def run_explore(
    db: AsyncSession,
    *,
    datasource_id,
    tenant_id,
    workspace_id,
    user_id,
    graph_access,
    run_sync: Callable[..., Awaitable],
    force_refresh=False,
    run_id=None,
    connector=None,
    ds_config=None,
    execution=None,
) -> ExploreResult:
    """Both scheduled and direct callers use the same SQL execution ownership."""
    run_id = run_id or generate_run_id()
    if execution is None:
        epoch = await explore_repo.claim_run(db, tenant_id, workspace_id, datasource_id)
        if epoch is None:
            await db.rollback()
            return ExploreResult(status="already_running", run_id=run_id)
        await db.commit()
        execution = _ExploreRun(tenant_id, workspace_id, datasource_id, int(epoch))
    session_factory = async_sessionmaker(db.bind, expire_on_commit=False)
    async with execution.running(session_factory):
        try:
            result = await _run_owned_explore(
                db,
                datasource_id=datasource_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                graph_access=graph_access,
                run_sync=run_sync,
                force_refresh=force_refresh,
                run_id=run_id,
                connector=connector,
                ds_config=ds_config,
                execution=execution,
            )
        except ExploreLost:
            await db.rollback()
            raise
        except (Exception, asyncio.CancelledError) as exc:
            await db.rollback()
            result = ExploreResult(
                status="failed",
                run_id=run_id,
                error=str(exc)[:1000] or "执行已中断，请手动重试",
            )
        # The ownership lock and result commit are in one short transaction.
        await execution.guard(db)
        await explore_repo.ensure_scope(db, tenant_id, workspace_id, datasource_id)
        if not await explore_repo.finish_run(
            db,
            tenant_id,
            workspace_id,
            datasource_id,
            status=result.status,
            counts_json=json.dumps(result.counts, ensure_ascii=False),
            error=result.error,
            epoch=execution.epoch,
        ):
            raise ExploreLost(datasource_id)
        await db.commit()
        return result


async def _run_owned_explore(
    db: AsyncSession,
    *,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    user_id: str,
    graph_access: Any,
    run_sync: Callable[..., Awaitable],
    force_refresh: bool = False,
    run_id: str | None = None,
    connector: Any = None,
    ds_config: dict | None = None,
    execution: _ExploreRun,
) -> ExploreResult:
    """执行一轮探查（10 步）。``connector`` / ``ds_config`` 可注入（测试/复用）。"""
    # ① 取配置（缺行自动生成默认 scope，§1.2）
    await execution.guard(db)
    scope = await explore_repo.ensure_scope(db, tenant_id, workspace_id, datasource_id)
    run_id = run_id or generate_run_id()
    counts = {
        "tables_new": 0,
        "tables_refreshed": 0,
        "columns": 0,
        "sampled_cols": 0,
        "skipped_manual": 0,
        "truncated": False,
    }
    if not scope["enabled"]:
        return ExploreResult(status="disabled", run_id=run_id, counts=counts)
    if not await explore_repo.mark_running(
        db, tenant_id, workspace_id, datasource_id, execution.epoch
    ):
        raise ExploreLost(datasource_id)
    # running 状态立即落盘；连接、元数据拉取和采样都可能耗时，期间不得持有
    # SQLite 写事务，否则会阻塞 weave 任务及其他控制面写入。
    await db.commit()

    # ② 解析连接配置（凭证失效/无权限 → failed 留痕）
    if connector is None or ds_config is None:
        try:
            connector, ds_config = await run_sync(
                _resolve_connector,
                graph_access,
                datasource_id,
                force_refresh=force_refresh,
            )
        except Exception as exc:  # noqa: BLE001 — ConnectorError 及桥接异常统一留痕
            return await _fail(
                db, tenant_id, workspace_id, datasource_id, run_id, counts, exc
            )
    ds_type = _normalize_type((ds_config or {}).get("_datasource_type", ""))
    project = str((ds_config or {}).get("project") or "")

    # ③ 元数据拉取（TTL 缓存；手动触发 force_refresh 防读旧 manifest）
    schemas = split_patterns(scope["schemas"])
    try:
        manifest = await run_sync(
            _extract,
            connector,
            schemas,
            datasource_id=datasource_id,
            force_refresh=force_refresh,
        )
    except Exception as exc:  # noqa: BLE001
        return await _fail(
            db, tenant_id, workspace_id, datasource_id, run_id, counts, exc
        )

    deadline = time.monotonic() + RUN_BUDGET_SECS

    # ④ scope 过滤（内存面，零额外 SQL）
    kept = filter_manifest_tables(
        manifest.tables,
        schemas=schemas,
        include=split_patterns(scope["include_patterns"]),
        exclude=split_patterns(scope["exclude_patterns"]),
    )
    cols_by_table: dict[str, list] = {}
    for c in manifest.columns:
        cols_by_table.setdefault(c.table, []).append(c)

    # ⑤ 两段全名拼装 + table_limit 截断（字典序稳定）
    entries: dict[str, list] = {}
    for t in kept:
        full = full_table_name(
            t,
            ds_type=ds_type,
            project=project,
            db_id=(manifest.db_id or datasource_id),
        )
        entries[full] = cols_by_table.get(t.name, [])
    kept_names, truncated = apply_table_limit(
        list(entries), limit=int(scope["table_limit"] or 300)
    )
    counts["truncated"] = truncated

    # ⑥ 人工优先（编辑器层存在性 = 人工标记，零 schema 变更）
    await execution.guard(db)
    manual_keys = await _manual_column_keys(db, tenant_id, workspace_id, datasource_id)

    # ⑦ 指纹比对 → 未变表跳过（不写图、不采样）
    state_map = await explore_repo.get_state_map(
        db, tenant_id, workspace_id, datasource_id
    )

    to_write: list[tuple[str, list, str, bool]] = []  # (全名, 自动列, 指纹, 是否新表)
    for full in kept_names:
        cols = entries[full]
        fp = table_fingerprint(full, cols)
        auto_cols, manual_n = split_manual_columns(full, cols, manual_keys)
        if cols and not auto_cols:
            # 全列人工在场 → 整表跳过（§3.5 skipped_manual）
            counts["skipped_manual"] += 1
            await explore_repo.upsert_state(
                db,
                tenant_id,
                workspace_id,
                datasource_id,
                full,
                fingerprint=fp,
                run_id=run_id,
                status="skipped_manual",
                column_count=0,
                sampled=0,
                updated_by=user_id,
            )
            continue
        if state_map.get(full) == fp:
            continue
        to_write.append((full, auto_cols, fp, full not in state_map))

    # 释放人工优先/指纹阶段可能开启的读写事务，再进入最长可达 300s 的采样。
    await db.commit()

    # ⑧ 抽样（仅待写表；预算 300s 含在整轮预算内；静默降级）
    samples_by_table: dict[str, dict[str, list[str]]] = {}
    if sampling_enabled(scope["sample_mode"], ds_type) and connector_can_sample(
        connector
    ):
        sample_budget = min(SAMPLE_BUDGET_SECS, max(deadline - time.monotonic(), 0))
        sample_start = time.monotonic()
        sample_limit = int(scope["sample_limit"] or 10)
        for full, auto_cols, _fp, _new in to_write:
            remaining = sample_budget - (time.monotonic() - sample_start)
            if remaining <= 0:
                log.info("autoexplore sample budget exhausted ds=%s", datasource_id)
                break
            samples_by_table[full] = await run_sync(
                sample_columns,
                connector,
                full,
                auto_cols,
                sample_limit=sample_limit,
                ds_type=ds_type,
                budget_secs=remaining,
            )

    # ⑨ 组装 → upsert_rows 双路径直写（50 表/批；失败批记录后继续；写预算 600s）
    status_final = "ok"
    for i in range(0, len(to_write), TABLE_BATCH_SIZE):
        if time.monotonic() > deadline:
            log.warning(
                "autoexplore run budget exhausted ds=%s (partial)", datasource_id
            )
            status_final = "partial"
            break
        chunk = to_write[i : i + TABLE_BATCH_SIZE]
        table_rows: list[dict] = []
        col_rows: list[dict] = []
        metas: list[tuple[str, str, int, int, bool]] = []
        for full, auto_cols, fp, is_new in chunk:
            samples = samples_by_table.get(full, {})
            t_row, c_rows = assemble_table_rows(datasource_id, full, auto_cols, samples)
            table_rows.append(t_row)
            col_rows.extend(c_rows)
            sampled_n = sum(1 for v in samples.values() if v)
            metas.append((full, fp, len(c_rows), sampled_n, is_new))
        await execution.guard(db)
        await db.commit()
        try:
            await run_sync(
                _write_graph,
                graph_access,
                "table",
                table_rows,
                datasource_id=datasource_id,
            )
            await execution.guard(db)
            await db.commit()
            await run_sync(
                _write_graph,
                graph_access,
                "table_column",
                col_rows,
                datasource_id=datasource_id,
            )
        except ExploreLost:
            raise
        except Exception as exc:  # noqa: BLE001 — 失败批记录后继续下批
            log.warning(
                "autoexplore write batch failed ds=%s batch=%d: %s",
                datasource_id,
                i // TABLE_BATCH_SIZE,
                exc,
            )
            await execution.guard(db)
            status_final = "partial"
            for full, fp, _n, _s, _new in metas:
                await explore_repo.upsert_state(
                    db,
                    tenant_id,
                    workspace_id,
                    datasource_id,
                    full,
                    fingerprint=fp,
                    run_id=run_id,
                    status="failed",
                    column_count=0,
                    sampled=0,
                    updated_by=user_id,
                )
            await db.commit()
            continue
        # ⑩（批内）：计数 + per-表状态回写
        await execution.guard(db)
        for full, fp, n_cols, sampled_n, is_new in metas:
            counts["columns"] += n_cols
            counts["sampled_cols"] += sampled_n
            counts["tables_new" if is_new else "tables_refreshed"] += 1
            await explore_repo.upsert_state(
                db,
                tenant_id,
                workspace_id,
                datasource_id,
                full,
                fingerprint=fp,
                run_id=run_id,
                status="ok",
                column_count=n_cols,
                sampled=1 if sampled_n else 0,
                updated_by=user_id,
            )
        # 每批状态独立提交；下一批图写入期间不占用 SQLite 写锁。
        await db.commit()

    # ⑩（汇总）：scope 运行汇总留痕（不新建 weave_task 行，§3.9）
    log.info(
        "autoexplore finished ds=%s run=%s status=%s counts=%s",
        datasource_id,
        run_id,
        status_final,
        counts,
    )
    return ExploreResult(status=status_final, run_id=run_id, counts=counts)

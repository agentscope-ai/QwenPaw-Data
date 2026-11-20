"""AlgoProvider：把 ``profiling.algo`` 的四任务流水线适配为编排层契约。

本模块只做两件事：**task ↔ stage 的映射**，和**帧 → Candidate\\* 的投影**。

编排层只交 ``ctx.facts``（原始行），context 在这里装 —— 算法库的内部类型
（``StaticProbeFactsInput`` 等）因此不外泄。装配配置时复用宿主的鉴权身份、
落盘路径和用户模型配置；算法库拿到完整配置，不处理 tenant/workspace。

------------------------------------------------------------------------
七个 probe_* 的执行入口
------------------------------------------------------------------------
::

    probe_biz_domains        → DOMAIN    domains ⋈ members
    probe_datasets           → DATASET   table_meta（含嵌套 fields）
    probe_datasets_fields    → 已确认 Dataset 的 source → 输出列（不重跑 DATASET）
    probe_dimension_calibers ┐→ CALIBER  calibers 中 column_role == dimension
    probe_metric_calibers    ┘                            == metric
    probe_dimensions         ┐→ DICT     dimensions
    probe_metrics            ┘           metrics

CALIBER 一次同时定维度与指标角色，DICT 一次产出两张表——旧实现里
``_CALIBER_TASKS`` 那种「点一边跑两个任务」的强绑定因此消失。**仍然只
emit 被点击的那一侧**，与旧行为一致：另一侧没有经过用户这一次的审核意图，
不该悄悄落进 staging。

------------------------------------------------------------------------
投影的输入从哪来
------------------------------------------------------------------------
``run()`` 返回 ``executed=True`` 之后，直接读 output context 的
``current()`` —— ``SnapshotContext.save()`` **先设 ``_current`` 再落盘**，
所以那一定是本次 handler 新产出的快照。

这里曾用 ``EmitStore`` + 一个 ``_Capture`` 缓冲来取代它，理由是「读
current() 分不清新算的 / 缓存的 / 权威的」。``outcome.executed`` 已经把
这件事回答了，那层间接因此消失。emit 现在只服务它本来的用途：把 staged
产物流式吐给需要的接收端。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from profiling.cm_adapter import RuntimeLlmClient, resolve_profile_out_dir
from profiling.algo.stores import complete_file_writes
from profiling.algo import AlgoConfig, AlgoContexts, AlgoProfiler, ProfileTask, RunHooks
from profiling.algo.handlers.dataset_fields import dataset_source_columns
from profiling.algo.snapshots import (
    CALIBERS_KEY,
    COLUMN_META_PREFIX,
    DIMENSIONS_KEY,
    DOMAINS_KEY,
    MEMBERS_KEY,
    METRICS_KEY,
    TABLE_META_KEY,
    ColumnRole,
    TableSnapshot,
)
from profiling.models.probe import ROW_CONFIRMED
from profiling.provider import (
    CandidateBizDomain,
    CandidateDataset,
    CandidateDatasetField,
    CandidateDimension,
    CandidateDimensionCaliber,
    CandidateMemberTable,
    CandidateMetric,
    CandidateMetricCaliber,
    ProbeContext,
    ProbeSink,
)

logger = logging.getLogger(__name__)

Frames = Mapping[str, pd.DataFrame]

# ===========================================================================
# 取值：算法侧列契约是「必须有」，值仍可能是 NA / 空串 / list
# ===========================================================================


def _records(frame: Any) -> list[dict[str, Any]]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    return frame.to_dict(orient="records")


def _text(value: Any) -> str | None:
    """标量 → 去空白的非空字符串；NA / 空串 / "nan" 归一成 None。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set, dict)):  # pd.isna 对容器返回数组
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _cell(row: Mapping[str, Any], *keys: str) -> str | None:
    """取第一个非空单元格。多个 key 用于兼容可选列。"""
    for key in keys:
        if key in row:
            value = _text(row[key])
            if value is not None:
                return value
    return None


def _multi(value: Any) -> str | None:
    """list 列 → ``$$$`` 串（v2.1 存储面多值统一约定，weave 侧按此拆）。"""
    if isinstance(value, (list, tuple, set)):
        parts = [t for t in (_text(v) for v in value) if t]
        return "$$$".join(parts) or None
    return _text(value)


def _yn(value: Any, default: str) -> str:
    """bool / 0/1 / 'Y'/'N' 杂型 → 正式表的 ``'Y'/'N'`` 约定。NA 走默认。"""
    text = _text(value)
    if text is None:
        return default
    if text.lower() in ("y", "yes", "true", "1"):
        return "Y"
    if text.lower() in ("n", "no", "false", "0"):
        return "N"
    return default


# ===========================================================================
# 投影：snapshot frames → Candidate*
# ===========================================================================


def map_biz_domains(frames: Frames) -> list[CandidateBizDomain]:
    """``domains`` ⋈ ``members``。members 每张表一行，按 domain_name 归拢。"""
    members: dict[str, list[CandidateMemberTable]] = {}
    for row in _records(frames.get(MEMBERS_KEY)):
        domain_name = _cell(row, "domain_name")
        source_table = _cell(row, "table_name")
        if not domain_name or not source_table:
            continue
        members.setdefault(domain_name, []).append(
            CandidateMemberTable(source_table=source_table)
        )

    items: list[CandidateBizDomain] = []
    for row in _records(frames.get(DOMAINS_KEY)):
        name = _cell(row, "domain_name")
        if not name:
            continue
        items.append(
            CandidateBizDomain(
                domain_name=name,
                description=_cell(row, "description"),
                member_tables=members.get(name, []),
                # handler 在 domains 帧上带的额外列；老快照无此列按 create
                origin=_cell(row, "origin") or "create",
            )
        )
    return items


def map_datasets(frames: Frames) -> list[CandidateDataset]:
    """``table_meta`` + 每个 dataset 自己的 ``column_meta__{名}`` 帧。

    列嵌套在 Dataset 候选上。DATASET 阶段只写父候选；DATASET_FIELDS
    阶段按用户已确认的 source 重新装配列，再用相同投影交给 sink。
    """
    items: list[CandidateDataset] = []
    for row in _records(frames.get(TABLE_META_KEY)):
        name = _cell(row, "table_name")
        if not name:
            continue
        fields = [
            CandidateDatasetField(
                field_name=field_name,
                data_type=_cell(col, "data_type"),
                is_primary=_yn(col.get("is_primary"), "N"),
                is_nullable=_yn(col.get("is_nullable"), "Y"),
                # L3 description：物理注释是业务描述的最好初值（候选态，人审改写）
                description=_cell(col, "column_comment"),
            )
            for col in _records(frames.get(f"{COLUMN_META_PREFIX}{name}"))
            if (field_name := _cell(col, "column_name"))
        ]
        # v2.1 单指针 source（②b）：写出侧（handlers/dataset.py）已按来源
        # 定好正身 —— 直通=物理名、日志派生=SQL 文本；老快照缺 source 列时
        # 回退 parents（直通旧行的物理名）再回退 dataset 名自身。
        # 不回退 sql_content：SQLite 直通视图的 view_definition 是整段
        # CREATE VIEW DDL，进 source 会污染读侧血缘与 weave 表名判定。
        source = _cell(row, "source", "parents") or name
        items.append(
            CandidateDataset(
                domain_name=_cell(row, "domain_name"),
                dataset_name=name,
                description=_cell(row, "table_comment"),
                source=source,
                fields=fields,
            )
        )
    return items


def _caliber_rows(frames: Frames, *roles: ColumnRole) -> Iterable[dict[str, Any]]:
    accepted = {role.value for role in roles}
    for row in _records(frames.get(CALIBERS_KEY)):
        if (_cell(row, "column_role") or "").lower() in accepted:
            yield row


def map_dimension_calibers(frames: Frames) -> list[CandidateDimensionCaliber]:
    """v2.1：date 角色也产绑定（时间维度的口径同样落 L5）。"""
    items: list[CandidateDimensionCaliber] = []
    for row in _caliber_rows(frames, ColumnRole.DIMENSION, ColumnRole.DATE):
        name = _cell(row, "dimension_name", "column_name")
        if not name:
            continue
        # expr 落正式表 NOT NULL（DTO 必填）：口径表达式缺失时列名即表达式；
        # 两者皆空的行无法落库，跳过并留痕，不让单行毁掉整批 emit
        expr = _cell(row, "expr", "column_name")
        if expr is None:
            logger.warning("map_dimension_calibers: %s 缺 expr，跳过", name)
            continue
        items.append(
            CandidateDimensionCaliber(
                domain_name=_cell(row, "domain_name"),
                dataset_name=_cell(row, "table_name"),
                dimension_name=name,
                expr=expr,
                value_example=_multi(row.get("value_example")),
                column_role=_cell(row, "column_role"),
                column_name=_cell(row, "column_name"),
            )
        )
    return items


def map_metric_calibers(frames: Frames) -> list[CandidateMetricCaliber]:
    items: list[CandidateMetricCaliber] = []
    for row in _caliber_rows(frames, ColumnRole.METRIC):
        name = _cell(row, "metric_name", "column_name")
        if not name:
            continue
        # expr/time_window 落正式表 NOT NULL（DTO 必填）：缺表达式时列名即
        # 表达式，两者皆空跳过；缺窗口兜底 1d（handler 已保证 metric 行有值）
        expr = _cell(row, "expr", "column_name")
        if expr is None:
            logger.warning("map_metric_calibers: %s 缺 expr，跳过", name)
            continue
        items.append(
            CandidateMetricCaliber(
                domain_name=_cell(row, "domain_name"),
                dataset_name=_cell(row, "table_name"),
                metric_name=name,
                expr=expr,
                time_window=_cell(row, "time_window") or "1d",
                additivity=_cell(row, "additivity"),
                column_name=_cell(row, "column_name"),
            )
        )
    return items


def map_dimensions(frames: Frames) -> list[CandidateDimension]:
    items: list[CandidateDimension] = []
    for row in _records(frames.get(DIMENSIONS_KEY)):
        name = _cell(row, "dimension_name")
        if not name:
            continue
        items.append(
            CandidateDimension(
                domain_name=_cell(row, "domain_name"),
                dimension_name=name,
                description=_cell(row, "description"),
                synonyms=_multi(row.get("synonyms")) or "",
                dimension_type=_cell(row, "dimension_role"),
            )
        )
    return items


def map_metrics(frames: Frames) -> list[CandidateMetric]:
    """``metrics``。``formulas`` / ``time_windows`` 不在此落地——它们属于
    ``CandidateMetricCaliber``，由 CALIBER 阶段吐出。"""
    items: list[CandidateMetric] = []
    for row in _records(frames.get(METRICS_KEY)):
        name = _cell(row, "metric_name")
        if not name:
            continue
        items.append(
            CandidateMetric(
                domain_name=_cell(row, "domain_name"),
                metric_name=name,
                description=_cell(row, "description"),
                synonyms=_multi(row.get("synonyms")),
            )
        )
    return items


# ===========================================================================
# 运行期接线
# ===========================================================================


def _progress_bridge(
    report_progress: Callable[[int, str], Any],
    loop: asyncio.AbstractEventLoop,
    pending: set[asyncio.Future],
) -> Callable[[int, str], None]:
    """``RunHooks.report_progress``（同步）→ 编排层的协程回调。

    algo 的 CPU 段跑在 ``to_thread`` 里，进度回调因此可能来自工作线程，也可能
    来自事件循环本身。``call_soon_threadsafe`` 两种情况都合法，且**不阻塞**——
    旧适配层那种 ``run_coroutine_threadsafe(...).result()`` 在同循环调用会自锁。
    """

    def report(pct: int, message: str) -> None:
        def spawn() -> None:
            fut = asyncio.ensure_future(report_progress(int(pct), str(message)))
            pending.add(fut)

            def _done(done: asyncio.Future) -> None:
                pending.discard(done)
                if not done.cancelled():
                    done.exception()  # 取走异常，避免 "Task exception was never retrieved"

            fut.add_done_callback(_done)

        try:
            loop.call_soon_threadsafe(spawn)
        except RuntimeError:  # 循环已关闭：进度是尽力而为，丢掉即可
            pass

    return report


async def _tick(ctx: ProbeContext, pct: int, message: str) -> None:
    ctx.raise_if_cancelled()
    await ctx.report_progress(pct, message)
    await asyncio.sleep(0)


def _algo_config(ctx: ProbeContext, *, use_llm: bool = False) -> AlgoConfig:
    """共用身份和文件目录；模型调用由 RuntimeLlmClient 装配。"""
    tenant_id = getattr(ctx.principal, "tenant_id", None)
    workspace_id = getattr(ctx.principal, "active_workspace_id", None)
    if not all(
        str(value or "").strip()
        for value in (tenant_id, workspace_id, ctx.datasource_id)
    ):
        raise ValueError(
            "profiling 落盘需要 tenant_id、active_workspace_id 和 datasource_id"
        )
    out_dir = resolve_profile_out_dir(tenant_id, workspace_id, str(ctx.datasource_id))
    return AlgoConfig.build(
        datasource_id=str(ctx.datasource_id),
        datasource_connection=dict(ctx.connection or {}),
        datasource_type=ctx.datasource_type,
        datasource_principal=ctx.principal,
        store_datasource_dir=out_dir / "runs" / ctx.artifact_id,
        # 编排层的 rerun 在算法侧按 create 执行。
        handler_mode="refine" if ctx.mode == "refine" else "create",
    )


class AlgoProvider:
    """编排层可插拔 Provider：``profiling.algo_provider:AlgoProvider``。"""

    @complete_file_writes()
    async def _probe(
        self,
        ctx: ProbeContext,
        sink: ProbeSink,
        task: ProfileTask,
        project: Callable[[Frames], Sequence[Any]],
        emit_name: str,
        *,
        start_message: str,
        done_message: str,
    ) -> None:
        # 同一份 hooks 交给 contexts 与 profiler：parse 的 on_progress
        # （分钟级 SQL）和 handler 的 LLM 单元都要落到任务表，不能只给后者。
        loop = asyncio.get_running_loop()
        pending: set[asyncio.Future] = set()
        hooks = RunHooks(
            raise_if_cancelled=ctx.raise_if_cancelled,
            report_progress=_progress_bridge(ctx.report_progress, loop, pending),
        )
        # 装配在这里：编排层只交原始事实（ctx.facts），算法库自己把它包成
        # ProbeFactsInput —— 那个类是算法库内部的，编排层不认识。
        contexts = AlgoContexts.build(
            _algo_config(ctx, use_llm=True),
            facts=ctx.facts,
            hooks=hooks,
        )

        await _tick(ctx, 5, start_message)

        async with RuntimeLlmClient.open(contexts.config) as llm:
            profiler = AlgoProfiler.build(contexts, hooks=hooks, llm=llm)
            try:
                outcome = await profiler.run(task)
            finally:
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        # 门禁未过（上游任务还没跑、证据不足）——不是错误，据实回报即可。
        if not outcome.executed:
            logger.info("AlgoProvider %s 未执行：%s", task.value, outcome.reason)
            await _tick(ctx, 100, f"{task.value} 未执行：{outcome.reason}")
            return

        await _tick(ctx, 85, f"正在映射 {task.value} 候选…")
        # ``executed=True`` 时 output context 的 current() **必然是本次 handler
        # 新产出的** —— ``save()`` 先设 _current 再落盘。所以不需要 EmitStore
        # 捕获：那条路曾用来区分「新算的 / 缓存的 / 权威的」，而 outcome 已经
        # 把这件事回答了。
        snapshot = contexts.output_for(task).current()
        items = list(project(snapshot.to_frames()))
        logger.info(
            "AlgoProvider %s -> %s: %d candidates", task.value, emit_name, len(items)
        )
        await getattr(sink, emit_name)(items)
        await _tick(ctx, 100, done_message)

    async def probe_biz_domains(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await self._probe(
            ctx,
            sink,
            ProfileTask.DOMAIN,
            map_biz_domains,
            "emit_biz_domains",
            start_message="正在运行算法流水线并聚类业务域…",
            done_message="业务域探查完成",
        )

    async def probe_datasets(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await self._probe(
            ctx,
            sink,
            ProfileTask.DATASET,
            map_datasets,
            "emit_datasets",
            start_message="正在运行算法流水线并解析数据集…",
            done_message="数据集探查完成",
        )

    @complete_file_writes()
    async def _load_dataset_field_tables(
        self, ctx: ProbeContext
    ) -> tuple[TableSnapshot, str]:
        """沿用当前源的连接、权限和元数据缓存，不加载日志或运行 Dataset 发现。"""
        pending: set[asyncio.Future] = set()
        hooks = RunHooks(
            raise_if_cancelled=ctx.raise_if_cancelled,
            report_progress=_progress_bridge(
                ctx.report_progress,
                asyncio.get_running_loop(),
                pending,
            ),
        )
        contexts = AlgoContexts.build(
            _algo_config(ctx),
            facts=ctx.facts,
            hooks=hooks,
        )
        try:
            return await contexts.source_table.load(), contexts.config.parser_dialect
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def probe_datasets_fields(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        """逐条消费当前源已确认 Dataset 的当前定义；名称只用于关联输出。"""
        await _tick(ctx, 5, "正在读取已确认 Dataset 的 source…")
        confirmed = [
            row
            for row in _records((ctx.facts or {}).get("staged_dataset"))
            if _cell(row, "probe_status") == ROW_CONFIRMED
            and _yn(row.get("is_deleted"), "N") == "N"
            and _cell(row, "datasource_id") in {None, str(ctx.datasource_id)}
        ]
        if not confirmed:
            await _tick(ctx, 100, "没有已确认 Dataset，未生成 dataset_field 候选")
            return
        tables, dialect = await self._load_dataset_field_tables(ctx)
        items: list[CandidateDataset] = []
        for index, row in enumerate(confirmed):
            name = _cell(row, "dataset_name")
            source = _cell(row, "source")
            identity = f"Dataset {name!r} (staging id={row.get('id')})"
            if not name or not source:
                raise ValueError(f"{identity}: dataset_name 和 source 不能为空")
            await _tick(
                ctx,
                10 + 70 * index // len(confirmed),
                f"正在按 source 探查 {name} 的 dataset_field…",
            )
            try:
                columns = await asyncio.to_thread(
                    dataset_source_columns,
                    source,
                    tables,
                    dialect=dialect,
                )
            except ValueError as exc:
                raise ValueError(f"{identity}: {exc}") from exc
            columns["table_name"] = name
            snapshot = TableSnapshot.build(
                pd.DataFrame(
                    [
                        {
                            "table_name": name,
                            "table_comment": _cell(row, "description"),
                            "domain_name": _cell(row, "domain_name"),
                            "source": source,
                        }
                    ]
                ),
                {name: columns},
            )
            items.extend(map_datasets(snapshot.to_frames()))
        # 全部 source 解析成功后才交给 sink，避免坏 source 留下半批新候选。
        await _tick(ctx, 85, "正在保存 dataset_field 候选…")
        await sink.emit_dataset_fields(items)
        await _tick(ctx, 100, "dataset_field 探查完成")

    async def probe_dimension_calibers(
        self, ctx: ProbeContext, sink: ProbeSink
    ) -> None:
        await self._probe(
            ctx,
            sink,
            ProfileTask.CALIBER,
            map_dimension_calibers,
            "emit_dimension_calibers",
            start_message="正在运行算法流水线并推断维度口径…",
            done_message="维度口径探查完成",
        )

    async def probe_metric_calibers(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await self._probe(
            ctx,
            sink,
            ProfileTask.CALIBER,
            map_metric_calibers,
            "emit_metric_calibers",
            start_message="正在运行算法流水线并推断指标口径…",
            done_message="指标口径探查完成",
        )

    async def probe_dimensions(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await self._probe(
            ctx,
            sink,
            ProfileTask.DICT,
            map_dimensions,
            "emit_dimension",
            start_message="正在运行算法流水线并聚合维度实体…",
            done_message="维度实体探查完成",
        )

    async def probe_metrics(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await self._probe(
            ctx,
            sink,
            ProfileTask.DICT,
            map_metrics,
            "emit_metric",
            start_message="正在运行算法流水线并聚合指标实体…",
            done_message="指标实体探查完成",
        )


__all__ = [
    "AlgoProvider",
    "map_biz_domains",
    "map_datasets",
    "map_dimension_calibers",
    "map_dimensions",
    "map_metric_calibers",
    "map_metrics",
]

"""默认 StubProvider：产出少量假数据，保证前后端全链路联调不依赖算法侧就绪。

每个阶段 emit 1~2 条候选并 await 一次让出（便于停止探查命中取消）。上游引用用名称，
优先复用 ctx 中已有的正式实体名，缺省时回退到内置样例名，保证 promote 能按名解析。
"""

from __future__ import annotations

import asyncio

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


async def _tick(ctx: ProbeContext, pct: int, message: str) -> None:
    ctx.raise_if_cancelled()
    await ctx.report_progress(pct, message)
    await asyncio.sleep(0)  # 让出事件循环，便于协作式取消


def _first_domain_name(ctx: ProbeContext, fallback: str) -> str:
    frame = ctx.facts.get("domain")
    return (
        str(frame.iloc[0]["domain_name"])
        if frame is not None and len(frame)
        else fallback
    )


def _first_dataset_name(ctx: ProbeContext, fallback: str) -> str:
    frame = ctx.facts.get("dataset")
    return (
        str(frame.iloc[0]["dataset_name"])
        if frame is not None and len(frame)
        else fallback
    )


class StubProvider:
    async def probe_biz_domains(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await _tick(ctx, 30, "正在聚类物理表…")
        await sink.emit_biz_domains(
            [
                CandidateBizDomain(
                    domain_name="STUB_DOMAIN",
                    description="StubProvider 产出的示例业务域",
                    member_tables=[CandidateMemberTable(source_table="stub_table_a")],
                )
            ]
        )
        await _tick(ctx, 100, "业务域探查完成")

    def _datasets(self, ctx: ProbeContext) -> list[CandidateDataset]:
        """DATASET / DATASET_FIELDS 两阶段共用的候选 —— 同构于 AlgoProvider：
        两个阶段跑同一个算法任务，各取所需（本体 / 字段）。"""
        return [
            CandidateDataset(
                domain_name=_first_domain_name(ctx, "STUB_DOMAIN"),
                dataset_name="STUB_DATASET",
                description="示例数据集",
                source="SELECT 1",
                fields=[
                    CandidateDatasetField(
                        field_name="country_code",
                        data_type="STRING",
                        description="国家编码",
                    )
                ],
            )
        ]

    async def probe_datasets(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await _tick(ctx, 50, "正在解析数据集…")
        await sink.emit_datasets(self._datasets(ctx))
        await _tick(ctx, 100, "数据集探查完成")

    async def probe_datasets_fields(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await _tick(ctx, 50, "正在继承数据集列元数据…")
        await sink.emit_dataset_fields(self._datasets(ctx))
        await _tick(ctx, 100, "数据集字段探查完成")

    async def probe_dimension_calibers(
        self, ctx: ProbeContext, sink: ProbeSink
    ) -> None:
        await _tick(ctx, 50, "正在推断维度口径…")
        await sink.emit_dimension_calibers(
            [
                CandidateDimensionCaliber(
                    domain_name=_first_domain_name(ctx, "STUB_DOMAIN"),
                    dataset_name=_first_dataset_name(ctx, "STUB_DATASET"),
                    dimension_name="地区",
                    expr="country_code",
                    value_example="CN$$$US$$$JP",
                )
            ]
        )
        await _tick(ctx, 100, "维度口径探查完成")

    async def probe_dimensions(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await _tick(ctx, 50, "正在聚合维度实体…")
        await sink.emit_dimension(
            [
                CandidateDimension(
                    domain_name=_first_domain_name(ctx, "STUB_DOMAIN"),
                    dimension_name="地区",
                    description="地理维度",
                    synonyms="区域$$$地域",
                    dimension_type="categorical",
                )
            ]
        )
        await _tick(ctx, 100, "维度实体探查完成")

    async def probe_metric_calibers(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await _tick(ctx, 50, "正在推断指标口径…")
        await sink.emit_metric_calibers(
            [
                CandidateMetricCaliber(
                    domain_name=_first_domain_name(ctx, "STUB_DOMAIN"),
                    dataset_name=_first_dataset_name(ctx, "STUB_DATASET"),
                    metric_name="日活用户数",
                    expr="COUNT(DISTINCT user_id)",
                    time_window="1d",
                    additivity="non_additive",
                )
            ]
        )
        await _tick(ctx, 100, "指标口径探查完成")

    async def probe_metrics(self, ctx: ProbeContext, sink: ProbeSink) -> None:
        await _tick(ctx, 50, "正在聚合指标实体…")
        await sink.emit_metric(
            [
                CandidateMetric(
                    domain_name=_first_domain_name(ctx, "STUB_DOMAIN"),
                    metric_name="日活用户数",
                    description="每日活跃用户",
                    synonyms="DAU$$$活跃数",
                )
            ]
        )
        await _tick(ctx, 100, "指标实体探查完成")

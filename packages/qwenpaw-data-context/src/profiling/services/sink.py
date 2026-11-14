"""DbProbeSink：算法 emit 的候选结果 → 立即落 staging 并提交（SPEC §5.4 增量吐出）。

上游引用按名称解析为正式 id（domain_name→domain_id、dataset_name→dataset_id），
同时把 name 冗余写入 staging，读路径 SELECT * 即可返回。source_type 恒 ALGO。

业务字段不逐个手抄：Candidate* 字段名与 staging 列名**逐字一致**（由
``tests/profiling/test_probe_candidate_contract.py`` 强制），``_values`` 直接
``model_dump`` 派生，本文件只写「名称引用解析 + 嵌套序列化」两类特判。
新增字段只改 Candidate 与 staging 表两处，sink 自动跟随。
"""

from __future__ import annotations

from typing import Sequence

import aiosqlite

from profiling.provider import (
    CandidateBizDomain,
    CandidateDataset,
    CandidateDimension,
    CandidateDimensionCaliber,
    CandidateMetric,
    CandidateMetricCaliber,
    ProbeContext,
)
from profiling.repositories import staging_repo
import json


def _name_to_id(frame, name_column: str) -> dict:
    """正式表的 name → id。上游引用按名称传递，id 只在落库时用。"""
    if frame is None or len(frame) == 0:
        return {}
    return {r[name_column]: r["id"] for _, r in frame.iterrows() if r[name_column]}


class DbProbeSink:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        ctx: ProbeContext,
        *,
        datasource_id: str,
        tenant_id: str,
        workspace_id: str,
        created_by: str,
        task_id: str,
        execution=None,
    ) -> None:
        self._execution = execution
        self._conn = conn
        self._ds = datasource_id
        self._tid = tenant_id
        self._wid = workspace_id
        self._by = created_by
        self._task = task_id
        self._domain_id = _name_to_id(ctx.facts.get("domain"), "domain_name")
        self._dataset_id = _name_to_id(ctx.facts.get("dataset"), "dataset_name")

    async def _guard(self) -> None:
        if self._execution is not None:
            await self._execution.guard(self._conn)

    async def _insert(self, table: str, values: dict) -> None:
        await staging_repo.insert_row(
            self._conn,
            table,
            values,
            datasource_id=self._ds,
            tenant_id=self._tid,
            workspace_id=self._wid,
            created_by=self._by,
            task_id=self._task,
        )

    def _values(
        self,
        it,
        *,
        domain_ref: bool = True,
        dataset_ref: bool = False,
        exclude: set[str] | None = None,
    ) -> dict:
        """业务字段 = ``model_dump`` 原样；名称引用在此解析出 id 列。

        ``domain_ref=False`` 仅用于 biz_domain 自身 —— 它的 ``domain_name``
        是实体主键不是上游引用，staging 表也没有 ``domain_id`` 列。
        """
        values = it.model_dump(exclude=exclude or set())
        if domain_ref and "domain_name" in values:
            values["domain_id"] = self._domain_id.get(values["domain_name"])
        if dataset_ref:
            values["dataset_id"] = self._dataset_id.get(values.get("dataset_name"))
        return values

    async def emit_biz_domains(self, items: Sequence[CandidateBizDomain]) -> None:
        await self._guard()
        for it in items:
            values = self._values(it, domain_ref=False, exclude={"member_tables"})
            # 嵌套 → JSON 列：存储编码是 sink 的私事，DTO 不感知
            values["member_tables"] = (
                json.dumps(
                    [m.model_dump() for m in it.member_tables], ensure_ascii=False
                )
                if it.member_tables
                else None
            )
            await self._insert("staging_biz_domain", values)
        await self._conn.commit()

    async def emit_datasets(self, items: Sequence[CandidateDataset]) -> None:
        """只写数据集本体。字段归 ``emit_dataset_fields`` —— L3 字段是独立评审
        阶段（DATASET_FIELDS），两张表各由各自阶段落库。"""
        await self._guard()
        for it in items:
            await self._insert("staging_dataset", self._values(it, exclude={"fields"}))
        await self._conn.commit()

    async def emit_dataset_fields(self, items: Sequence[CandidateDataset]) -> None:
        """DATASET_FIELDS 阶段：只写字段子表。

        入参仍是 ``CandidateDataset`` —— 字段与数据集原子成对（算法侧铁律：
        列必须在 DATASET 环节继承好），拆的只是落库时机，不拆载体。
        """
        await self._guard()
        for it in items:
            for field in it.fields:
                # 嵌套 → 展平进子表：父级键（dataset_id/dataset_name）由此注入
                values = field.model_dump()
                values["dataset_id"] = self._dataset_id.get(it.dataset_name)
                values["dataset_name"] = it.dataset_name
                await self._insert("staging_dataset_field", values)
        await self._conn.commit()

    async def emit_dimension_calibers(
        self, items: Sequence[CandidateDimensionCaliber]
    ) -> None:
        await self._guard()
        for it in items:
            await self._insert(
                "staging_dataset_dimension", self._values(it, dataset_ref=True)
            )
        await self._conn.commit()

    async def emit_dimension(self, items: Sequence[CandidateDimension]) -> None:
        await self._guard()
        for it in items:
            await self._insert("staging_dimension", self._values(it))
        await self._conn.commit()

    async def emit_metric_calibers(
        self, items: Sequence[CandidateMetricCaliber]
    ) -> None:
        await self._guard()
        for it in items:
            await self._insert(
                "staging_metric_caliber", self._values(it, dataset_ref=True)
            )
        await self._conn.commit()

    async def emit_metric(self, items: Sequence[CandidateMetric]) -> None:
        await self._guard()
        for it in items:
            await self._insert("staging_metric", self._values(it))
        await self._conn.commit()

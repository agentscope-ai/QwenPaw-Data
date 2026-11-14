"""跨源业务测试夹具：只 mock 物理输入与 LLM，审核和持久化使用生产实现。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pytest
from qwenpaw_data_auth import Principal
from profiling.algo import AlgoConfig, AlgoContexts, AlgoProfiler, ProfileTask
from profiling.algo.handlers.dictionary import DictHandler
from profiling.algo.snapshots import TableSnapshot
from profiling.algo.tasks import OUTPUT_CONTEXT
from profiling.algo_provider import (
    AlgoProvider,
    map_biz_domains,
    map_datasets,
    map_dimension_calibers,
    map_dimensions,
    map_metric_calibers,
    map_metrics,
)
from profiling.repositories import probe_stage_repo, staging_repo
from profiling.services import promote, row_service
from profiling.services.context import build_context
from profiling.services.sink import DbProbeSink
from profiling.stages import def_by_stage
from semantic_config.repositories import datasource_repo
from semantic_config.tables import metadata, physical_table, table_column
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@dataclass
class BusinessTable:
    physical_name: str
    domain: str
    dataset_name: str
    columns: list[dict]
    roles: list[dict]


@pytest.fixture
def dataset_field_tables(business_flow, monkeypatch):
    """只替换当前源物理元数据 I/O，Dataset source 解析和 sink 均使用生产代码。"""

    async def load(self, ctx):
        tables = business_flow.tables[ctx.datasource_id]
        return TableSnapshot.build(
            pd.DataFrame(
                [
                    {"table_name": table.physical_name, "table_comment": table.domain}
                    for table in tables
                ]
            ),
            {
                table.physical_name: pd.DataFrame(
                    [
                        {
                            "table_name": table.physical_name,
                            "is_primary": "N",
                            "is_nullable": "Y",
                            **col,
                        }
                        for col in table.columns
                    ]
                )
                for table in tables
            },
        ), "postgres"

    monkeypatch.setattr(AlgoProvider, "_load_dataset_field_tables", load)


@pytest.fixture
def orders_table():
    """相同物理列名和不同本源样例/公式，防止伪造表名前缀掩盖隔离问题。"""

    def make(
        source,
        *,
        domain="sales",
        physical="public.orders",
        suffix="",
        dimension="客户国家",
        metric="销售收入",
        window="1d",
    ):
        samples = {
            "a": ["中国", "日本"],
            "b": ["法国", "德国"],
            "c": ["美国", "加拿大"],
        }[source]
        return BusinessTable(
            physical,
            domain,
            f"{domain}_orders_{source}{suffix}",
            [
                {
                    "column_name": "country",
                    "column_comment": "买方所在国家",
                    "data_type": "varchar",
                    "column_enums": samples,
                },
                {
                    "column_name": "amount",
                    "column_comment": "订单成交金额",
                    "data_type": "decimal",
                },
            ],
            [
                {
                    "column_name": "country",
                    "column_role": "dimension",
                    "dimension_name": dimension,
                },
                {
                    "column_name": "amount",
                    "column_role": "metric",
                    "metric_name": metric,
                    "expr": f"SUM(amount * {dict(a=1, b=100, c=1000)[source]})",
                    "time_window": window,
                    "additivity": "additive",
                },
            ],
        )

    return make


class BusinessLlm:
    def __init__(self, tables, *, matches=None, match_failure=None):
        self.tables = tables
        self.matches = matches or {}
        self.match_failure = match_failure
        self.catalog_payloads = []
        self.role_payloads = []

    async def complete(self, system_prompt, user_prompt, *, temperature=0.2):
        if "reusable_entities" in user_prompt:
            payload = json.loads(user_prompt.rsplit("\n\n", 1)[-1])
            self.catalog_payloads.append(payload)
            if self.match_failure == "exception":
                raise RuntimeError("mock semantic matching outage")
            if self.match_failure == "invalid_json":
                return "{invalid json"
            if self.match_failure == "incomplete":
                return "[]"
            return json.dumps(
                [
                    {
                        "name": item["name"],
                        "canonical_name": self.matches.get(
                            (
                                payload["domain_name"],
                                payload["entity_kind"],
                                item["name"],
                            ),
                            item["name"],
                        ),
                    }
                    for item in payload["candidates"]
                ],
                ensure_ascii=False,
            )
        if "column_role" in system_prompt:
            name = user_prompt.split("`", 2)[1]
            self.role_payloads.append(json.loads(user_prompt.rsplit("\n\n", 1)[-1]))
            table = next(table for table in self.tables if table.dataset_name == name)
            return json.dumps(
                [
                    {
                        "confidence": 0.95,
                        "reason": "mock business classification",
                        **role,
                    }
                    for role in table.roles
                ],
                ensure_ascii=False,
            )
        # DOMAIN: 只响应本轮输入表，已确认的历史成员不在该集合内。
        payload = json.loads(user_prompt.rsplit("\n\n", 1)[-1])
        input_names = {r["table_name"] for r in payload["unclustered_items"]}
        input_names.update(
            r["table_name"] for c in payload["cluster_items"] for r in c["tables"]
        )
        # 故意每表一行，同一个域的多个表由生产代码合并。
        return json.dumps(
            {
                "domains": [
                    {
                        "domain": table.domain,
                        "display_name": table.domain.title(),
                        "description": f"业务域 {table.domain}",
                        "tables": [table.physical_name],
                    }
                    for table in self.tables
                    if table.physical_name in input_names
                ]
            },
            ensure_ascii=False,
        )


@dataclass
class BusinessFlow:
    session: AsyncSession
    tmp_path: Path
    tenant: str = "business-tenant"
    workspace: str = "business-workspace"
    tables: dict[str, list[BusinessTable]] = field(default_factory=dict)
    sequence: int = 0

    @property
    def principal(self):
        return Principal(
            user_id="reviewer",
            tenant_id=self.tenant,
            workspace_ids=[self.workspace],
            active_workspace_id=self.workspace,
            role="tenant_admin",
        )

    @property
    def scope(self):
        return dict(
            tenant_id=self.tenant, workspace_id=self.workspace, created_by="reviewer"
        )

    async def add_source(self, source, tables):
        await datasource_repo.insert(
            self.session, source, source, "sqlite", "{}", **self.scope
        )
        await probe_stage_repo.ensure_stages(self.session, source, **self.scope)
        self.tables[source] = tables
        for table in tables:
            result = await self.session.execute(
                insert(physical_table).values(
                    datasource_id=source,
                    table_name=table.physical_name,
                    **self.scope,
                )
            )
            for column in table.columns:
                await self.session.execute(
                    insert(table_column).values(
                        datasource_id=source,
                        table_id=result.inserted_primary_key[0],
                        column_name=column["column_name"],
                        data_type=column["data_type"],
                        column_comment=column.get("column_comment"),
                        sample_values="$$$".join(column.get("column_enums", []))
                        or None,
                        **self.scope,
                    )
                )
        await self.session.commit()

    async def context(self, source):
        ds = await datasource_repo.find_by_datasource_id(
            self.session, source, self.tenant, self.workspace
        )
        return await build_context(
            self.session, ds, self.tenant, self.workspace, self.principal
        )

    async def profile(
        self, source, task, *, llm=None, mode="create", matches=None, match_failure=None
    ):
        context = await self.context(source)
        self.sequence += 1
        config = AlgoConfig(
            _env_file=None,
            datasource_id=source,
            datasource_type="sqlite",
            datasource_connection={},
            llm_api_key="",
            handler_mode=mode,
            store_out_dir=str(self.tmp_path / str(self.sequence)),
            store_excel_enabled=False,
            store_include_contexts=("dataset_staged",),
            dataset_derived_from_tables_include_patterns=("%",),
            dataset_parse_from_logs_enabled=False,
        )
        contexts = AlgoContexts.build(config, offline=True, facts=context.facts)
        tables = self.tables[source]
        await contexts.source_table.save(
            TableSnapshot.build(
                pd.DataFrame(
                    [
                        {"table_name": t.physical_name, "table_comment": t.domain}
                        for t in tables
                    ]
                ),
                {
                    t.physical_name: pd.DataFrame(
                        [
                            {
                                "table_name": t.physical_name,
                                "is_primary": "N",
                                "is_nullable": "Y",
                                **c,
                            }
                            for c in t.columns
                        ]
                    )
                    for t in tables
                },
            )
        )
        llm = llm or BusinessLlm(tables, matches=matches, match_failure=match_failure)
        profiler = (
            AlgoProfiler.build(contexts, handlers={ProfileTask.DICT: DictHandler(None)})
            if task == ProfileTask.DICT
            else AlgoProfiler.build(contexts, llm=llm)
        )
        result = await profiler.run(task)
        assert result.executed, result.reason
        return getattr(contexts, OUTPUT_CONTEXT[task]).current(), llm

    async def sink(self, source):
        self.sequence += 1
        return DbProbeSink(
            self.session,
            await self.context(source),
            datasource_id=source,
            task_id=f"business-{self.sequence}",
            **self.scope,
        )

    async def rows(self, source, stage):
        return await staging_repo.list_all(
            self.session, def_by_stage(stage).table, source, self.tenant, self.workspace
        )

    async def review(self, source, stage, *, confirm=True, ids=None):
        if ids is None:
            ids = [
                r["id"]
                for r in await self.rows(source, stage)
                if r["probe_status"] == "PENDING"
            ]
        fn = row_service.batch_confirm if confirm else row_service.batch_skip
        result = await fn(
            self.session, def_by_stage(stage), source, ids, self.principal
        )
        assert next(iter(result.values())) == len(ids)
        await self.session.commit()

    async def complete(self, source, stage):
        result = await promote.complete(self.session, source, stage, self.principal)
        await self.session.commit()
        return result

    async def model_datasets(self, source, *, review_names=True):
        domains, _ = await self.profile(source, ProfileTask.DOMAIN)
        await (await self.sink(source)).emit_biz_domains(
            map_biz_domains(domains.to_frames())
        )
        await self.review(source, "BIZ_DOMAIN")
        domain_result = await self.complete(source, "BIZ_DOMAIN")
        datasets, _ = await self.profile(source, ProfileTask.DATASET)
        candidates = map_datasets(datasets.to_frames())
        assert {c.source for c in candidates} == {
            t.physical_name for t in self.tables[source]
        }
        sink = await self.sink(source)
        await sink.emit_datasets(candidates)
        # 模拟工作台审核：逻辑 Dataset 使用用户指定的业务名；物理 source 保持原样。
        # 单独的碰撞测试关闭该步骤，验证默认同名直通的真实行为。
        aliases = {t.physical_name: t.dataset_name for t in self.tables[source]}
        if review_names:
            for row in await self.rows(source, "DATASET"):
                await row_service.update_row(
                    self.session,
                    def_by_stage("DATASET"),
                    source,
                    row["id"],
                    {"dataset_name": aliases[row["source"]]},
                    self.principal,
                )
            candidates = [
                c.model_copy(update={"dataset_name": aliases[c.source]})
                for c in candidates
            ]
        await self.review(source, "DATASET")
        await self.complete(source, "DATASET")
        await sink.emit_dataset_fields(candidates)
        await self.review(source, "DATASET_FIELDS")
        dataset_result = await self.complete(source, "DATASET_FIELDS")
        return domain_result, dataset_result

    async def stage_caliber(self, source, snapshot):
        sink = await self.sink(source)
        await sink.emit_dimension_calibers(map_dimension_calibers(snapshot.to_frames()))
        await sink.emit_metric_calibers(map_metric_calibers(snapshot.to_frames()))

    async def finish_caliber(self, source, snapshot):
        await self.stage_caliber(source, snapshot)
        for stage in ("DIMENSION_CALIBER", "METRIC_CALIBER"):
            await self.review(source, stage)
            await self.complete(source, stage)
        dictionary, _ = await self.profile(source, ProfileTask.DICT)
        sink = await self.sink(source)
        await sink.emit_dimension(map_dimensions(dictionary.to_frames()))
        await sink.emit_metric(map_metrics(dictionary.to_frames()))
        return (
            await self.complete(source, "DIMENSION_MANAGE"),
            await self.complete(source, "METRIC_MANAGE"),
        )

    async def formal(self, table):
        return [
            dict(r)
            for r in (
                await self.session.execute(
                    select(table).where(
                        table.c.tenant_id == self.tenant,
                        table.c.workspace_id == self.workspace,
                        table.c.is_deleted == 0,
                    )
                )
            ).mappings()
        ]


@pytest.fixture
async def business_flow(tmp_path: Path) -> AsyncIterator[BusinessFlow]:
    import profiling.tables  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield BusinessFlow(session, tmp_path)
    finally:
        await engine.dispose()

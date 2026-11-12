"""CALIBER 跨数据源复用 Metric / Dimension 的业务场景测试。

新正式表按 ``(datasource_id, dataset_name)`` 唯一：数据库 E2E 中三个源的
逻辑 Dataset 和物理 source 都叫 ``public.orders``。Domain / Dimension / Metric
共享正式 ID，Dataset / Binding / Caliber 分源保存。算法编排测试进一步验证
已确认关系只锁当前 datasource，不会因为同名 Dataset 误锁其他来源。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from qwenpaw_data_auth import Principal
from profiling.algo import AlgoConfig, AlgoContexts, AlgoProfiler, ProfileTask
from profiling.algo.evidence import CaliberEvidence, TaskEvidence
from profiling.algo.handlers.dictionary import DictHandler
from profiling.algo.snapshots import CaliberSnapshot, DictSnapshot
from profiling.algo_provider import (
    map_dimension_calibers,
    map_dimensions,
    map_metric_calibers,
    map_metrics,
)
from profiling.provider import ProbeContext
from profiling.repositories import probe_stage_repo, staging_repo
from profiling.services import promote, row_service
from profiling.services.context import build_context
from profiling.services.sink import DbProbeSink
from profiling.stages import def_by_stage
from semantic_config.models.dataset_field import DatasetFieldCreate
from semantic_config.repositories import (
    dataset_field_repo,
    datasource_repo,
)
from semantic_config.tables import (
    dataset,
    dataset_dimension,
    dimension_dict,
    domain,
    metadata,
    metric_caliber,
    metric_dict,
)
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

TENANT_ID = "tenant-caliber-reuse"
WORKSPACE_ID = "workspace-caliber-reuse"
DOMAIN_NAME = "sales"
CANONICAL_DIMENSION = "客户国家"
CANONICAL_METRIC = "销售收入"


@dataclass(frozen=True)
class _SourceSpec:
    datasource_id: str
    dataset_name: str
    dimension_column: str
    metric_column: str
    provisional_dimension: str
    provisional_metric: str
    metric_expr: str
    time_window: str


SOURCE_SPECS: tuple[_SourceSpec, ...] = (
    _SourceSpec(
        "source-a",
        "public.orders",
        "country_code_a",
        "gross_amount_a",
        CANONICAL_DIMENSION,
        CANONICAL_METRIC,
        "SUM(gross_amount_a)",
        "1d",
    ),
    _SourceSpec(
        "source-b",
        "public.orders",
        "buyer_nation_b",
        "paid_value_b",
        "购方所在国",
        "订单成交额",
        "SUM(paid_value_b)",
        "1d",
    ),
    _SourceSpec(
        "source-c",
        "public.orders",
        "ship_country_c",
        "gmv_value_c",
        "收货国家",
        "GMV",
        "SUM(gmv_value_c)",
        "7d",
    ),
)


class _CaliberLlm:
    """逐表抽取 + 按 Domain 复用的确定性 LLM。"""

    def __init__(self, spec: _SourceSpec) -> None:
        self.spec = spec
        self.match_payloads: list[dict[str, Any]] = []

    def _role_response(self) -> list[dict[str, Any]]:
        return [
            {
                "column_name": self.spec.dimension_column,
                "column_role": "dimension",
                "dimension_name": self.spec.provisional_dimension,
                "metric_name": None,
                "time_window": None,
                "additivity": None,
                "expr": None,
                "reason": "订单所属国家",
                "confidence": 0.95,
            },
            {
                "column_name": self.spec.metric_column,
                "column_role": "metric",
                "dimension_name": None,
                "metric_name": self.spec.provisional_metric,
                "time_window": self.spec.time_window,
                "additivity": "additive",
                "expr": self.spec.metric_expr,
                "reason": "订单收入金额",
                "confidence": 0.95,
            },
        ]

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
    ) -> str:
        del system_prompt, temperature
        if "reusable_entities" not in user_prompt:
            return json.dumps(self._role_response(), ensure_ascii=False)

        payload = json.loads(user_prompt.rsplit("\n\n", 1)[-1])
        self.match_payloads.append(payload)
        reusable_names = {str(item["name"]) for item in payload["reusable_entities"]}
        target = (
            CANONICAL_DIMENSION
            if CANONICAL_DIMENSION in reusable_names
            else CANONICAL_METRIC
            if CANONICAL_METRIC in reusable_names
            else None
        )
        return json.dumps(
            [
                {
                    "name": candidate["name"],
                    "canonical_name": target or candidate["name"],
                }
                for candidate in payload["candidates"]
            ],
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class _BusinessDb:
    session: AsyncSession
    principal: Principal
    datasource_rows: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class _CaliberRun:
    context: ProbeContext
    snapshot: CaliberSnapshot
    llm: _CaliberLlm


@pytest.fixture
async def business_db() -> AsyncIterator[_BusinessDb]:
    # semantic_config 与 profiling 共用 metadata；显式 import 注册 staging 表。
    import profiling.tables  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            principal = Principal(
                user_id="alice",
                tenant_id=TENANT_ID,
                workspace_ids=[WORKSPACE_ID],
                active_workspace_id=WORKSPACE_ID,
                role="tenant_admin",
            )
            scope = dict(
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                created_by=principal.user_id,
            )
            created = await session.execute(
                insert(domain).values(
                    domain_name=DOMAIN_NAME,
                    description="订单销售业务",
                    **scope,
                )
            )
            domain_id = int(created.inserted_primary_key[0])
            for spec in SOURCE_SPECS:
                await datasource_repo.insert(
                    session,
                    spec.datasource_id,
                    spec.datasource_id,
                    "sqlite",
                    "{}",
                    tenant_id=TENANT_ID,
                    workspace_id=WORKSPACE_ID,
                    created_by=principal.user_id,
                )
                await probe_stage_repo.ensure_stages(
                    session,
                    spec.datasource_id,
                    tenant_id=TENANT_ID,
                    workspace_id=WORKSPACE_ID,
                    created_by=principal.user_id,
                )
                created = await session.execute(
                    insert(dataset).values(
                        datasource_id=spec.datasource_id,
                        domain_id=domain_id,
                        dataset_name=spec.dataset_name,
                        description=f"{spec.datasource_id} 的订单表",
                        source="public.orders",
                        **scope,
                    )
                )
                dataset_id = int(created.inserted_primary_key[0])
                for column_name, description, data_type in (
                    (spec.dimension_column, "客户或收货国家", "varchar"),
                    (spec.metric_column, "订单成交收入金额", "decimal"),
                ):
                    await dataset_field_repo.insert(
                        session,
                        DatasetFieldCreate(
                            datasource_id=spec.datasource_id,
                            domain_id=domain_id,
                            dataset_id=dataset_id,
                            field_name=column_name,
                            description=description,
                            data_type=data_type,
                            is_primary="N",
                            is_nullable="Y",
                        ),
                        tenant_id=TENANT_ID,
                        workspace_id=WORKSPACE_ID,
                        created_by=principal.user_id,
                    )
            await session.commit()

            datasource_rows = {
                spec.datasource_id: await datasource_repo.find_by_datasource_id(
                    session,
                    spec.datasource_id,
                    TENANT_ID,
                    WORKSPACE_ID,
                )
                for spec in SOURCE_SPECS
            }
            assert all(datasource_rows.values())
            yield _BusinessDb(session, principal, datasource_rows)
    finally:
        await engine.dispose()


def _config(tmp_path: Path, datasource_id: str) -> AlgoConfig:
    return AlgoConfig(
        _env_file=None,
        datasource_id=datasource_id,
        datasource_type="sqlite",
        datasource_connection={},
        store_out_dir=str(tmp_path),
        store_excel_enabled=False,
        # 被测 context 只走事实输入和内存 current，避免本地缓存参与隔离。
        store_include_contexts=("domain_staged",),
        llm_api_key="",
    )


async def _profile_caliber(
    business: _BusinessDb,
    tmp_path: Path,
    spec: _SourceSpec,
) -> _CaliberRun:
    context = await build_context(
        business.session,
        business.datasource_rows[spec.datasource_id],
        TENANT_ID,
        WORKSPACE_ID,
        business.principal,
    )
    contexts = AlgoContexts.build(
        _config(tmp_path, spec.datasource_id),
        offline=True,
        facts=context.facts,
    )
    llm = _CaliberLlm(spec)
    outcome = await AlgoProfiler.build(contexts, llm=llm).run(ProfileTask.CALIBER)
    assert outcome.executed, outcome.reason
    return _CaliberRun(context, contexts.columns_caliber_staged.current(), llm)


async def _confirm_stage_rows(
    business: _BusinessDb,
    datasource_id: str,
    stage: str,
    task_id: str,
) -> None:
    sdef = def_by_stage(stage)
    rows = await staging_repo.list_all(
        business.session,
        sdef.table,
        datasource_id,
        TENANT_ID,
        WORKSPACE_ID,
    )
    current = [row for row in rows if row["task_id"] == task_id]
    assert len(current) == 1
    result = await row_service.batch_confirm(
        business.session,
        sdef,
        datasource_id,
        [current[0]["id"]],
        business.principal,
    )
    assert result == {"confirmed": 1}
    completed = await promote.complete(
        business.session,
        datasource_id,
        stage,
        business.principal,
    )
    assert completed["status"] == "COMPLETED"


async def _stage_calibers(
    business: _BusinessDb,
    spec: _SourceSpec,
    run: _CaliberRun,
) -> None:
    frames = run.snapshot.to_frames()
    dimensions = map_dimension_calibers(frames)
    metrics = map_metric_calibers(frames)
    assert len(dimensions) == len(metrics) == 1
    assert dimensions[0].dimension_name == CANONICAL_DIMENSION
    assert metrics[0].metric_name == CANONICAL_METRIC
    assert dimensions[0].expr == spec.dimension_column
    assert metrics[0].expr == spec.metric_expr
    assert metrics[0].time_window == spec.time_window

    task_id = f"CALIBER-{spec.datasource_id}"
    sink = DbProbeSink(
        business.session,
        run.context,
        datasource_id=spec.datasource_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        created_by=business.principal.user_id,
        task_id=task_id,
    )
    await sink.emit_dimension_calibers(dimensions)
    await sink.emit_metric_calibers(metrics)
    await _confirm_stage_rows(
        business, spec.datasource_id, "DIMENSION_CALIBER", task_id
    )
    await _confirm_stage_rows(business, spec.datasource_id, "METRIC_CALIBER", task_id)
    await business.session.commit()


async def _profile_dict(
    business: _BusinessDb,
    tmp_path: Path,
    spec: _SourceSpec,
) -> tuple[ProbeContext, DictSnapshot]:
    context = await build_context(
        business.session,
        business.datasource_rows[spec.datasource_id],
        TENANT_ID,
        WORKSPACE_ID,
        business.principal,
    )
    contexts = AlgoContexts.build(
        _config(tmp_path, spec.datasource_id),
        offline=True,
        facts=context.facts,
    )
    profiler = AlgoProfiler.build(
        contexts,
        handlers={ProfileTask.DICT: DictHandler(None)},
    )
    outcome = await profiler.run(ProfileTask.DICT)
    assert outcome.executed, outcome.reason
    return context, contexts.columns_dict_staged.current()


async def _stage_dict_and_complete(
    business: _BusinessDb,
    spec: _SourceSpec,
    context: ProbeContext,
    snapshot: DictSnapshot,
) -> tuple[dict[str, Any], dict[str, Any]]:
    dimensions = map_dimensions(snapshot.to_frames())
    metrics = map_metrics(snapshot.to_frames())
    # DICT 只补描述/同义词，不得二次修改 CALIBER 已确定的 canonical name。
    assert [item.dimension_name for item in dimensions] == [CANONICAL_DIMENSION]
    assert [item.metric_name for item in metrics] == [CANONICAL_METRIC]

    sink = DbProbeSink(
        business.session,
        context,
        datasource_id=spec.datasource_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        created_by=business.principal.user_id,
        task_id=f"DICT-{spec.datasource_id}",
    )
    await sink.emit_dimension(dimensions)
    await sink.emit_metric(metrics)
    dim_result = await promote.complete(
        business.session,
        spec.datasource_id,
        "DIMENSION_MANAGE",
        business.principal,
    )
    metric_result = await promote.complete(
        business.session,
        spec.datasource_id,
        "METRIC_MANAGE",
        business.principal,
    )
    await business.session.commit()
    return dim_result, metric_result


async def test_three_datasources_reuse_entities_but_keep_local_calibers(
    business_db: _BusinessDb,
    tmp_path: Path,
) -> None:
    """A 建实体，B/C 语义复用；实体各一份、关系与本源表达式各三份。"""

    manage_results: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    runs: dict[str, _CaliberRun] = {}
    for index, spec in enumerate(SOURCE_SPECS):
        run = await _profile_caliber(business_db, tmp_path, spec)
        runs[spec.datasource_id] = run

        if index:
            # build_context 已按父 Dataset 收窄正式关系：A（以及之前的源）不会
            # 出现在 B/C 的 locked prior 中，即便物理 source 完全相同。
            assert run.context.facts["dimension_caliber"].empty
            assert run.context.facts["metric_caliber"].empty
            reusable = {
                item["name"]
                for payload in run.llm.match_payloads
                for item in payload["reusable_entities"]
            }
            assert reusable == {CANONICAL_DIMENSION, CANONICAL_METRIC}

        caliber_rows = run.snapshot.calibers.set_index("column_name")
        assert caliber_rows.loc[spec.dimension_column, "dimension_name"] == (
            CANONICAL_DIMENSION
        )
        assert caliber_rows.loc[spec.metric_column, "metric_name"] == CANONICAL_METRIC
        assert caliber_rows.loc[spec.metric_column, "expr"] == spec.metric_expr
        assert caliber_rows.loc[spec.metric_column, "time_window"] == spec.time_window

        await _stage_calibers(business_db, spec, run)
        dict_context, dictionary = await _profile_dict(business_db, tmp_path, spec)
        manage_results[spec.datasource_id] = await _stage_dict_and_complete(
            business_db, spec, dict_context, dictionary
        )

        if index == 0:
            rerun = await _profile_caliber(business_db, tmp_path, spec)
            assert rerun.snapshot.is_empty()

    assert manage_results["source-a"][0]["promoted_dimension"] == 1
    assert manage_results["source-a"][1]["promoted_metric"] == 1
    for datasource_id in ("source-b", "source-c"):
        dim_result, metric_result = manage_results[datasource_id]
        assert dim_result["promoted_dimension"] == 0
        assert metric_result["promoted_metric"] == 0
    assert all(
        results[0]["promoted_dataset_dimension"] == 1
        and results[1]["promoted_metric_caliber"] == 1
        for results in manage_results.values()
    )

    formal_dimensions = (
        (
            await business_db.session.execute(
                select(dimension_dict.c.id, dimension_dict.c.dimension_name).where(
                    dimension_dict.c.tenant_id == TENANT_ID,
                    dimension_dict.c.workspace_id == WORKSPACE_ID,
                    dimension_dict.c.is_deleted == 0,
                )
            )
        )
        .mappings()
        .all()
    )
    formal_metrics = (
        (
            await business_db.session.execute(
                select(metric_dict.c.id, metric_dict.c.metric_name).where(
                    metric_dict.c.tenant_id == TENANT_ID,
                    metric_dict.c.workspace_id == WORKSPACE_ID,
                    metric_dict.c.is_deleted == 0,
                )
            )
        )
        .mappings()
        .all()
    )
    assert [(row["dimension_name"]) for row in formal_dimensions] == [
        CANONICAL_DIMENSION
    ]
    assert [(row["metric_name"]) for row in formal_metrics] == [CANONICAL_METRIC]
    dimension_id = formal_dimensions[0]["id"]
    metric_id = formal_metrics[0]["id"]

    bindings = (
        (
            await business_db.session.execute(
                select(
                    dataset.c.id.label("dataset_id"),
                    dataset.c.dataset_name,
                    dataset.c.datasource_id,
                    dataset.c.source,
                    dataset_dimension.c.dimension_id,
                    dataset_dimension.c.expr,
                )
                .select_from(
                    dataset_dimension.join(
                        dataset,
                        dataset.c.id == dataset_dimension.c.dataset_id,
                    )
                )
                .where(
                    dataset_dimension.c.tenant_id == TENANT_ID,
                    dataset_dimension.c.workspace_id == WORKSPACE_ID,
                    dataset_dimension.c.is_deleted == 0,
                )
            )
        )
        .mappings()
        .all()
    )
    calibers = (
        (
            await business_db.session.execute(
                select(
                    dataset.c.id.label("dataset_id"),
                    dataset.c.dataset_name,
                    dataset.c.datasource_id,
                    dataset.c.source,
                    metric_caliber.c.metric_id,
                    metric_caliber.c.expr,
                    metric_caliber.c.time_window,
                )
                .select_from(
                    metric_caliber.join(
                        dataset, dataset.c.id == metric_caliber.c.dataset_id
                    )
                )
                .where(
                    metric_caliber.c.tenant_id == TENANT_ID,
                    metric_caliber.c.workspace_id == WORKSPACE_ID,
                    metric_caliber.c.is_deleted == 0,
                )
            )
        )
        .mappings()
        .all()
    )
    assert len(bindings) == len(calibers) == 3
    assert {row["dataset_name"] for row in [*bindings, *calibers]} == {"public.orders"}
    assert len({row["dataset_id"] for row in bindings}) == 3
    assert {row["dataset_id"] for row in bindings} == {
        row["dataset_id"] for row in calibers
    }
    assert {row["dimension_id"] for row in bindings} == {dimension_id}
    assert {row["metric_id"] for row in calibers} == {metric_id}
    assert {row["source"] for row in [*bindings, *calibers]} == {"public.orders"}
    assert {row["datasource_id"]: row["expr"] for row in bindings} == {
        spec.datasource_id: spec.dimension_column for spec in SOURCE_SPECS
    }
    assert {
        row["datasource_id"]: (row["expr"], row["time_window"]) for row in calibers
    } == {
        spec.datasource_id: (spec.metric_expr, spec.time_window)
        for spec in SOURCE_SPECS
    }

    assert (
        await business_db.session.scalar(
            select(func.count()).select_from(dimension_dict)
        )
        == 1
    )
    assert (
        await business_db.session.scalar(select(func.count()).select_from(metric_dict))
        == 1
    )


def _same_name_facts(spec: _SourceSpec, *, locked: bool) -> dict[str, list[dict]]:
    """每个 datasource 各收一份隔离事实，逻辑表名均为 public.orders。"""

    common = {
        "dataset_name": "public.orders",
        "domain_name": DOMAIN_NAME,
        "domain_id": 1,
    }
    facts: dict[str, list[dict]] = {
        "domain": [
            {
                "id": 1,
                "domain_name": DOMAIN_NAME,
                "description": "订单销售业务",
            }
        ],
        "dataset": [
            {
                **common,
                "id": 10,
                "datasource_id": spec.datasource_id,
                "description": "订单表",
                "source": "public.orders",
            }
        ],
        "dataset_fields": [
            {
                **common,
                "dataset_id": 10,
                "field_name": spec.dimension_column,
                "description": "客户国家",
                "data_type": "varchar",
                "is_primary": "N",
                "is_nullable": "Y",
            },
            {
                **common,
                "dataset_id": 10,
                "field_name": spec.metric_column,
                "description": "销售收入",
                "data_type": "decimal",
                "is_primary": "N",
                "is_nullable": "Y",
            },
        ],
        "dimension": [
            {
                "domain_name": DOMAIN_NAME,
                "dimension_name": CANONICAL_DIMENSION,
                "description": "客户所在国家",
                "synonyms": "国家",
                "dimension_type": "categorical",
            }
        ],
        "metric": [
            {
                "domain_name": DOMAIN_NAME,
                "metric_name": CANONICAL_METRIC,
                "description": "订单销售收入",
                "synonyms": "收入",
            }
        ],
        "dimension_caliber": [],
        "metric_caliber": [],
    }
    if locked:
        facts["dimension_caliber"] = [
            {
                **common,
                "column_name": spec.dimension_column,
                "dimension_name": CANONICAL_DIMENSION,
                "expr": spec.dimension_column,
                "value_example": None,
            }
        ]
        facts["metric_caliber"] = [
            {
                **common,
                "column_name": spec.metric_column,
                "metric_name": CANONICAL_METRIC,
                "expr": spec.metric_expr,
                "time_window": spec.time_window,
                "additivity": "additive",
            }
        ]
    return facts


async def test_confirmed_same_named_dataset_only_locks_source_a(
    tmp_path: Path,
) -> None:
    """A 的 public.orders 已确认，B/C 的 public.orders 仍各自产出口径。"""

    snapshots: dict[str, CaliberSnapshot] = {}
    for spec in SOURCE_SPECS:
        contexts = AlgoContexts.build(
            _config(tmp_path, f"same-name-{spec.datasource_id}"),
            offline=True,
            facts=_same_name_facts(spec, locked=spec.datasource_id == "source-a"),
        )
        llm = _CaliberLlm(spec)
        outcome = await AlgoProfiler.build(contexts, llm=llm).run(ProfileTask.CALIBER)
        assert outcome.executed, outcome.reason
        snapshots[spec.datasource_id] = contexts.columns_caliber_staged.current()

    assert snapshots["source-a"].is_empty()
    for datasource_id in ("source-b", "source-c"):
        snapshot = snapshots[datasource_id]
        assert set(snapshot.calibers["table_name"]) == {"public.orders"}
        assert set(snapshot.calibers["column_role"]) == {"dimension", "metric"}
        assert set(snapshot.calibers["dimension_name"].dropna()) == {
            CANONICAL_DIMENSION
        }
        assert set(snapshot.calibers["metric_name"].dropna()) == {CANONICAL_METRIC}


async def test_dict_keeps_distinct_canonical_names_with_same_physical_column() -> None:
    """同一物理列名不能诱使 DICT 把 CALIBER 已定的两个实体名再次合并。"""

    evidence = TaskEvidence(
        task=ProfileTask.DICT,
        calibers=CaliberEvidence(
            pd.DataFrame(
                [
                    {
                        "table_name": "orders_a",
                        "column_name": "country_code",
                        "column_comment": "账单国家",
                        "domain_name": DOMAIN_NAME,
                        "column_role": "dimension",
                        "dimension_name": "账单国家",
                    },
                    {
                        "table_name": "orders_b",
                        "column_name": "country_code",
                        "column_comment": "收货国家",
                        "domain_name": DOMAIN_NAME,
                        "column_role": "dimension",
                        "dimension_name": "收货国家",
                    },
                ]
            )
        ),
    )

    snapshot = await DictHandler(None).handle(evidence)

    assert set(snapshot.dimensions["dimension_name"]) == {"账单国家", "收货国家"}
    assert snapshot.metrics.empty

"""Domain profiling 跨数据源复用的业务场景测试。

这里刻意使用完全相同的物理表名 ``public.orders``，不把 datasource id
编码进 ``table_name``。测试走生产事实加载、算法编排、staging、人工确认与
Domain complete，验证隔离来自 datasource scope，而不是测试数据里的伪前缀。
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
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from profiling.algo import AlgoConfig, AlgoContexts, AlgoProfiler, ProfileTask
from profiling.algo.snapshots import DomainSnapshot, TableSnapshot
from profiling.algo_provider import map_biz_domains
from profiling.provider import ProbeContext
from profiling.repositories import probe_stage_repo, staging_repo
from profiling.services import promote, row_service
from profiling.services.context import build_context
from profiling.services.sink import DbProbeSink
from profiling.stages import def_by_stage
from semantic_config.repositories import datasource_repo
from semantic_config.tables import dataset, domain, metadata


TENANT_ID = "tenant-domain-reuse"
WORKSPACE_ID = "workspace-domain-reuse"
DATASOURCE_IDS = ("source-a", "source-b", "source-c")
DOMAIN_STAGE = "BIZ_DOMAIN"


class _FakeLlm:
    """DomainHandler 所需的确定性 LLM；完整覆盖输入时不会触发 retry。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str, float]] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
    ) -> str:
        self.calls.append((system_prompt, user_prompt, temperature))
        return self.response


@dataclass(frozen=True)
class _BusinessDb:
    session: AsyncSession
    principal: Principal
    datasource_rows: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class _ProfileRun:
    context: ProbeContext
    snapshot: DomainSnapshot
    llm: _FakeLlm


@pytest.fixture
async def business_db() -> AsyncIterator[_BusinessDb]:
    # semantic_config 与 profiling 共用 metadata；显式 import 触发 staging 表注册。
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
            for datasource_id in DATASOURCE_IDS:
                await datasource_repo.insert(
                    session,
                    datasource_id,
                    datasource_id,
                    "sqlite",
                    "{}",
                    tenant_id=TENANT_ID,
                    workspace_id=WORKSPACE_ID,
                    created_by=principal.user_id,
                )
                await probe_stage_repo.ensure_stages(
                    session,
                    datasource_id,
                    tenant_id=TENANT_ID,
                    workspace_id=WORKSPACE_ID,
                    created_by=principal.user_id,
                )
            await session.commit()

            datasource_rows = {
                datasource_id: await datasource_repo.find_by_datasource_id(
                    session, datasource_id, TENANT_ID, WORKSPACE_ID
                )
                for datasource_id in DATASOURCE_IDS
            }
            assert all(datasource_rows.values())
            yield _BusinessDb(session, principal, datasource_rows)
    finally:
        await engine.dispose()


def _domain_response(domain_tables: Mapping[str, tuple[str, ...]]) -> str:
    return json.dumps(
        {
            "domains": [
                {
                    "domain": domain_name,
                    "display_name": domain_name.title(),
                    "description": f"{domain_name} domain",
                    "tables": list(tables),
                }
                for domain_name, tables in domain_tables.items()
            ]
        }
    )


def _source_snapshot(table_names: tuple[str, ...]) -> TableSnapshot:
    return TableSnapshot.build(
        pd.DataFrame(
            [
                {
                    "table_name": table_name,
                    "table_comment": f"business table {table_name}",
                }
                for table_name in table_names
            ]
        )
    )


def _member_tables_of(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("member_tables")
    return json.loads(raw) if isinstance(raw, str) else list(raw or [])


def _reusable_domain_names(llm: _FakeLlm) -> set[str]:
    if not llm.calls:
        return set()
    prompt = llm.calls[0][1]
    marker = "可复用域：\n"
    if marker not in prompt:
        return set()
    catalog_json = prompt.split(marker, 1)[1].split("\n\n以下是 JOIN", 1)[0]
    return {row["domain"] for row in json.loads(catalog_json)}


async def _profile_domains(
    business: _BusinessDb,
    tmp_path: Path,
    datasource_id: str,
    domain_tables: Mapping[str, tuple[str, ...]],
) -> _ProfileRun:
    """走 build_context → AlgoContexts → AlgoProfiler 的 DOMAIN 生产链。"""

    context = await build_context(
        business.session,
        business.datasource_rows[datasource_id],
        TENANT_ID,
        WORKSPACE_ID,
        business.principal,
    )
    config = AlgoConfig(
        _env_file=None,
        datasource_id=datasource_id,
        datasource_type="sqlite",
        datasource_connection={},
        store_out_dir=str(tmp_path),
        store_excel_enabled=False,
        # 给一个与 DOMAIN 无关的合法 context 挂 store；本测试所有 DOMAIN
        # context 仅走内存，避免本地缓存替 datasource scope 提供额外隔离。
        store_include_contexts=("dataset_staged",),
        llm_api_key="",
    )
    contexts = AlgoContexts.build(config, offline=True, facts=context.facts)
    table_names = tuple(
        dict.fromkeys(table for tables in domain_tables.values() for table in tables)
    )
    await contexts.source_table.save(_source_snapshot(table_names))

    llm = _FakeLlm(_domain_response(domain_tables))
    profiler = AlgoProfiler.build(contexts, llm=llm)
    outcome = await profiler.run(ProfileTask.DOMAIN)
    assert outcome.executed, outcome.reason
    return _ProfileRun(context, contexts.domain_staged.current(), llm)


async def _confirm_and_complete(
    business: _BusinessDb,
    datasource_id: str,
    run: _ProfileRun,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """算法快照落 staging，按真实行服务确认，再调用公开 complete。"""

    task_id = f"DOMAIN-{datasource_id}"
    candidates = map_biz_domains(run.snapshot.to_frames())
    sink = DbProbeSink(
        business.session,
        run.context,
        datasource_id=datasource_id,
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        created_by=business.principal.user_id,
        task_id=task_id,
    )
    await sink.emit_biz_domains(candidates)

    staged = await staging_repo.list_all(
        business.session,
        "staging_biz_domain",
        datasource_id,
        TENANT_ID,
        WORKSPACE_ID,
    )
    current_rows = [row for row in staged if row["task_id"] == task_id]
    assert len(current_rows) == len(candidates)
    confirmed = await row_service.batch_confirm(
        business.session,
        def_by_stage(DOMAIN_STAGE),
        datasource_id,
        [row["id"] for row in current_rows],
        business.principal,
    )
    assert confirmed == {"confirmed": len(current_rows)}

    result = await promote.complete(
        business.session, datasource_id, DOMAIN_STAGE, business.principal
    )
    await business.session.commit()
    final_rows = await staging_repo.list_all(
        business.session,
        "staging_biz_domain",
        datasource_id,
        TENANT_ID,
        WORKSPACE_ID,
    )
    return result, final_rows


async def _seed_domains(business: _BusinessDb, *names: str) -> dict[str, int]:
    ids: dict[str, int] = {}
    for name in names:
        result = await business.session.execute(
            insert(domain).values(
                domain_name=name,
                description=f"canonical {name} domain",
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                created_by=business.principal.user_id,
            )
        )
        ids[name] = int(result.inserted_primary_key[0])
    await business.session.commit()
    return ids


async def test_three_datasources_pairwise_reuse_workspace_domains(
    business_db: _BusinessDb,
    tmp_path: Path,
) -> None:
    """A/B/C 依次建模；任意两源恰好共享一个 canonical Domain。"""

    matrix: dict[str, dict[str, tuple[str, ...]]] = {
        "source-a": {
            "sales": ("public.orders",),
            "finance": ("public.invoices",),
        },
        "source-b": {
            "sales": ("public.orders",),
            "support": ("public.tickets",),
        },
        "source-c": {
            "finance": ("public.orders",),
            "support": ("public.accounts",),
        },
    }

    runs: dict[str, _ProfileRun] = {}
    promoted: list[int] = []
    for datasource_id, domain_tables in matrix.items():
        run = await _profile_domains(
            business_db, tmp_path, datasource_id, domain_tables
        )
        runs[datasource_id] = run
        result, staged = await _confirm_and_complete(business_db, datasource_id, run)
        promoted.append(result["promoted"])
        assert {row["domain_name"] for row in staged} == set(domain_tables)

    modeled = {
        datasource_id: set(run.snapshot.domains["domain_name"])
        for datasource_id, run in runs.items()
    }
    assert all(len(names) == 2 for names in modeled.values())
    assert modeled["source-a"] & modeled["source-b"] == {"sales"}
    assert modeled["source-a"] & modeled["source-c"] == {"finance"}
    assert modeled["source-b"] & modeled["source-c"] == {"support"}
    assert set.intersection(*modeled.values()) == set()
    assert promoted == [2, 1, 0]
    assert _reusable_domain_names(runs["source-a"].llm) == set()
    assert _reusable_domain_names(runs["source-b"].llm) == {
        "sales",
        "finance",
    }
    assert _reusable_domain_names(runs["source-c"].llm) == {
        "sales",
        "finance",
        "support",
    }

    formal = (
        (
            await business_db.session.execute(
                select(domain.c.id, domain.c.domain_name).where(
                    domain.c.tenant_id == TENANT_ID,
                    domain.c.workspace_id == WORKSPACE_ID,
                    domain.c.is_deleted == 0,
                )
            )
        )
        .mappings()
        .all()
    )
    canonical = {row["domain_name"]: row["id"] for row in formal}
    assert set(canonical) == {"sales", "finance", "support"}
    assert len(formal) == 3

    # 每个 datasource 重建生产上下文时，读到的是同一份 workspace catalog
    # 及同一组正式 ID，而不是各自复制出的 Domain。
    for datasource_id in DATASOURCE_IDS:
        context = await build_context(
            business_db.session,
            business_db.datasource_rows[datasource_id],
            TENANT_ID,
            WORKSPACE_ID,
            business_db.principal,
        )
        visible = {
            row["domain_name"]: row["id"]
            for row in context.facts["domain"].to_dict(orient="records")
        }
        assert visible == canonical


async def test_same_public_orders_is_profiled_independently_per_datasource(
    business_db: _BusinessDb,
    tmp_path: Path,
) -> None:
    """三个源的相同表名可独立归域，table_name 无需拼 datasource 前缀。"""

    await _seed_domains(business_db, "sales", "finance", "support")
    expected_owner = {
        "source-a": "sales",
        "source-b": "sales",
        # 故意与 A/B 不同，更强地证明同名表没有共享全局 owner。
        "source-c": "finance",
    }

    for datasource_id, owner in expected_owner.items():
        run = await _profile_domains(
            business_db,
            tmp_path,
            datasource_id,
            {owner: ("public.orders",)},
        )
        # 运行前的 staging facts 只查当前源；A/B 的确认行不能泄漏进后续源。
        assert len(run.context.facts["staged_domain"]) == 0
        assert run.snapshot.members[["table_name", "domain_name"]].to_dict(
            orient="records"
        ) == [{"table_name": "public.orders", "domain_name": owner}]

        result, staged = await _confirm_and_complete(business_db, datasource_id, run)
        assert result["promoted"] == 0
        assert len(staged) == 1
        assert staged[0]["domain_name"] == owner
        assert _member_tables_of(staged[0]) == [
            {"source_table": "public.orders", "dataset_name": None}
        ]

        if datasource_id == "source-a":
            # 同一源重跑时，刚确认的 staging member 必须生效并锁住该表；
            # 后续 B/C 仍能产出，才证明隔离确实来自 datasource scope。
            rerun = await _profile_domains(
                business_db,
                tmp_path,
                datasource_id,
                {owner: ("public.orders",)},
            )
            assert rerun.snapshot.is_empty()
            assert rerun.llm.calls == []


async def test_confirmed_orders_only_locks_its_own_datasource(
    business_db: _BusinessDb,
    tmp_path: Path,
) -> None:
    """A 的正式 Dataset 归属锁住 A，但不能锁住 B/C 的同名物理表。"""

    domain_ids = await _seed_domains(business_db, "sales")
    await business_db.session.execute(
        insert(dataset).values(
            datasource_id="source-a",
            domain_id=domain_ids["sales"],
            dataset_name="public.orders",
            description="confirmed orders from source A",
            source="public.orders",
            tenant_id=TENANT_ID,
            workspace_id=WORKSPACE_ID,
            created_by=business_db.principal.user_id,
        ),
    )
    await business_db.session.commit()

    run_a = await _profile_domains(
        business_db,
        tmp_path,
        "source-a",
        {"sales": ("public.orders",)},
    )
    assert len(run_a.context.facts["dataset"]) == 1
    assert run_a.snapshot.is_empty()
    assert run_a.llm.calls == []

    for datasource_id in ("source-b", "source-c"):
        run = await _profile_domains(
            business_db,
            tmp_path,
            datasource_id,
            {"sales": ("public.orders",)},
        )
        assert len(run.context.facts["dataset"]) == 0
        assert set(run.context.facts["domain"]["domain_name"]) == {"sales"}
        assert run.snapshot.members[["table_name", "domain_name"]].to_dict(
            orient="records"
        ) == [{"table_name": "public.orders", "domain_name": "sales"}]
        assert len(run.llm.calls) == 1

        result, staged = await _confirm_and_complete(business_db, datasource_id, run)
        assert result["promoted"] == 0
        assert _member_tables_of(staged[0]) == [
            {"source_table": "public.orders", "dataset_name": None}
        ]

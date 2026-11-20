"""只替换物理输入及 LLM；Dataset Provider → staging → 审核 → promote 使用生产代码。"""

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pandas as pd
import pytest
from sqlalchemy import insert

from profiling.algo import AlgoConfig, AlgoContexts, ProfileTask
from profiling.algo.snapshots import QueryLogSnapshot, TableSnapshot
from profiling.algo_provider import AlgoProvider, map_biz_domains
from profiling.cm_adapter import RuntimeLlmClient
from profiling.repositories import probe_stage_repo, staging_repo
from profiling.services import orchestrator, row_service, stage_service
from profiling.stages import STAGE_DEFS
from semantic_config.errors import BadRequestError
from semantic_config.tables import dataset, dataset_field, domain


class DomainLlm:
    def __init__(self, *responses):
        self.responses = responses
        self.calls = []

    async def complete(self, system_prompt, user_prompt, *, temperature=0.2):
        self.calls.append((system_prompt, user_prompt))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


def reply(domain_name, *tables):
    return json.dumps({"domains": [{"domain": domain_name, "tables": list(tables)}]})


@pytest.fixture
def run_dataset(monkeypatch):
    """仅装配离线源元数据与假 LLM；保留真实 profiler/context、映射和 sink。"""
    build_contexts = AlgoContexts.build

    async def run(flow, source, llm, *, mode="create", joins=()):
        context = replace(await flow.context(source), mode=mode)
        flow.sequence += 1
        config = AlgoConfig(
            _env_file=None,
            datasource_id=source,
            datasource_type="sqlite",
            datasource_connection={},
            llm_api_key="",
            handler_mode="refine" if mode == "refine" else "create",
            store_out_dir=str(flow.tmp_path / f"assignment-{flow.sequence}"),
            store_excel_enabled=False,
            store_include_contexts=("dataset_staged",),
            dataset_derived_from_tables_include_patterns=("%",),
            dataset_parse_from_logs_enabled=False,
        )
        contexts = build_contexts(config, offline=True, facts=context.facts)
        source_tables = flow.tables[source]
        await contexts.source_table.save(
            TableSnapshot.build(
                pd.DataFrame(
                    [
                        {
                            "table_name": table.physical_name,
                            "table_comment": table.domain,
                        }
                        for table in source_tables
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
                    for table in source_tables
                },
            )
        )
        await contexts.query_log.save(
            QueryLogSnapshot.build(
                {
                    "join": pd.DataFrame(
                        [
                            {
                                "left_table": left,
                                "right_table": right,
                                "on_columns": "id",
                                "count": 5,
                            }
                            for left, right in joins
                        ]
                    )
                }
            )
        )
        monkeypatch.setattr(AlgoConfig, "build", lambda **kwargs: config)
        monkeypatch.setattr(AlgoContexts, "build", lambda *args, **kwargs: contexts)

        @asynccontextmanager
        async def open_llm(_config):
            yield llm

        monkeypatch.setattr(RuntimeLlmClient, "open", open_llm)
        await AlgoProvider().probe_datasets(context, await flow.sink(source))
        return contexts

    return run


async def seed_domain(flow, name="sales", **overrides):
    values = {
        **flow.scope,
        "domain_name": name,
        "description": f"正式 {name}",
        **overrides,
    }
    result = await flow.session.execute(insert(domain).values(**values))
    await flow.session.commit()
    return result.inserted_primary_key[0]


async def stage_domain(flow, source, name, *members, status="PENDING"):
    return await staging_repo.insert_row(
        flow.session,
        "staging_biz_domain",
        {
            "domain_name": name,
            "description": f"业务域 {name}",
            "member_tables": json.dumps([{"source_table": t} for t in members]),
        },
        datasource_id=source,
        task_id="domain-review",
        probe_status=status,
        **flow.scope,
    )


async def finish_dataset(flow, source):
    await flow.review(source, "DATASET")
    await flow.complete(source, "DATASET")
    await AlgoProvider().probe_datasets_fields(
        await flow.context(source), await flow.sink(source)
    )
    await flow.review(source, "DATASET_FIELDS")
    return await flow.complete(source, "DATASET_FIELDS")


async def test_reviewed_ai_domain_is_reused_without_another_assignment(
    business_flow,
    orders_table,
    run_dataset,
    dataset_field_tables,
):
    flow = business_flow
    await flow.add_source("a", [orders_table("a")])
    snapshot, _ = await flow.profile("a", ProfileTask.DOMAIN)
    await (await flow.sink("a")).emit_biz_domains(map_biz_domains(snapshot.to_frames()))
    await flow.review("a", "BIZ_DOMAIN")
    assert (await flow.complete("a", "BIZ_DOMAIN"))["promoted"] == 1
    before = await flow.rows("a", "BIZ_DOMAIN")
    formal_before = await flow.formal(domain)
    llm = DomainLlm(AssertionError("已有确认，不得再调用模型"))
    await run_dataset(flow, "a", llm)
    rows = await flow.rows("a", "DATASET")
    assert [(r["source"], r["domain_name"], r["probe_status"]) for r in rows] == [
        ("public.orders", "sales", "PENDING"),
    ]
    assert llm.calls == []
    await finish_dataset(flow, "a")
    assert (await flow.formal(dataset))[0]["domain_id"] == formal_before[0]["id"]
    assert await flow.rows("a", "BIZ_DOMAIN") == before
    [updated_domain] = await flow.formal(domain)
    assert updated_domain["datasource_scope"] == "a"
    assert updated_domain == {
        **formal_before[0],
        **{
            key: updated_domain[key]
            for key in ("datasource_scope", "updated_by", "updated_at")
        },
    }


async def test_dataset_review_cannot_repair_existing_formal_dataset_without_domain(
    business_flow,
    orders_table,
    run_dataset,
    dataset_field_tables,
):
    flow = business_flow
    await flow.add_source("b", [orders_table("b")])
    await seed_domain(flow)
    await flow.session.execute(
        insert(dataset).values(
            datasource_id="b",
            dataset_name="public.orders",
            source="public.orders",
            domain_id=None,
            description="已有正式描述",
            **flow.scope,
        )
    )
    await flow.session.commit()
    before = await flow.formal(dataset)
    # 算法可以生成候选归属；正式写入仍遵守 Dataset 必须已有 Domain 的规则。
    await run_dataset(flow, "b", DomainLlm(reply("sales", "public.orders")))
    with pytest.raises(BadRequestError, match="请先修复正式数据"):
        await finish_dataset(flow, "b")
    assert await flow.formal(dataset) == before
    assert await flow.formal(dataset_field) == []
    assert {r["probe_status"] for r in await flow.rows("b", "DATASET_FIELDS")} == {
        "CONFIRMED"
    }


async def test_three_sources_same_table_can_skip_domain_and_reuse_shared_id(
    business_flow,
    orders_table,
    run_dataset,
    dataset_field_tables,
):
    flow = business_flow
    for source in ("a", "b", "c"):
        await flow.add_source(source, [orders_table(source)])
    domain_id = await seed_domain(flow)
    original_domain = await flow.formal(domain)
    for source in ("a", "b", "c"):
        assert (
            await stage_service.skip(flow.session, source, "BIZ_DOMAIN", flow.principal)
        )["status"] == "SKIPPED"
        await orchestrator._check_prereq(
            flow.session, source, "DATASET", flow.principal
        )
        llm = DomainLlm(reply("sales", "public.orders"))
        contexts = await run_dataset(flow, source, llm)
        assert len(llm.calls) == 1  # A 的正式同名表不能锁住 B / C。
        assert "public.orders" in llm.calls[0][1]
        assert not await flow.rows(source, "BIZ_DOMAIN")
        assert contexts.domain_staged.current().is_empty()
        assert (await finish_dataset(flow, source))["promoted"] == 1
        stage = await probe_stage_repo.find_one(
            flow.session, source, "BIZ_DOMAIN", flow.tenant, flow.workspace
        )
        assert stage["status"] == "SKIPPED"  # 内部分配不是完成 Domain 阶段。
    formal = await flow.formal(dataset)
    assert {
        (r["datasource_id"], r["dataset_name"], r["domain_id"]) for r in formal
    } == {(source, "public.orders", domain_id) for source in ("a", "b", "c")}
    assert len({r["id"] for r in formal}) == 3
    assert {r["dataset_id"] for r in await flow.formal(dataset_field)} == {
        r["id"] for r in formal
    }
    [updated_domain] = await flow.formal(domain)
    assert updated_domain["datasource_scope"] == "a$$$b$$$c"
    assert updated_domain == {
        **original_domain[0],
        **{
            key: updated_domain[key]
            for key in ("datasource_scope", "updated_by", "updated_at")
        },
    }
    # 本源正式归属可以复用：重跑不再需要模型判断 Domain。
    llm = DomainLlm(AssertionError("本源已有正式归属"))
    await run_dataset(flow, "a", llm, mode="rerun")
    assert llm.calls == []
    assert (await finish_dataset(flow, "a"))["promoted"] == 0


@pytest.mark.parametrize("mode", ["create", "rerun", "refine"])
async def test_rejected_reusable_domain_can_be_proposed_again_in_dataset(
    business_flow,
    orders_table,
    run_dataset,
    dataset_field_tables,
    mode,
):
    flow = business_flow
    await flow.add_source("b", [orders_table("b")])
    domain_id = await seed_domain(flow)
    row_id = await stage_domain(flow, "b", "sales", "public.orders")
    await flow.review("b", "BIZ_DOMAIN", confirm=False, ids=[row_id])
    assert (await flow.complete("b", "BIZ_DOMAIN"))["promoted"] == 0
    rejected = await flow.rows("b", "BIZ_DOMAIN")
    llm = DomainLlm(reply("sales", "public.orders"))
    await run_dataset(flow, "b", llm, mode=mode)
    assert len(llm.calls) == 1
    assert [
        (r["domain_name"], r["probe_status"]) for r in await flow.rows("b", "DATASET")
    ] == [("sales", "PENDING")]
    await finish_dataset(flow, "b")
    assert (await flow.formal(dataset))[0]["domain_id"] == domain_id
    assert await flow.rows("b", "BIZ_DOMAIN") == rejected
    assert rejected[0]["probe_status"] == "SKIPPED"


async def test_rejected_new_domain_is_not_a_choice_but_its_tables_can_use_an_existing_domain(
    business_flow,
    orders_table,
    run_dataset,
):
    flow = business_flow
    await flow.add_source("b", [orders_table("b")])
    await seed_domain(flow)
    await stage_domain(flow, "b", "rejected_draft", "public.orders")
    await flow.review("b", "BIZ_DOMAIN", confirm=False)
    await flow.complete("b", "BIZ_DOMAIN")
    llm = DomainLlm(reply("sales", "public.orders"))
    await run_dataset(flow, "b", llm)
    assert "rejected_draft" not in llm.calls[0][1]
    assert (await flow.rows("b", "DATASET"))[0]["domain_name"] == "sales"
    assert [r["domain_name"] for r in await flow.formal(domain)] == ["sales"]


async def test_increment_preserves_confirmed_members_and_reassigns_rejected_and_new_tables(
    business_flow,
    orders_table,
    run_dataset,
):
    flow = business_flow
    names = ("public.old", "public.rejected", "public.new")
    await flow.add_source("b", [orders_table("b", physical=n) for n in names])
    await seed_domain(flow, "sales")
    await seed_domain(flow, "support")
    confirmed_id = await stage_domain(flow, "b", "sales", names[0])
    skipped_id = await stage_domain(flow, "b", "sales", names[1])
    await flow.review("b", "BIZ_DOMAIN", ids=[confirmed_id])
    await flow.review("b", "BIZ_DOMAIN", confirm=False, ids=[skipped_id])
    await flow.complete("b", "BIZ_DOMAIN")
    before = await flow.rows("b", "BIZ_DOMAIN")
    llm = DomainLlm(reply("support", names[1], names[2]))
    await run_dataset(
        flow, "b", llm, joins=((names[0], names[1]), (names[1], names[2]))
    )
    assert names[0] not in llm.calls[0][1]
    assert names[1] in llm.calls[0][1] and names[2] in llm.calls[0][1]
    owners = {r["source"]: r["domain_name"] for r in await flow.rows("b", "DATASET")}
    assert owners == {names[0]: "sales", names[1]: "support", names[2]: "support"}
    assert await flow.rows("b", "BIZ_DOMAIN") == before


async def test_catalog_is_formal_active_and_tenant_workspace_scoped(
    business_flow,
    orders_table,
    run_dataset,
):
    flow = business_flow
    await flow.add_source("b", [orders_table("b")])
    await seed_domain(flow)
    await seed_domain(flow, "foreign_tenant", tenant_id="other")
    await seed_domain(flow, "foreign_workspace", workspace_id="other")
    await seed_domain(flow, "deleted_domain", is_deleted=1)
    for status in ("PENDING", "CONFIRMED", "SKIPPED"):
        await stage_domain(flow, "b", f"staged_{status.lower()}", status=status)
    llm = DomainLlm(reply("sales", "public.orders"))
    await run_dataset(flow, "b", llm)
    prompt = llm.calls[0][1]
    for name in (
        "foreign_tenant",
        "foreign_workspace",
        "deleted_domain",
        "staged_pending",
        "staged_confirmed",
        "staged_skipped",
    ):
        assert name not in prompt
    assert "正式 sales" in prompt


@pytest.mark.parametrize(
    ("confirmed_count", "initial_count", "retry_count", "expected_calls", "passes"),
    [
        (9, 0, 0, 0, True),
        (6, 3, 3, 1, True),
        (6, 2, 3, 2, True),
        (0, 8, 9, 2, True),
        (8, 0, 0, 2, False),
        (6, 2, 2, 2, False),
        (6, 1, 1, 2, False),
    ],
)
async def test_total_coverage_must_exceed_eighty_percent_before_saving_datasets(
    business_flow,
    orders_table,
    run_dataset,
    confirmed_count,
    initial_count,
    retry_count,
    expected_calls,
    passes,
):
    flow = business_flow
    names = tuple(f"public.orders_{index}" for index in range(10))
    await flow.add_source("b", [orders_table("b", physical=name) for name in names])
    await seed_domain(flow)
    if confirmed_count:
        await stage_domain(
            flow, "b", "sales", *names[:confirmed_count], status="CONFIRMED"
        )
        await flow.complete("b", "BIZ_DOMAIN")
    else:
        await stage_service.skip(flow.session, "b", "BIZ_DOMAIN", flow.principal)
    domain_before = await flow.rows("b", "BIZ_DOMAIN")
    formal_before = await flow.formal(domain)
    if not passes:
        await staging_repo.insert_row(
            flow.session,
            "staging_dataset",
            {"domain_name": "sales", "dataset_name": "untouched", "source": names[0]},
            datasource_id="b",
            task_id="previous-task",
            probe_status="PENDING",
            **flow.scope,
        )
        await flow.session.commit()
    before = await flow.rows("b", "DATASET")

    def assigned(count):
        if not count:
            return json.dumps({"domains": []})
        return reply("sales", *names[confirmed_count : confirmed_count + count])

    llm = DomainLlm(
        assigned(initial_count)
        if expected_calls
        else AssertionError("总体超过 80%，无需补分配"),
        assigned(retry_count),
    )
    if passes:
        await run_dataset(flow, "b", llm)
        rows = await flow.rows("b", "DATASET")
        assert len(rows) == 9
        assert {
            (row["source"], row["domain_name"], row["probe_status"]) for row in rows
        } == {(name, "sales", "PENDING") for name in names[:9]}
    else:
        coverage = (confirmed_count + max(initial_count, retry_count)) / len(names)
        with pytest.raises(ValueError, match=f"总体覆盖率 {coverage:.1%} 未超过 80%"):
            await run_dataset(flow, "b", llm)
        assert await flow.rows("b", "DATASET") == before
    assert len(llm.calls) == expected_calls
    assert await flow.rows("b", "BIZ_DOMAIN") == domain_before
    assert await flow.formal(domain) == formal_before
    assert not await flow.formal(dataset)


@pytest.mark.parametrize(
    "failure", ["no_catalog", "new_domain", "incomplete", "invalid_json", "llm_outage"]
)
async def test_failed_assignment_does_not_write_partial_datasets_or_change_review(
    business_flow,
    orders_table,
    run_dataset,
    failure,
):
    flow = business_flow
    await flow.add_source(
        "b", [orders_table("b"), orders_table("b", physical="public.items")]
    )
    if failure != "no_catalog":
        await seed_domain(flow)
    await stage_service.skip(flow.session, "b", "BIZ_DOMAIN", flow.principal)
    await staging_repo.insert_row(
        flow.session,
        "staging_dataset",
        {
            "domain_name": "sales",
            "dataset_name": "untouched",
            "source": "public.orders",
        },
        datasource_id="b",
        task_id="previous-task",
        probe_status="PENDING",
        **flow.scope,
    )
    await flow.session.commit()
    before = await flow.rows("b", "DATASET")
    formal_before = await flow.formal(domain)
    bad = {
        "no_catalog": reply("invented", "public.orders", "public.items"),
        "new_domain": reply("invented", "public.orders", "public.items"),
        "incomplete": reply("sales", "public.orders"),
        "invalid_json": "{broken",
        "llm_outage": RuntimeError("mock LLM outage"),
    }[failure]
    llm = DomainLlm(bad)
    with pytest.raises((ValueError, RuntimeError)):
        await run_dataset(flow, "b", llm)
    assert await flow.rows("b", "DATASET") == before
    assert not await flow.rows("b", "BIZ_DOMAIN")
    assert await flow.formal(domain) == formal_before
    assert not await flow.formal(dataset)
    assert len(llm.calls) == (
        0 if failure == "no_catalog" else 1 if failure == "llm_outage" else 2
    )


async def test_dataset_rejection_does_not_create_formal_dataset_or_change_domain_review(
    business_flow,
    orders_table,
    run_dataset,
):
    flow = business_flow
    await flow.add_source("b", [orders_table("b")])
    await seed_domain(flow)
    await stage_service.skip(flow.session, "b", "BIZ_DOMAIN", flow.principal)
    await run_dataset(flow, "b", DomainLlm(reply("sales", "public.orders")))
    await flow.review("b", "DATASET", confirm=False)
    await flow.complete("b", "DATASET")
    await flow.complete("b", "DATASET_FIELDS")
    assert not await flow.formal(dataset)
    assert not await flow.rows("b", "BIZ_DOMAIN")


async def test_user_can_review_and_change_proposed_dataset_domain_before_promotion(
    business_flow,
    orders_table,
    run_dataset,
    dataset_field_tables,
):
    flow = business_flow
    await flow.add_source("b", [orders_table("b")])
    await seed_domain(flow, "sales")
    support_id = await seed_domain(flow, "support")
    await stage_service.skip(flow.session, "b", "BIZ_DOMAIN", flow.principal)
    await run_dataset(flow, "b", DomainLlm(reply("sales", "public.orders")))
    candidate = (await flow.rows("b", "DATASET"))[0]
    await row_service.update_row(
        flow.session,
        STAGE_DEFS["DATASET"],
        "b",
        candidate["id"],
        {"domain_name": "support"},
        flow.principal,
    )
    await finish_dataset(flow, "b")
    assert (await flow.formal(dataset))[0]["domain_id"] == support_id
    assert not await flow.rows("b", "BIZ_DOMAIN")


async def test_unreviewed_domain_stage_is_still_an_api_prerequisite(
    business_flow, orders_table
):
    flow = business_flow
    await flow.add_source("b", [orders_table("b")])
    await seed_domain(flow)
    with pytest.raises(BadRequestError):
        await orchestrator._check_prereq(flow.session, "b", "DATASET", flow.principal)
    pending_id = await stage_domain(flow, "b", "sales", "public.orders")
    await probe_stage_repo.update_status(
        flow.session,
        "b",
        "BIZ_DOMAIN",
        "REVIEWING",
        tenant_id=flow.tenant,
        workspace_id=flow.workspace,
        updated_by="reviewer",
    )
    with pytest.raises(BadRequestError):
        await orchestrator._check_prereq(flow.session, "b", "DATASET", flow.principal)
    assert (await flow.rows("b", "BIZ_DOMAIN"))[0]["id"] == pending_id

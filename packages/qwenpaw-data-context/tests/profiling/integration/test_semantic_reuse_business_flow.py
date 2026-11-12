"""Domain → Dataset → Caliber → Dict → complete 的跨源业务回归。

物理输入/LLM 为确定性 mock；事实查询、算法、DTO 映射、SQLite staging、
行审核与正式落库全部使用生产代码。每次阶段运行重新从数据库装配上下文。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
from profiling.algo import ProfileTask
from profiling.algo_provider import (
    map_biz_domains,
    map_dimension_calibers,
    map_dimensions,
    map_metric_calibers,
    map_metrics,
)
from profiling.services import orchestrator
from semantic_config.errors import BadRequestError
from semantic_config.tables import (
    domain as biz_domain,
    dataset,
    dataset_dimension,
    dataset_field,
    dimension_dict as dimension,
    metric_caliber,
    metric_dict as metric_lib,
)
from sqlalchemy import insert, update


async def _seed_entities(
    flow,
    *,
    dim="CustomerCountry",
    metric="OrderRevenue",
    dim_synonyms="客户国家$$$Buyer Country",
    metric_synonyms="销售收入$$$GMV",
):
    domain_id = (await flow.formal(biz_domain))[0]["id"]
    for table, fields in (
        (
            dimension,
            dict(
                dimension_name=dim, synonyms=dim_synonyms, dimension_type="categorical"
            ),
        ),
        (metric_lib, dict(metric_name=metric, synonyms=metric_synonyms)),
    ):
        await flow.session.execute(
            insert(table).values(
                domain_id=domain_id,
                description="只读业务目录描述",
                **fields,
                **flow.scope,
            )
        )
    await flow.session.commit()


def _names(snapshot):
    return (
        set(snapshot.calibers.dimension_name.dropna()),
        set(snapshot.calibers.metric_name.dropna()),
    )


@pytest.mark.parametrize("order", [("a", "b", "c"), ("b", "c", "a"), ("c", "a", "b")])
async def test_pairwise_domains_and_entities_reuse_through_all_stages(
    business_flow, orders_table, order
):
    """轮换首次建模者：三个域各只有一组实体，六个 Dataset 各有本地关系。"""
    flow = business_flow
    matrix = {
        "a": ("sales", "finance"),
        "b": ("sales", "support"),
        "c": ("finance", "support"),
    }
    for source, domains in matrix.items():
        await flow.add_source(
            source,
            [
                orders_table(
                    source,
                    domain=domain,
                    physical="public.orders" if index == 0 else "public.details",
                    dimension=f"{source}国家",
                    metric=f"{source}金额",
                    window="7d" if source == "c" else "1d",
                )
                for index, domain in enumerate(domains)
            ],
        )

    known_domains = set()
    original_entities = {}
    for source in order:
        domains, datasets = await flow.model_datasets(source)
        new_count = len(set(matrix[source]) - known_domains)
        assert domains["promoted"] == new_count
        assert datasets["promoted"] == 2
        assert datasets["promoted_dataset_field"] == 4
        context = await flow.context(source)
        assert context.facts["dimension_caliber"].empty
        assert context.facts["metric_caliber"].empty
        assert set(context.facts["dataset"].datasource_id) == {source}
        matches = {
            (domain, kind, f"{source}{local}"): canonical
            for domain in matrix[source]
            for kind, local, canonical in (
                ("维度", "国家", "国家"),
                ("指标", "金额", "金额"),
            )
        }
        snapshot, llm = await flow.profile(source, ProfileTask.CALIBER, matches=matches)
        assert len(snapshot.calibers) == 4
        assert _names(snapshot) == ({"国家"}, {"金额"})
        for payload in llm.catalog_payloads:
            assert payload["domain_name"] in matrix[source]
            # 目录严格按当前 Domain、实体类型分区，且不携带他源关系内容。
            expected = 1 if payload["domain_name"] in known_domains else 0
            assert len(payload["reusable_entities"]) == expected
            for item in payload["reusable_entities"]:
                assert set(item) == (
                    {"name", "description", "synonyms", "dimension_role"}
                    if payload["entity_kind"] == "维度"
                    else {"name", "description", "synonyms"}
                )

        dim_result, metric_result = await flow.finish_caliber(source, snapshot)
        assert (
            dim_result["promoted_dimension"]
            == metric_result["promoted_metric"]
            == new_count
        )
        assert (
            dim_result["promoted_dataset_dimension"]
            == metric_result["promoted_metric_caliber"]
            == 2
        )
        known_domains.update(matrix[source])
        for table in (biz_domain, dimension, metric_lib):
            for row in await flow.formal(table):
                key = (table.name, row["id"])
                # 新 Domain 会随确认来源追加 scope；复用仍须保留实体业务元信息。
                stable = {
                    k: v
                    for k, v in row.items()
                    if table is not biz_domain
                    or k not in {"datasource_scope", "updated_by", "updated_at"}
                }
                assert stable == original_entities.setdefault(key, stable)

        # 所有来源自己的 rerun 都锁住已确认关系；不能只测试首源。
        rerun, _ = await flow.profile(source, ProfileTask.CALIBER, matches=matches)
        assert rerun.is_empty()
        domain_rerun, _ = await flow.profile(source, ProfileTask.DOMAIN)
        assert domain_rerun.is_empty()

    assert len(await flow.formal(biz_domain)) == 3
    for table in (dimension, metric_lib):
        entities = await flow.formal(table)
        assert len(entities) == 3
        assert len({row["domain_id"] for row in entities}) == 3
    datasets = {row["id"]: row for row in await flow.formal(dataset)}
    domain_ids = {r["domain_name"]: r["id"] for r in await flow.formal(biz_domain)}
    for relation_table, entity_table, id_key in (
        (dataset_dimension, dimension, "dimension_id"),
        (metric_caliber, metric_lib, "metric_id"),
    ):
        relations = await flow.formal(relation_table)
        entities = {r["id"]: r for r in await flow.formal(entity_table)}
        assert len(relations) == 6
        assert len({r["dataset_id"] for r in relations}) == 6
        for relation in relations:
            dataset_row = datasets[relation["dataset_id"]]
            assert (
                relation["domain_id"]
                == dataset_row["domain_id"]
                == entities[relation[id_key]]["domain_id"]
            )
            source = dataset_row["datasource_id"]
            spec = next(
                t
                for t in flow.tables[source]
                if t.dataset_name == dataset_row["dataset_name"]
            )
            assert dataset_row["domain_id"] == domain_ids[spec.domain]
            if id_key == "dimension_id":
                assert relation["expr"] == "country"
                assert set(relation["value_example"].split("$$$")) == set(
                    spec.columns[0]["column_enums"]
                )
            else:
                assert (
                    relation["expr"],
                    relation["time_window"],
                    relation["additivity"],
                ) == (spec.roles[1]["expr"], spec.roles[1]["time_window"], "additive")


@pytest.mark.parametrize(
    "dim_name,metric_name,semantic",
    [
        ("CustomerCountry", "OrderRevenue", False),
        ("customercountry", "ORDERREVENUE", False),
        ("buyer country", "gmv", False),
        ("购方地区", "交易总额", True),
    ],
)
async def test_match_routes_preserve_canonical_ids_and_local_payload(
    business_flow, orders_table, dim_name, metric_name, semantic
):
    flow = business_flow
    await flow.add_source(
        "b", [orders_table("b", dimension=dim_name, metric=metric_name)]
    )
    await flow.model_datasets("b")
    await _seed_entities(flow)
    before = (await flow.formal(dimension), await flow.formal(metric_lib))
    snapshot, llm = await flow.profile(
        "b",
        ProfileTask.CALIBER,
        matches={
            (
                "sales",
                "维度",
                dim_name,
            ): "customercountry",  # 模型返回大小写变体也收口为正式原名
            ("sales", "指标", metric_name): "ORDERREVENUE",
        },
    )
    assert _names(snapshot) == ({"CustomerCountry"}, {"OrderRevenue"})
    assert len(llm.catalog_payloads) == (2 if semantic else 0)
    d, m = await flow.finish_caliber("b", snapshot)
    assert (d["promoted_dimension"], m["promoted_metric"]) == (0, 0)
    assert (d["promoted_dataset_dimension"], m["promoted_metric_caliber"]) == (1, 1)
    assert (await flow.formal(dimension), await flow.formal(metric_lib)) == before
    assert (await flow.formal(dataset_dimension))[0]["dimension_id"] == before[0][0][
        "id"
    ]
    assert (await flow.formal(metric_caliber))[0]["metric_id"] == before[1][0]["id"]


@pytest.mark.parametrize("failure", ["exception", "invalid_json", "incomplete"])
async def test_partial_matching_failure_keeps_exact_reuse_and_new_business_rows(
    business_flow, orders_table, failure
):
    flow = business_flow
    table = orders_table("b", dimension="CustomerCountry", metric="退款金额")
    await flow.add_source("b", [table])
    await flow.model_datasets("b")
    await _seed_entities(flow)
    snapshot, llm = await flow.profile("b", ProfileTask.CALIBER, match_failure=failure)
    assert len(snapshot.calibers) == 2
    assert _names(snapshot) == ({"CustomerCountry"}, {"退款金额"})
    assert {p["entity_kind"] for p in llm.catalog_payloads} == {"指标"}
    d, m = await flow.finish_caliber("b", snapshot)
    assert d["promoted_dimension"] == 0
    assert m["promoted_metric"] == 1
    assert (await flow.formal(metric_caliber))[0]["expr"] == table.roles[1]["expr"]


@pytest.mark.parametrize("status", ["PENDING", "SKIPPED"])
async def test_partial_review_is_local_and_unreviewed_caliber_stays_out_of_dict(
    business_flow, orders_table, status
):
    flow = business_flow
    for source in ("a", "b", "c"):
        await flow.add_source(source, [orders_table(source)])
        await flow.model_datasets(source)
    snapshot, _ = await flow.profile("a", ProfileTask.CALIBER)
    await flow.stage_caliber("a", snapshot)
    await flow.review("a", "DIMENSION_CALIBER")
    if status == "SKIPPED":
        await flow.review("a", "METRIC_CALIBER", confirm=False)
    original = await flow.rows("a", "DIMENSION_CALIBER")
    assert len(original) == 1
    assert not await flow.formal(
        dimension
    )  # 尚未 Manage：本源 pinned，不是跨源 Catalog。
    dictionary, _ = await flow.profile("a", ProfileTask.DICT)
    assert set(dictionary.dimensions.dimension_name) == {"客户国家"}
    assert dictionary.metrics.empty
    rerun, _ = await flow.profile("a", ProfileTask.CALIBER)
    assert _names(rerun) == (set(), {"销售收入"})
    for source in ("b", "c"):
        other, llm = await flow.profile(source, ProfileTask.CALIBER)
        assert _names(other) == ({"客户国家"}, {"销售收入"})
        assert all(
            p["reusable_entities"] == [] and p["local_pinned_names"] == []
            for p in llm.catalog_payloads
        )

    # 使用线上 rerun 清理函数，再从 DB 取事实，不手工删除候选模拟清理成功。
    for stage in ("DIMENSION_CALIBER", "METRIC_CALIBER"):
        await orchestrator._rerun_cleanup(
            flow.session,
            "a",
            stage,
            tenant_id=flow.tenant,
            workspace_id=flow.workspace,
            user_id="reviewer",
        )
    await flow.stage_caliber("a", rerun)
    assert await flow.rows("a", "DIMENSION_CALIBER") == original
    pending = await flow.rows("a", "METRIC_CALIBER")
    assert len(pending) == 1 and pending[0]["probe_status"] == "PENDING"


@pytest.mark.parametrize(
    "kind,changed",
    [
        ("dimension", "expr"),
        ("dimension", "value_example"),
        ("metric", "expr"),
        ("metric", "additivity"),
    ],
)
async def test_conflicts_block_all_writes_then_user_choice_recovers(
    business_flow, orders_table, kind, changed
):
    flow = business_flow
    await flow.add_source("a", [orders_table("a")])
    await flow.model_datasets("a")
    await _seed_entities(flow, dim="客户国家", metric="销售收入")
    snapshot, _ = await flow.profile("a", ProfileTask.CALIBER)
    if kind == "dimension":
        candidates = map_dimension_calibers(snapshot.to_frames())
        stage, manage, relation_table, count_key = (
            "DIMENSION_CALIBER",
            "DIMENSION_MANAGE",
            dataset_dimension,
            "promoted_dataset_dimension",
        )
        other = candidates[0].model_copy(
            update={
                changed: "UPPER(country)" if changed == "expr" else "英国$$$澳大利亚"
            }
        )
        emit = (await flow.sink("a")).emit_dimension_calibers
    else:
        candidates = map_metric_calibers(snapshot.to_frames())
        stage, manage, relation_table, count_key = (
            "METRIC_CALIBER",
            "METRIC_MANAGE",
            metric_caliber,
            "promoted_metric_caliber",
        )
        other = candidates[0].model_copy(
            update={
                changed: "SUM(amount * 0.8)" if changed == "expr" else "non_additive"
            }
        )
        emit = (await flow.sink("a")).emit_metric_calibers
    await emit([candidates[0], other])
    rows = await flow.rows("a", stage)
    assert len(rows) == 2 and {r["probe_status"] for r in rows} == {"PENDING"}
    await flow.review("a", stage)
    await flow.complete("a", stage)
    before = (await flow.formal(dimension), await flow.formal(metric_lib))
    with pytest.raises(BadRequestError, match="冲突"):
        await flow.complete("a", manage)
    # 异常后不先 rollback，直接断言没有写入，验证预校验在任何实体/关系写入之前。
    assert not await flow.formal(relation_table)
    assert (await flow.formal(dimension), await flow.formal(metric_lib)) == before
    await flow.review("a", stage, confirm=False, ids=[rows[1]["id"]])
    done = await flow.complete("a", manage)
    assert done[count_key] == 1
    relation = (await flow.formal(relation_table))[0]
    assert relation[changed] == getattr(candidates[0], changed)
    assert (await flow.complete("a", manage))[count_key] == 0


async def test_incremental_time_window_preserves_confirmed_relations(
    business_flow, orders_table
):
    flow = business_flow
    table = orders_table("a")
    await flow.add_source("a", [table])
    await flow.model_datasets("a")
    initial, _ = await flow.profile("a", ProfileTask.CALIBER)
    await flow.finish_caliber("a", initial)
    old = deepcopy(await flow.formal(metric_caliber))
    table.roles[1]["time_window"] = "7d"
    table.roles[1]["expr"] = "SUM(amount * 7)"
    snapshot, _ = await flow.profile("a", ProfileTask.CALIBER)
    assert len(snapshot.calibers) == 1
    assert set(snapshot.calibers.time_window) == {"7d"}
    d, m = await flow.finish_caliber("a", snapshot)
    assert (
        d["promoted_dimension"],
        d["promoted_dataset_dimension"],
        m["promoted_metric"],
        m["promoted_metric_caliber"],
    ) == (0, 0, 0, 1)
    persisted = await flow.formal(metric_caliber)
    assert len(persisted) == 2
    assert next(r for r in persisted if r["id"] == old[0]["id"]) == old[0]
    assert {r["time_window"] for r in persisted} == {"1d", "7d"}
    assert len({r["metric_id"] for r in persisted}) == 1
    rerun, _ = await flow.profile("a", ProfileTask.CALIBER)
    assert rerun.is_empty()


@pytest.mark.parametrize("boundary", ["workspace", "tenant"])
async def test_identical_entity_names_do_not_cross_scope(
    business_flow, orders_table, boundary
):
    first = business_flow
    second = replace(
        first,
        tables={},
        tmp_path=first.tmp_path / boundary,
        **{boundary: f"other-{boundary}"},
    )
    formal_ids = []
    # probe_stage 的全局主键包含 datasource_id；使用各源独立编码，实体名称完全相同。
    for flow, source in ((first, "a"), (second, "b")):
        await flow.add_source(source, [orders_table(source)])
        initial = await flow.context(source)
        assert all(
            initial.facts[key].empty
            for key in ("domain", "dimension", "metric", "staged_domain")
        )
        await flow.model_datasets(source)
        snapshot, llm = await flow.profile(source, ProfileTask.CALIBER)
        assert all(p["reusable_entities"] == [] for p in llm.catalog_payloads)
        d, m = await flow.finish_caliber(source, snapshot)
        assert d["promoted_dimension"] == m["promoted_metric"] == 1
        formal_ids.append(
            tuple(
                [
                    (await flow.formal(t))[0]["id"]
                    for t in (biz_domain, dimension, metric_lib)
                ]
            )
        )
    assert all(a != b for a, b in zip(*formal_ids))


async def test_deleted_entities_are_not_reused(business_flow, orders_table):
    flow = business_flow
    await flow.add_source(
        "b", [orders_table("b", dimension="CustomerCountry", metric="OrderRevenue")]
    )
    await flow.model_datasets("b")
    await _seed_entities(flow)
    deleted_ids = []
    for table in (dimension, metric_lib):
        old_id = (await flow.formal(table))[0]["id"]
        deleted_ids.append(old_id)
        await flow.session.execute(
            update(table).where(table.c.id == old_id).values(is_deleted=1)
        )
    await flow.session.commit()
    snapshot, llm = await flow.profile("b", ProfileTask.CALIBER)
    assert all(p["reusable_entities"] == [] for p in llm.catalog_payloads)
    d, m = await flow.finish_caliber("b", snapshot)
    assert d["promoted_dimension"] == m["promoted_metric"] == 1
    for table, old_id in zip((dimension, metric_lib), deleted_ids):
        assert (await flow.formal(table))[0]["id"] != old_id


async def test_multiple_local_datasets_share_one_entity_with_distinct_bindings(
    business_flow, orders_table
):
    flow = business_flow
    first = orders_table("a")
    archive = orders_table("a", physical="archive.orders", suffix="_archive")
    archive.roles[1]["expr"] = "SUM(amount * 0.5)"
    archive.columns[0]["column_enums"] = ["澳大利亚"]
    await flow.add_source("a", [first, archive])
    domains, datasets = await flow.model_datasets("a")
    assert domains["promoted"] == 1
    assert datasets["promoted"] == 2
    snapshot, _ = await flow.profile("a", ProfileTask.CALIBER)
    assert len(snapshot.calibers) == 4
    d, m = await flow.finish_caliber("a", snapshot)
    assert d["promoted_dimension"] == m["promoted_metric"] == 1
    assert d["promoted_dataset_dimension"] == m["promoted_metric_caliber"] == 2
    assert {r["expr"] for r in await flow.formal(metric_caliber)} == {
        "SUM(amount * 1)",
        "SUM(amount * 0.5)",
    }
    assert {r["value_example"] for r in await flow.formal(dataset_dimension)} == {
        "中国$$$日本",
        "澳大利亚",
    }


@pytest.mark.parametrize("failure", [None, "invalid_json"])
async def test_ambiguous_alias_requires_semantic_choice_or_preserves_provisional(
    business_flow, orders_table, failure
):
    flow = business_flow
    await flow.add_source(
        "b", [orders_table("b", dimension="国家", metric="OrderRevenue")]
    )
    await flow.model_datasets("b")
    await _seed_entities(flow, dim_synonyms="国家")
    await flow.session.execute(
        insert(dimension).values(
            domain_id=(await flow.formal(biz_domain))[0]["id"],
            dimension_name="ShipCountry",
            description="收货国家",
            synonyms="国家",
            dimension_type="categorical",
            **flow.scope,
        )
    )
    await flow.session.commit()
    snapshot, llm = await flow.profile(
        "b",
        ProfileTask.CALIBER,
        match_failure=failure,
        matches={("sales", "维度", "国家"): "ShipCountry"},
    )
    assert len(llm.catalog_payloads) == 1  # 不能因碰到第一条 synonym 就跳过语义判定。
    assert {e["name"] for e in llm.catalog_payloads[0]["reusable_entities"]} == {
        "CustomerCountry",
        "ShipCountry",
    }
    assert _names(snapshot) == (
        {"国家" if failure else "ShipCountry"},
        {"OrderRevenue"},
    )
    d, _ = await flow.finish_caliber("b", snapshot)
    assert d["promoted_dimension"] == (1 if failure else 0)


async def test_exact_name_wins_over_another_entity_synonym(business_flow, orders_table):
    flow = business_flow
    await flow.add_source("b", [orders_table("b", metric="GMV")])
    await flow.model_datasets("b")
    await _seed_entities(flow, dim="客户国家")  # OrderRevenue 同义词也有 GMV。
    result = await flow.session.execute(
        insert(metric_lib).values(
            domain_id=(await flow.formal(biz_domain))[0]["id"],
            metric_name="GMV",
            **flow.scope,
        )
    )
    exact_id = result.inserted_primary_key[0]
    await flow.session.commit()
    snapshot, llm = await flow.profile("b", ProfileTask.CALIBER)
    assert llm.catalog_payloads == []
    await flow.finish_caliber("b", snapshot)
    assert (await flow.formal(metric_caliber))[0]["metric_id"] == exact_id


async def test_date_dimension_reuses_time_entity_with_local_examples(
    business_flow, orders_table
):
    flow = business_flow
    table = orders_table("b", dimension="下单日期")
    table.columns[0].update(data_type="date", column_enums=["2026-09-01", "2026-09-02"])
    table.roles[0]["column_role"] = "date"
    await flow.add_source("b", [table])
    await flow.model_datasets("b")
    await _seed_entities(flow, dim="OrderDate", dim_synonyms="下单日期")
    await flow.session.execute(update(dimension).values(dimension_type="time"))
    await flow.session.commit()
    snapshot, _ = await flow.profile("b", ProfileTask.CALIBER)
    assert _names(snapshot)[0] == {"OrderDate"}
    assert (
        snapshot.calibers.set_index("column_name").loc["country", "column_role"]
        == "date"
    )
    d, _ = await flow.finish_caliber("b", snapshot)
    assert d["promoted_dimension"] == 0
    assert (await flow.formal(dimension))[0]["dimension_type"] == "time"
    assert (await flow.formal(dataset_dimension))[0][
        "value_example"
    ] == "2026-09-01$$$2026-09-02"


async def test_confirmed_domain_plus_pending_same_name_is_only_an_increment(
    business_flow, orders_table
):
    flow = business_flow
    await flow.add_source("a", [orders_table("a")])
    await flow.model_datasets("a")
    confirmed = await flow.rows("a", "BIZ_DOMAIN")
    assert len(confirmed) == 1
    # 本轮新物理表的算法输入。PENDING 成员不锁定，但可在 Dataset 内独立补分配。
    flow.tables["a"].append(
        orders_table("a", physical="public.refunds", suffix="_refunds")
    )
    snapshot, _ = await flow.profile("a", ProfileTask.DOMAIN)
    assert set(snapshot.members.table_name) == {"public.refunds"}
    assert set(snapshot.domains.domain_name) == {"sales"}
    await (await flow.sink("a")).emit_biz_domains(map_biz_domains(snapshot.to_frames()))
    rows = await flow.rows("a", "BIZ_DOMAIN")
    assert len(rows) == 2
    assert {r["probe_status"] for r in rows} == {"CONFIRMED", "PENDING"}
    assert next(r for r in rows if r["id"] == confirmed[0]["id"]) == confirmed[0]
    before_review, _ = await flow.profile("a", ProfileTask.DATASET)
    assert before_review.names() == {"public.orders", "public.refunds"}
    assert (
        await flow.rows("a", "BIZ_DOMAIN") == rows
    )  # 补分配不确认或重写 Domain 草稿。
    await flow.review("a", "BIZ_DOMAIN")
    assert (await flow.complete("a", "BIZ_DOMAIN"))["promoted"] == 0
    after_review, _ = await flow.profile("a", ProfileTask.DATASET)
    assert after_review.names() == {"public.orders", "public.refunds"}


async def test_two_sources_draft_before_either_promotes_still_share_formal_entities(
    business_flow, orders_table
):
    flow = business_flow
    for source in ("a", "b"):
        await flow.add_source(source, [orders_table(source)])
        await flow.model_datasets(source)
        snapshot, llm = await flow.profile(source, ProfileTask.CALIBER)
        assert all(p["reusable_entities"] == [] for p in llm.catalog_payloads)
        await flow.stage_caliber(source, snapshot)
        for stage in ("DIMENSION_CALIBER", "METRIC_CALIBER"):
            await flow.review(source, stage)
            await flow.complete(source, stage)
        dictionary, _ = await flow.profile(source, ProfileTask.DICT)
        sink = await flow.sink(source)
        await sink.emit_dimension(map_dimensions(dictionary.to_frames()))
        await sink.emit_metric(map_metrics(dictionary.to_frames()))
    assert not await flow.formal(dimension)
    assert not await flow.formal(metric_lib)
    for index, source in enumerate(("a", "b")):
        d = await flow.complete(source, "DIMENSION_MANAGE")
        m = await flow.complete(source, "METRIC_MANAGE")
        assert d["promoted_dimension"] == m["promoted_metric"] == 1 - index
        assert d["promoted_dataset_dimension"] == m["promoted_metric_caliber"] == 1
    assert len(await flow.formal(dimension)) == len(await flow.formal(metric_lib)) == 1
    assert (
        len(await flow.formal(dataset_dimension))
        == len(await flow.formal(metric_caliber))
        == 2
    )


async def test_two_columns_semantically_merge_to_conflicting_bindings_for_review(
    business_flow, orders_table
):
    flow = business_flow
    table = orders_table("b", dimension="账单国家")
    table.columns.append(
        {
            "column_name": "shipping_country",
            "column_comment": "收货所在国",
            "data_type": "varchar",
            "column_enums": ["美国"],
        }
    )
    table.roles.append(
        {
            "column_name": "shipping_country",
            "column_role": "dimension",
            "dimension_name": "收货国家",
        }
    )
    await flow.add_source("b", [table])
    await flow.model_datasets("b")
    await _seed_entities(flow, dim="客户国家", dim_synonyms=None)
    # 模拟模型把两个列映射到同一实体；算法必须保留不同 expr 供用户裁决。
    snapshot, _ = await flow.profile(
        "b",
        ProfileTask.CALIBER,
        matches={
            ("sales", "维度", "账单国家"): "客户国家",
            ("sales", "维度", "收货国家"): "客户国家",
        },
    )
    candidates = map_dimension_calibers(snapshot.to_frames())
    assert len(candidates) == 2
    assert {c.dimension_name for c in candidates} == {"客户国家"}
    assert {c.expr for c in candidates} == {"country", "shipping_country"}
    await (await flow.sink("b")).emit_dimension_calibers(candidates)
    await flow.review("b", "DIMENSION_CALIBER")
    with pytest.raises(BadRequestError, match="冲突"):
        await flow.complete("b", "DIMENSION_MANAGE")
    assert not await flow.formal(dataset_dimension)
    unwanted = [
        r["id"]
        for r in await flow.rows("b", "DIMENSION_CALIBER")
        if r["expr"] == "shipping_country"
    ]
    await flow.review("b", "DIMENSION_CALIBER", confirm=False, ids=unwanted)
    assert (await flow.complete("b", "DIMENSION_MANAGE"))[
        "promoted_dataset_dimension"
    ] == 1
    assert (await flow.formal(dataset_dimension))[0]["expr"] == "country"


@pytest.mark.parametrize(
    "domains",
    [("sales", "sales", "sales"), ("sales", "sales", "finance")],
    ids=["shared-domain", "different-domain"],
)
async def test_default_same_named_dataset_must_not_report_success_without_local_dataset(
    business_flow, orders_table, domains
):
    """三源同名 Dataset 和字段必须分别晋升，不能再静默跳过或拒绝跨源同名。"""
    flow = business_flow
    for source, domain_name in zip(("a", "b", "c"), domains):
        table = orders_table(source, domain=domain_name)
        table.dataset_name = "public.orders"
        await flow.add_source(source, [table])
    previous_datasets, previous_fields = {}, {}
    dataset_ids = set()
    for source, domain_name in zip(("a", "b", "c"), domains):
        _, result = await flow.model_datasets(source, review_names=False)
        assert result["status"] == "COMPLETED"
        assert result["promoted"] == 1
        assert result["promoted_dataset_field"] == 2
        facts = (await flow.context(source)).facts
        [local] = facts["dataset"].to_dict("records")
        assert local["datasource_id"] == source
        assert local["dataset_name"] == local["source"] == "public.orders"
        assert local["domain_name"] == domain_name
        assert local["id"] not in dataset_ids
        dataset_ids.add(local["id"])
        assert set(facts["dataset_fields"]["dataset_id"]) == {local["id"]}
        assert set(facts["dataset_fields"]["field_name"]) == {"country", "amount"}

        # 本源 complete 重试幂等，B/C 晋升不会改写 A 的 Dataset 或字段。
        retry = await flow.complete(source, "DATASET_FIELDS")
        assert retry["promoted"] == retry["promoted_dataset_field"] == 0
        datasets = {r["id"]: r for r in await flow.formal(dataset)}
        fields = {r["id"]: r for r in await flow.formal(dataset_field)}
        for row_id, row in previous_datasets.items():
            assert datasets[row_id] == row
        for row_id, row in previous_fields.items():
            assert fields[row_id] == row
        previous_datasets, previous_fields = datasets, fields
    assert len(previous_datasets) == 3 and len(previous_fields) == 6

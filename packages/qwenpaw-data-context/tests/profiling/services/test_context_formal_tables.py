"""正式事实读取迁移：只读新实体表，全局目录与本源数据集关系分别隔离。"""

import json

from sqlalchemy import insert

from profiling.algo.inputs.probe_facts_input import StaticProbeFactsInput
from profiling.repositories import staging_repo
from semantic_config.tables import (
    biz_domain,
    dataset,
    dataset_dimension,
    dataset_field,
    dataset_meta,
    dimension,
    dimension_dict,
    domain,
    metric_caliber,
    metric_dict,
    metric_lib,
)


async def _insert(session, table, scope, **values):
    result = await session.execute(insert(table).values(**{**scope, **values}))
    return int(result.inserted_primary_key[0])


async def test_new_global_catalog_is_available_without_local_datasets(business_flow):
    flow = business_flow
    await flow.add_source("b", [])

    # scope 中没有 B，仍须向 B 提供 workspace 全局目录用于语义复用。
    domain_id = await _insert(
        flow.session,
        domain,
        flow.scope,
        domain_name="sales",
        description="销售域",
        datasource_scope="a",
    )
    dimension_id = await _insert(
        flow.session,
        dimension_dict,
        flow.scope,
        domain_id=domain_id,
        dimension_name="country",
        dimension_type="categorical",
        synonyms="国家$$$地区",
        child_dimensions="province",
        priority="HIGH",
    )
    metric_id = await _insert(
        flow.session,
        metric_dict,
        flow.scope,
        domain_id=domain_id,
        metric_name="revenue",
        synonyms="收入",
        priority="LOW",
    )

    # 活跃旧表记录不能作为正式目录或本源 Dataset 的回退来源。
    old_domain_id = await _insert(
        flow.session,
        biz_domain,
        flow.scope,
        domain_name="legacy_sales",
        datasource_id="b",
    )
    await _insert(
        flow.session,
        dimension,
        flow.scope,
        domain_id=old_domain_id,
        dimension_name="legacy_country",
    )
    await _insert(
        flow.session,
        metric_lib,
        flow.scope,
        domain_id=old_domain_id,
        metric_name="legacy_revenue",
    )
    await _insert(
        flow.session,
        dataset_meta,
        flow.scope,
        domain_id=old_domain_id,
        source_datasource="b",
        dataset_name="public.orders",
        source="public.orders",
    )

    # 其他租户、其他 workspace、已删除记录均不进入目录。
    for excluded in (
        {"tenant_id": "other-tenant"},
        {"workspace_id": "other-workspace"},
        {"is_deleted": 1},
    ):
        scope = {**flow.scope, **excluded}
        excluded_domain_id = await _insert(
            flow.session,
            domain,
            scope,
            domain_name="sales",
        )
        await _insert(
            flow.session,
            dimension_dict,
            scope,
            domain_id=excluded_domain_id,
            dimension_name="country",
        )
        await _insert(
            flow.session,
            metric_dict,
            scope,
            domain_id=excluded_domain_id,
            metric_name="revenue",
        )
    await flow.session.commit()

    facts = (await flow.context("b")).facts
    assert facts["domain"]["id"].tolist() == [domain_id]
    assert facts["domain"]["domain_name"].tolist() == ["sales"]
    assert facts["domain"]["datasource_scope"].tolist() == ["a"]
    assert facts["dimension"]["id"].tolist() == [dimension_id]
    assert facts["dimension"]["domain_name"].tolist() == ["sales"]
    assert facts["dimension"]["dimension_type"].tolist() == ["categorical"]
    assert facts["dimension"]["child_dimensions"].tolist() == ["province"]
    assert facts["dimension"]["priority"].tolist() == ["HIGH"]
    assert facts["metric"]["id"].tolist() == [metric_id]
    assert facts["metric"]["domain_name"].tolist() == ["sales"]
    assert facts["metric"]["priority"].tolist() == ["LOW"]
    for key in ("dataset", "dataset_fields", "dimension_caliber", "metric_caliber"):
        assert facts[key].empty, key

    # 原 facts 接口与算法回读协议不变，无需重命名算法内部 dimension_role。
    inputs = StaticProbeFactsInput(facts)
    dictionary = await inputs.facts_of("columns_dict", "confirmed")()
    assert dictionary.dimensions["dimension_role"].tolist() == ["categorical"]
    domains = await inputs.facts_of("domain", "confirmed")()
    assert domains.members.empty
    assert domains.domains["domain_name"].tolist() == ["sales"]


async def test_same_named_new_datasets_keep_fields_and_relations_local(
    business_flow, orders_table
):
    flow = business_flow
    domain_id = await _insert(flow.session, domain, flow.scope, domain_name="sales")
    dimension_id = await _insert(
        flow.session,
        dimension_dict,
        flow.scope,
        domain_id=domain_id,
        dimension_name="country",
        dimension_type="categorical",
    )
    metric_id = await _insert(
        flow.session,
        metric_dict,
        flow.scope,
        domain_id=domain_id,
        metric_name="revenue",
    )
    ids = {}
    for source in ("a", "b", "c"):
        await flow.add_source(source, [orders_table(source)])
        dataset_id = await _insert(
            flow.session,
            dataset,
            flow.scope,
            datasource_id=source,
            domain_id=domain_id,
            dataset_name="public.orders",
            source="public.orders",
        )
        ids[source] = dataset_id
        await _insert(
            flow.session,
            dataset_field,
            flow.scope,
            dataset_id=dataset_id,
            domain_id=domain_id,
            field_name="country",
            data_type="varchar",
        )
        # 不填关系上的冗余 datasource_id，验证隔离来自父 Dataset。
        await _insert(
            flow.session,
            dataset_dimension,
            flow.scope,
            dataset_id=dataset_id,
            domain_id=domain_id,
            dimension_id=dimension_id,
            expr=f"country_{source}",
            value_example=f"example_{source}",
        )
        await _insert(
            flow.session,
            metric_caliber,
            flow.scope,
            dataset_id=dataset_id,
            domain_id=domain_id,
            metric_id=metric_id,
            expr=f"SUM(amount_{source})",
            time_window="1d",
            additivity="additive",
        )
        await staging_repo.insert_row(
            flow.session,
            "staging_biz_domain",
            {
                "domain_name": "sales",
                "member_tables": json.dumps(
                    [
                        {"source_table": "public.orders"},
                    ]
                ),
            },
            datasource_id=source,
            task_id=f"task-{source}",
            **flow.scope,
        )
    await flow.session.commit()

    for source, dataset_id in ids.items():
        facts = (await flow.context(source)).facts
        assert facts["dataset"]["id"].tolist() == [dataset_id]
        assert facts["dataset"]["dataset_name"].tolist() == ["public.orders"]
        assert facts["dataset"]["datasource_id"].tolist() == [source]
        assert "source_datasource" not in facts["dataset"].columns
        for key in ("dataset_fields", "dimension_caliber", "metric_caliber"):
            assert facts[key]["dataset_id"].tolist() == [dataset_id], key
            assert facts[key]["dataset_name"].tolist() == ["public.orders"], key
            assert facts[key]["domain_name"].tolist() == ["sales"], key
        samples = orders_table(source).columns[0]["column_enums"]
        assert facts["dataset_fields"]["sample_values"].tolist() == [
            "$$$".join(samples)
        ]
        assert facts["dimension_caliber"]["dimension_name"].tolist() == ["country"]
        assert facts["dimension_caliber"]["expr"].tolist() == [f"country_{source}"]
        assert facts["dimension_caliber"]["value_example"].tolist() == [
            f"example_{source}"
        ]
        assert facts["metric_caliber"]["metric_name"].tolist() == ["revenue"]
        assert facts["metric_caliber"]["expr"].tolist() == [f"SUM(amount_{source})"]
        assert facts["staged_domain"]["datasource_id"].tolist() == [source]
        assert facts["staged_domain"]["probe_status"].tolist() == ["PENDING"]
        assert facts["staged_domain"]["task_id"].tolist() == [f"task-{source}"]

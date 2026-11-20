"""新表晋升边界：直接构造 staging，验证 complete 与正式事实回读。

不经过仍待字段协议适配的算法/DTO；不代表新表全链路 E2E 已完成。
"""

import json

import pytest
from sqlalchemy import insert, select, update

from profiling.repositories import staging_repo
from profiling.services import promote
from profiling.stages import STAGE_DEFS
from profiling.tables import PROFILING_TABLES, probe_stage
from semantic_config.errors import BadRequestError
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


async def _insert(flow, table, **values):
    result = await flow.session.execute(
        insert(table).values(**{**flow.scope, **values})
    )
    return int(result.inserted_primary_key[0])


async def _stage(flow, table, *, datasource_id="b", status="CONFIRMED", **values):
    if table in {"staging_biz_domain", "staging_dataset_field"}:
        values.setdefault("description", "测试描述")
    return await staging_repo.insert_row(
        flow.session,
        table,
        values,
        datasource_id=datasource_id,
        task_id=None,
        probe_status=status,
        **flow.scope,
    )


async def _stage_dictionary(flow, *, source="b", domain_id=100):
    common = dict(datasource_id=source, domain_id=domain_id, domain_name="sales")
    await _stage(
        flow,
        "staging_dimension",
        **common,
        dimension_name="country",
        description="本轮维度描述",
        synonyms="国家",
        dimension_type="categorical",
    )
    await _stage(
        flow,
        "staging_metric",
        **common,
        metric_name="revenue",
        description="本轮指标描述",
        synonyms="收入",
    )


async def _stage_relations(flow, *, source="b", status="CONFIRMED", dataset_id=101):
    common = dict(
        datasource_id=source,
        status=status,
        domain_id=100,
        domain_name="sales",
        dataset_id=dataset_id,
        dataset_name="public.orders",
    )
    local_column = source if status == "CONFIRMED" else f"{source}_{status}"
    await _stage(
        flow,
        "staging_dataset_dimension",
        **common,
        dimension_name="country",
        expr=f"country_{local_column}",
        value_example=source,
    )
    await _stage(
        flow,
        "staging_metric_caliber",
        **common,
        metric_name="revenue",
        expr=f"SUM(amount_{local_column})",
        time_window="1d",
        additivity="additive",
    )


async def test_complete_creates_new_entities_and_only_references_new_ids(business_flow):
    flow = business_flow
    await flow.add_source("b", [])

    # 四张旧表故意放同名实体，ID 与新表错开；不得用于查重、引用或更新。
    await _insert(flow, biz_domain, id=100, domain_name="sales", datasource_id="b")
    await _insert(
        flow,
        dataset_meta,
        id=101,
        domain_id=100,
        source_datasource="b",
        dataset_name="public.orders",
        source="public.orders",
    )
    await _insert(flow, dimension, id=102, domain_id=100, dimension_name="country")
    await _insert(flow, metric_lib, id=103, domain_id=100, metric_name="revenue")
    legacy = {
        table.name: await flow.formal(table)
        for table in (biz_domain, dataset_meta, dimension, metric_lib)
    }
    await _stage(flow, "staging_biz_domain", domain_name="sales", description="销售")
    await _stage(flow, "staging_biz_domain", domain_name="skipped", status="SKIPPED")
    assert (await flow.complete("b", "BIZ_DOMAIN"))["promoted"] == 1
    [new_domain] = await flow.formal(domain)
    assert new_domain["id"] != 100
    assert new_domain["description"] == "销售"
    assert (
        new_domain["datasource_scope"] == ""
    )  # SC 新建尚无 Dataset 的域时初始化为空。

    await _stage(
        flow,
        "staging_dataset",
        domain_id=100,
        domain_name="sales",
        dataset_name="public.orders",
        source="public.orders",
        description="本源订单",
    )
    await _stage(
        flow,
        "staging_dataset_field",
        dataset_id=101,
        dataset_name="public.orders",
        field_name="country",
        data_type="varchar",
        is_nullable="Y",
        is_primary="N",
    )
    assert (await flow.complete("b", "DATASET"))["promoted"] == 0
    assert await flow.formal(dataset) == []
    result = await flow.complete("b", "DATASET_FIELDS")
    assert result["promoted"] == result["promoted_dataset_field"] == 1
    [new_dataset] = await flow.formal(dataset)
    [new_field] = await flow.formal(dataset_field)
    assert new_dataset["id"] != 101
    assert new_dataset["datasource_id"] == "b"
    assert new_dataset["domain_id"] == new_field["domain_id"] == new_domain["id"]
    assert new_field["dataset_id"] == new_dataset["id"]
    assert new_field["datasource_id"] == "b"
    assert (await flow.formal(domain))[0]["datasource_scope"] == "b"

    await _stage_dictionary(flow)
    await _stage_relations(flow)
    for stage in ("DIMENSION_CALIBER", "METRIC_CALIBER"):
        assert (await flow.complete("b", stage))["promoted"] == 0
    dim_result = await flow.complete("b", "DIMENSION_MANAGE")
    metric_result = await flow.complete("b", "METRIC_MANAGE")
    assert (
        dim_result["promoted_dimension"]
        == dim_result["promoted_dataset_dimension"]
        == 1
    )
    assert (
        metric_result["promoted_metric"]
        == metric_result["promoted_metric_caliber"]
        == 1
    )
    [new_dim] = await flow.formal(dimension_dict)
    [new_metric] = await flow.formal(metric_dict)
    [binding] = await flow.formal(dataset_dimension)
    [caliber] = await flow.formal(metric_caliber)
    assert new_dim["id"] != 102 and new_metric["id"] != 103
    assert new_dim["dimension_type"] == "categorical"
    assert new_dim["priority"] == new_metric["priority"] == "MEDIUM"
    assert binding["dimension_id"] == new_dim["id"]
    assert caliber["metric_id"] == new_metric["id"]
    assert binding["dataset_id"] == caliber["dataset_id"] == new_dataset["id"]
    assert binding["datasource_id"] == caliber["datasource_id"] == "b"
    for row in (new_dataset, new_field, new_dim, new_metric, binding, caliber):
        assert row["domain_id"] == new_domain["id"]
    for row in (new_domain, new_dataset, new_dim, new_metric):
        assert row["tenant_id"] == flow.tenant and row["workspace_id"] == flow.workspace
        assert row["created_by"] == row["updated_by"] == "reviewer"
        assert row["created_at"] and row["updated_at"] and row["is_deleted"] == 0
    for table in (biz_domain, dataset_meta, dimension, metric_lib):
        assert await flow.formal(table) == legacy[table.name]

    # 第一步的读取路径能直接消费第二步的写入结果。
    facts = (await flow.context("b")).facts
    assert facts["dataset"]["id"].tolist() == [new_dataset["id"]]
    assert facts["dataset_fields"]["dataset_id"].tolist() == [new_dataset["id"]]
    assert facts["dimension_caliber"]["dimension_name"].tolist() == ["country"]
    assert facts["metric_caliber"]["metric_name"].tolist() == ["revenue"]

    # 正式服务按数据源过滤、展示口径以及图同步装配均能消费本次晋升结果。
    from semantic_config.services import (
        dataset_dimension_service,
        dimension_service,
        metric_caliber_service,
        metric_lib_service,
        weave_assembler,
    )

    dims = await dimension_service.list_page(
        flow.session, None, None, 1, 20, flow.principal, datasource_id="b"
    )
    metrics = await metric_lib_service.list_page(
        flow.session, None, None, 1, 20, flow.principal, datasource_id="b"
    )
    assert [r.dimension_name for r in dims.records] == ["country"]
    assert [r.metric_name for r in metrics.records] == ["revenue"]
    [binding_response] = await dataset_dimension_service.list_by_dataset(
        flow.session, new_dataset["id"], flow.principal
    )
    calibers = await metric_caliber_service.list_page(
        flow.session, None, new_dataset["id"], 1, 20, flow.principal
    )
    assert (
        binding_response.datasource_name == calibers.records[0].datasource_name == "b"
    )
    payload = await weave_assembler.assemble(
        flow.session, "b", "promote-test", flow.tenant, flow.workspace
    )
    [sales] = payload["semantic"]["domains"]
    assert sales["name"] == "sales"
    assert [r["name"] for r in sales["datasets"]] == ["public.orders"]
    assert [r["name"] for r in sales["dimensions"]] == ["country"]
    assert [r["name"] for r in sales["metrics"]] == ["revenue"]


async def test_reuse_new_catalog_keeps_three_sources_relations_independent(
    business_flow,
):
    flow = business_flow
    domain_id = await _insert(flow, domain, domain_name="sales", description="正式域")
    dim_id = await _insert(
        flow,
        dimension_dict,
        domain_id=domain_id,
        dimension_name="country",
        dimension_type="categorical",
        description="正式维度",
        synonyms="国家",
        priority="HIGH",
    )
    metric_id = await _insert(
        flow,
        metric_dict,
        domain_id=domain_id,
        metric_name="revenue",
        description="正式指标",
        synonyms="收入",
        priority="LOW",
    )
    original_catalog = {
        table.name: await flow.formal(table)
        for table in (domain, dimension_dict, metric_dict)
    }
    dataset_ids = {}
    for source in ("a", "b", "c"):
        await flow.add_source(source, [])
        # 直接准备新表 Dataset，专注验证共享实体的本源关系解析。
        dataset_ids[source] = await _insert(
            flow,
            dataset,
            datasource_id=source,
            domain_id=domain_id,
            dataset_name="public.orders",
            source="public.orders",
        )
        await _stage(
            flow, "staging_biz_domain", datasource_id=source, domain_name="sales"
        )
        assert (await flow.complete(source, "BIZ_DOMAIN"))["promoted"] == 0
        await _stage_dictionary(flow, source=source)
        # B/C 刻意携带 A 的 Dataset ID，必须回解到各自当前源的新 Dataset。
        await _stage_relations(flow, source=source, dataset_id=dataset_ids["a"])
        for status in ("PENDING", "SKIPPED"):
            await _stage_relations(flow, source=source, status=status)
        for attempt in (0, 1):
            dim_result = await flow.complete(source, "DIMENSION_MANAGE")
            metric_result = await flow.complete(source, "METRIC_MANAGE")
            assert (
                dim_result["promoted_dimension"]
                == metric_result["promoted_metric"]
                == 0
            )
            assert dim_result["promoted_dataset_dimension"] == 1 - attempt
            assert metric_result["promoted_metric_caliber"] == 1 - attempt

    bindings = {row["dataset_id"]: row for row in await flow.formal(dataset_dimension)}
    calibers = {row["dataset_id"]: row for row in await flow.formal(metric_caliber)}
    assert len(bindings) == len(calibers) == 3
    for source, dataset_id in dataset_ids.items():
        assert bindings[dataset_id]["dimension_id"] == dim_id
        assert bindings[dataset_id]["expr"] == f"country_{source}"
        assert bindings[dataset_id]["value_example"] == source
        assert calibers[dataset_id]["metric_id"] == metric_id
        assert calibers[dataset_id]["expr"] == f"SUM(amount_{source})"
    for table in (domain, dimension_dict, metric_dict):
        assert await flow.formal(table) == original_catalog[table.name]


@pytest.mark.parametrize(
    "stage", ["DATASET_FIELDS", "DIMENSION_MANAGE", "METRIC_MANAGE"]
)
async def test_old_domain_id_cannot_substitute_for_missing_new_domain(
    business_flow, stage
):
    flow = business_flow
    await flow.add_source("b", [])
    await _insert(flow, biz_domain, id=100, domain_name="sales")
    await _stage(
        flow,
        "staging_dataset",
        domain_id=100,
        domain_name="sales",
        dataset_name="orders",
    )
    await _stage_dictionary(flow)
    with pytest.raises(BadRequestError, match="sales.*不存在或尚未晋升"):
        await promote.complete(flow.session, "b", stage, flow.principal)
    for table in (dataset, dataset_field, dimension_dict, metric_dict):
        assert await flow.formal(table) == []


@pytest.mark.parametrize("kind", ["dimension", "metric"])
async def test_conflict_still_fails_before_new_dictionary_write(business_flow, kind):
    flow = business_flow
    await flow.add_source("b", [])
    domain_id = await _insert(flow, domain, domain_name="sales")
    await _insert(
        flow,
        dataset,
        datasource_id="b",
        domain_id=domain_id,
        dataset_name="public.orders",
        source="public.orders",
    )
    await _stage_dictionary(flow)
    await _stage_relations(flow)
    table, name_values = (
        (
            "staging_dataset_dimension",
            {"dimension_name": "country", "value_example": "other"},
        )
        if kind == "dimension"
        else ("staging_metric_caliber", {"metric_name": "revenue", "time_window": "1d"})
    )
    await _stage(
        flow,
        table,
        domain_name="sales",
        dataset_name="public.orders",
        expr="conflicting expression",
        **name_values,
    )
    with pytest.raises(BadRequestError, match="冲突"):
        await promote.complete(
            flow.session, "b", f"{kind.upper()}_MANAGE", flow.principal
        )
    for table in (dimension_dict, metric_dict, dataset_dimension, metric_caliber):
        assert await flow.formal(table) == []


@pytest.mark.parametrize("prior", ["formal", "staged"])
async def test_dataset_domain_conflict_fails_before_any_write(business_flow, prior):
    flow = business_flow
    await flow.add_source("b", [])
    sales_id = await _insert(flow, domain, domain_name="sales")
    await _insert(flow, domain, domain_name="finance")

    # 将合法的新 Dataset 排在冲突前面，确保不是写完一半才发现归属冲突。
    await _stage(
        flow,
        "staging_dataset",
        domain_name="sales",
        dataset_name="public.valid",
        source="public.valid",
    )
    if prior == "formal":
        await _insert(
            flow,
            dataset,
            datasource_id="b",
            domain_id=sales_id,
            dataset_name="public.orders",
            source="public.orders",
        )
    else:
        await _stage(
            flow,
            "staging_dataset",
            domain_name="sales",
            dataset_name="public.orders",
            source="public.orders",
        )
    await _stage(
        flow,
        "staging_dataset",
        domain_name="finance",
        dataset_name="public.orders",
        source="public.orders",
    )
    await _stage(
        flow,
        "staging_dataset_field",
        dataset_name="public.valid",
        field_name="amount",
    )
    original = await flow.formal(dataset)
    with pytest.raises(BadRequestError, match="b.*public.orders.*业务域归属冲突"):
        await promote.complete(flow.session, "b", "DATASET_FIELDS", flow.principal)
    assert await flow.formal(dataset) == original
    assert await flow.formal(dataset_field) == []


async def test_same_source_dataset_duplicates_in_one_domain_are_idempotent(
    business_flow,
):
    flow = business_flow
    await flow.add_source("b", [])
    domain_id = await _insert(flow, domain, domain_name="sales")
    for _ in range(2):
        await _stage(
            flow,
            "staging_dataset",
            domain_name="sales",
            dataset_name="public.orders",
            source="public.orders",
        )
        await _stage(
            flow,
            "staging_dataset_field",
            dataset_name="public.orders",
            field_name="amount",
        )
    result = await flow.complete("b", "DATASET_FIELDS")
    assert result["promoted"] == result["promoted_dataset_field"] == 1
    [new_dataset] = await flow.formal(dataset)
    [new_field] = await flow.formal(dataset_field)
    assert new_dataset["domain_id"] == domain_id
    assert new_field["dataset_id"] == new_dataset["id"]


@pytest.mark.parametrize("existing_domain", [False, True])
async def test_domain_members_reject_formal_dataset_without_domain_before_writes(
    business_flow,
    monkeypatch,
    existing_domain,
):
    flow = business_flow
    await flow.add_source("b", [])
    if existing_domain:
        await _insert(flow, domain, domain_name="Sales")
    await _insert(
        flow, dataset, datasource_id="b", dataset_name="orders", source="public.orders"
    )
    await _stage(
        flow,
        "staging_biz_domain",
        domain_name="Sales",
        member_tables=_members("public.orders"),
    )
    message = await _reject_before_writes(flow, "BIZ_DOMAIN", monkeypatch)
    assert "domain_id 为空" in message and "请先修复正式数据" in message


@pytest.mark.parametrize("kind", ["dataset", "field", "binding", "caliber"])
async def test_unassigned_formal_dataset_cannot_be_reused_or_receive_children(
    business_flow,
    monkeypatch,
    kind,
):
    flow = business_flow
    values = await _case(flow, kind)
    if kind == "dataset":
        await _insert(
            flow,
            dataset,
            datasource_id="b",
            dataset_name="public.orders",
            source="public.orders",
        )
    else:
        await flow.session.execute(update(dataset).values(domain_id=None))
    await _candidate(flow, kind, values)
    # 同批还包含可新增的字典/字段时，也必须在任何正式写入前拒绝。
    message = await _reject_before_writes(flow, _CASES[kind][0], monkeypatch)
    assert "domain_id 为空" in message and "请先修复正式数据" in message


@pytest.mark.parametrize("status", ["PENDING", "SKIPPED"])
async def test_unreviewed_dataset_candidates_do_not_repair_formal_data(
    business_flow, status
):
    flow = business_flow
    await flow.add_source("b", [])
    await _insert(flow, domain, domain_name="Sales")
    await _insert(
        flow, dataset, datasource_id="b", dataset_name="orders", source="public.orders"
    )
    await _stage(
        flow,
        "staging_dataset",
        status=status,
        domain_name="Sales",
        dataset_name="orders",
        source="public.orders",
    )
    before = await _state(flow)
    await flow.complete("b", "DATASET_FIELDS")
    assert await flow.formal(dataset) == before[dataset.name]
    assert await flow.formal(domain) == before[domain.name]


# 七类对象的新增、同批去重/冲突、正式复用共用同一套真实表夹具。
_CASES = {
    "domain": ("BIZ_DOMAIN", "BIZ_DOMAIN", domain, "promoted", "domain_name"),
    "dataset": ("DATASET_FIELDS", "DATASET", dataset, "promoted", "dataset_name"),
    "field": (
        "DATASET_FIELDS",
        "DATASET_FIELDS",
        dataset_field,
        "promoted_dataset_field",
        "field_name",
    ),
    "dimension": (
        "DIMENSION_MANAGE",
        "DIMENSION_MANAGE",
        dimension_dict,
        "promoted_dimension",
        "dimension_name",
    ),
    "metric": (
        "METRIC_MANAGE",
        "METRIC_MANAGE",
        metric_dict,
        "promoted_metric",
        "metric_name",
    ),
    "binding": (
        "DIMENSION_MANAGE",
        "DIMENSION_CALIBER",
        dataset_dimension,
        "promoted_dataset_dimension",
        "dimension_name",
    ),
    "caliber": (
        "METRIC_MANAGE",
        "METRIC_CALIBER",
        metric_caliber,
        "promoted_metric_caliber",
        "metric_name",
    ),
}
_FORMAL_TABLES = (
    domain,
    dataset,
    dataset_field,
    dimension_dict,
    metric_dict,
    dataset_dimension,
    metric_caliber,
)


async def _case(flow, kind):
    await flow.add_source("b", [])
    if kind == "domain":
        return dict(domain_name="Sales", description="销售")
    domain_id = await _insert(flow, domain, domain_name="Sales")
    common = dict(domain_name="Sales", domain_id=domain_id)
    if kind == "dataset":
        return dict(
            **common,
            dataset_name="public.orders",
            source="public.orders",
            description="订单",
        )
    dataset_id = await _insert(
        flow,
        dataset,
        domain_id=domain_id,
        datasource_id="b",
        dataset_name="public.orders",
        source="public.orders",
    )
    if kind == "field":
        return dict(
            dataset_name="public.orders",
            dataset_id=dataset_id,
            field_name="amount",
            description="金额",
            data_type="decimal",
            is_primary="N",
            is_nullable="Y",
        )
    if kind == "dimension":
        return dict(
            **common,
            dimension_name="Country",
            description="国家",
            synonyms="地区",
            dimension_type="categorical",
            child_dimensions="Province",
            priority="HIGH",
        )
    if kind == "metric":
        return dict(
            **common,
            metric_name="Revenue",
            description="收入",
            synonyms="销售额",
            priority="LOW",
        )
    if kind == "binding":
        await _insert(
            flow,
            dimension_dict,
            domain_id=domain_id,
            dimension_name="Country",
            dimension_type="categorical",
        )
        return dict(
            **common,
            dataset_id=dataset_id,
            dataset_name="public.orders",
            dimension_name="Country",
            expr="country_code",
            value_example="CN$$$US",
        )
    await _insert(flow, metric_dict, domain_id=domain_id, metric_name="Revenue")
    return dict(
        **common,
        dataset_id=dataset_id,
        dataset_name="public.orders",
        metric_name="Revenue",
        expr="SUM(amount)",
        time_window="1d",
        additivity="additive",
    )


async def _candidate(flow, kind, values, *, status="CONFIRMED", source="b"):
    return await _stage(
        flow,
        STAGE_DEFS[_CASES[kind][1]].table,
        datasource_id=source,
        status=status,
        **values,
    )


async def _state(flow):
    tables = (
        *_FORMAL_TABLES,
        probe_stage,
        *(PROFILING_TABLES[sdef.table] for sdef in STAGE_DEFS.values()),
    )
    return {
        table.name: [
            dict(r)
            for r in (
                await flow.session.execute(select(table).order_by(table.c.id))
            ).mappings()
        ]
        for table in tables
    }


async def _reject_before_writes(flow, stage, monkeypatch, *, source="b"):
    before = await _state(flow)
    writes = []
    execute = flow.session.execute

    async def spy(statement, *args, **kwargs):
        if (
            getattr(statement, "is_insert", False)
            or getattr(statement, "is_update", False)
        ) and statement.table.name in {table.name for table in _FORMAL_TABLES}:
            writes.append(statement)
        return await execute(statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(flow.session, "execute", spy)
        with pytest.raises(BadRequestError) as exc:
            await promote.complete(flow.session, source, stage, flow.principal)
    assert writes == [], (
        "冲突必须早于首次正式 INSERT/UPDATE，而不是依赖 rollback 隐藏部分写入"
    )
    assert await _state(flow) == before
    return str(exc.value)


@pytest.mark.parametrize("kind", _CASES)
@pytest.mark.parametrize("origin", ["create", "refine"])
async def test_all_objects_deduplicate_new_and_reuse_formal_without_updates(
    business_flow, kind, origin
):
    flow = business_flow
    values = await _case(flow, kind)
    stage, _, formal_table, counter, name_key = _CASES[kind]
    # 管理阶段的 PENDING 产物不要求逐行确认；其余候选经真实 review 确认。
    first = await _candidate(flow, kind, dict(values, origin=origin), status="PENDING")
    second = await _candidate(
        flow, kind, dict(values, origin="create"), status="PENDING"
    )
    if kind not in ("dimension", "metric"):
        await flow.review("b", _CASES[kind][1], ids=[first, second])
    result = await flow.complete("b", stage)
    assert result[counter] == 1
    original = await flow.formal(formal_table)
    assert len(original) == 1
    if name_key in original[0]:
        assert original[0][name_key] == values[name_key]  # 不 lowercase canonical name
    assert (await flow.complete("b", stage))[counter] == 0
    assert await flow.formal(formal_table) == original

    # 命中正式项后，描述/别名等非冲突属性不覆盖历史定义。
    metadata = dict(values)
    if kind in ("domain", "dataset", "field", "dimension", "metric"):
        metadata["description"] = "新的描述不得覆盖正式项"
    if kind in ("dimension", "metric"):
        metadata.update(synonyms="新别名", priority="MEDIUM")
    if kind == "dimension":
        metadata["child_dimensions"] = "City"
    await _candidate(flow, kind, dict(metadata, origin="refine"))
    assert (await flow.complete("b", stage))[counter] == 0
    assert await flow.formal(formal_table) == original


_NEW_CONFLICTS = [
    ("domain", "description", "另一描述"),
    ("dataset", "source", "public.other_orders"),
    ("dataset", "description", "另一描述"),
    ("field", "data_type", "varchar"),
    ("field", "is_primary", "Y"),
    ("field", "is_nullable", "N"),
    ("field", "description", "另一描述"),
    ("dimension", "dimension_type", "time"),
    ("dimension", "description", "另一描述"),
    ("dimension", "synonyms", "不同别名"),
    ("dimension", "priority", "LOW"),
    ("dimension", "child_dimensions", "City"),
    ("metric", "description", "另一描述"),
    ("metric", "synonyms", "不同别名"),
    ("metric", "priority", "HIGH"),
    ("binding", "expr", "country_name"),
    ("binding", "value_example", "FR"),
    ("caliber", "expr", "SUM(amount * 100)"),
    ("caliber", "additivity", "non_additive"),
]


@pytest.mark.parametrize("kind,attribute,value", _NEW_CONFLICTS)
@pytest.mark.parametrize("origin", ["create", "refine"])
async def test_conflicting_new_definitions_reject_whole_batch(
    business_flow, monkeypatch, kind, attribute, value, origin
):
    flow = business_flow
    values = await _case(flow, kind)
    first = await _candidate(flow, kind, values)
    second = await _candidate(
        flow, kind, dict(values, **{attribute: value}, origin=origin)
    )
    message = await _reject_before_writes(flow, _CASES[kind][0], monkeypatch)
    assert all(
        part in message
        for part in (
            "冲突",
            attribute,
            f"staging id={first}",
            f"staging id={second}",
            "跳过",
            values[_CASES[kind][4]],
        )
    )


_FORMAL_CONFLICTS = [
    case
    for case in _NEW_CONFLICTS
    if case[1] not in {"description", "synonyms", "priority", "child_dimensions"}
]


@pytest.mark.parametrize("kind,attribute,value", _FORMAL_CONFLICTS)
async def test_formal_definition_conflict_is_not_silently_reused(
    business_flow, monkeypatch, kind, attribute, value
):
    flow = business_flow
    values = await _case(flow, kind)
    await _candidate(flow, kind, values)
    stage, _, table, _, name_key = _CASES[kind]
    await flow.complete("b", stage)
    [formal] = await flow.formal(table)
    conflicting_id = await _candidate(
        flow, kind, dict(values, **{attribute: value}, origin="refine")
    )
    message = await _reject_before_writes(flow, stage, monkeypatch)
    assert all(
        part in message
        for part in (
            attribute,
            f"正式 id={formal['id']}",
            f"staging id={conflicting_id}",
            values[name_key],
        )
    )


@pytest.mark.parametrize("kind", _CASES)
async def test_deleted_and_skipped_candidates_do_not_promote(business_flow, kind):
    flow = business_flow
    values = await _case(flow, kind)
    stage, review_stage, table, counter, name_key = _CASES[kind]
    await _candidate(flow, kind, dict(values, **{name_key: None}), status="SKIPPED")
    deleted = await _candidate(flow, kind, dict(values, **{name_key: None}))
    staging_table = PROFILING_TABLES[STAGE_DEFS[review_stage].table]
    await flow.session.execute(
        update(staging_table).where(staging_table.c.id == deleted).values(is_deleted=1)
    )
    assert (await flow.complete("b", stage))[counter] == 0
    assert await flow.formal(table) == []


@pytest.mark.parametrize("kind", _CASES)
@pytest.mark.parametrize("invalid", [None, "", "   "])
async def test_empty_names_rejected_before_any_write(
    business_flow, monkeypatch, kind, invalid
):
    flow = business_flow
    values = await _case(flow, kind)
    await _candidate(flow, kind, values)  # 正常项也不能被部分写入
    await _candidate(flow, kind, dict(values, **{_CASES[kind][4]: invalid}))
    message = await _reject_before_writes(flow, _CASES[kind][0], monkeypatch)
    assert "名称为空" in message and "staging id=" in message


@pytest.mark.parametrize(
    "kind,attribute,value",
    [
        ("domain", "description", "another"),
        ("dataset", "source", "public.other"),
        ("field", "data_type", "varchar"),
        ("dimension", "dimension_type", "time"),
        ("metric", "priority", "HIGH"),
        ("binding", "expr", "country_name"),
        ("caliber", "expr", "SUM(tax)"),
    ],
)
async def test_user_can_resolve_conflict_by_skipping_one_candidate(
    business_flow, monkeypatch, kind, attribute, value
):
    flow = business_flow
    values = await _case(flow, kind)
    first = await _candidate(flow, kind, values, status="PENDING")
    second = await _candidate(
        flow, kind, dict(values, **{attribute: value}), status="PENDING"
    )
    # 编辑/单条确认不增加拦截；complete 才做最终校验。
    if kind not in ("dimension", "metric"):
        await flow.review("b", _CASES[kind][1], ids=[first, second])
    await _reject_before_writes(flow, _CASES[kind][0], monkeypatch)
    if kind in ("dimension", "metric"):
        # 管理阶段没有逐行确认/跳过入口，使用现有编辑入口统一描述/类型等定义。
        from profiling.services import row_service

        await row_service.update_row(
            flow.session,
            STAGE_DEFS[_CASES[kind][1]],
            "b",
            second,
            {attribute: values[attribute]},
            flow.principal,
        )
    else:
        await flow.review("b", _CASES[kind][1], ids=[second], confirm=False)
    assert (await flow.complete("b", _CASES[kind][0]))[_CASES[kind][3]] == 1


@pytest.mark.parametrize("kind", _CASES)
async def test_formal_key_ambiguity_is_explicit(business_flow, monkeypatch, kind):
    flow = business_flow
    values = await _case(flow, kind)
    await _candidate(flow, kind, values)
    stage, _, table, _, name_key = _CASES[kind]
    await flow.complete("b", stage)
    [original] = await flow.formal(table)
    duplicate = dict(original)
    duplicate.pop("id")
    if name_key in duplicate:
        duplicate[name_key] = f" {duplicate[name_key]} "
    elif kind == "caliber":
        duplicate["time_window"] = " 1d "
    # Binding 没有业务唯一索引；其余利用 trim 后同键、存储文本不同构造活跃歧义。
    other_id = await _insert(flow, table, **duplicate)
    message = await _reject_before_writes(flow, stage, monkeypatch)
    assert all(
        part in message
        for part in (
            "多条活跃正式记录",
            f"正式 id={original['id']}",
            f"正式 id={other_id}",
            "消除歧义",
        )
    )


@pytest.mark.parametrize("kind", _CASES)
@pytest.mark.parametrize(
    "scope_change",
    [
        {"is_deleted": 1},
        {"tenant_id": "other-tenant"},
        {"workspace_id": "other-workspace"},
    ],
    ids=["deleted", "other-tenant", "other-workspace"],
)
async def test_nonactive_or_out_of_scope_formal_records_do_not_block_new(
    business_flow, kind, scope_change
):
    flow = business_flow
    values = await _case(flow, kind)
    await _candidate(flow, kind, values)
    stage, _, table, counter, _ = _CASES[kind]
    await flow.complete("b", stage)
    [original] = await flow.formal(table)
    await flow.session.execute(
        update(table).where(table.c.id == original["id"]).values(**scope_change)
    )
    assert (await flow.complete("b", stage))[counter] == 1
    old = (
        (await flow.session.execute(select(table).where(table.c.id == original["id"])))
        .mappings()
        .one()
    )
    assert dict(old) == {**original, **scope_change}


def _members(*names):
    return json.dumps([{"source_table": name} for name in names])


async def test_domain_incremental_members_keep_all_confirmed_tasks_and_staging_rows(
    business_flow,
):
    flow = business_flow
    await flow.add_source("b", [])
    for task, members in (
        ("old-task", ("public.orders",)),
        ("new-task", ("public.orders", "public.refunds")),
    ):
        await staging_repo.insert_row(
            flow.session,
            "staging_biz_domain",
            dict(
                domain_name="Sales",
                description="sales",
                member_tables=_members(*members),
            ),
            datasource_id="b",
            task_id=task,
            probe_status="CONFIRMED",
            **flow.scope,
        )
    before = await flow.rows("b", "BIZ_DOMAIN")
    assert (await flow.complete("b", "BIZ_DOMAIN"))["promoted"] == 1
    assert await flow.rows("b", "BIZ_DOMAIN") == before
    assert (await flow.complete("b", "BIZ_DOMAIN"))["promoted"] == 0
    assert await flow.rows("b", "BIZ_DOMAIN") == before
    [formal] = await flow.formal(domain)
    assert formal["datasource_scope"] == ""


async def test_domain_member_conflict_includes_historical_confirmation(
    business_flow, monkeypatch
):
    flow = business_flow
    await flow.add_source("b", [])
    old_id = await staging_repo.insert_row(
        flow.session,
        "staging_biz_domain",
        dict(
            domain_name="Sales",
            description="销售",
            member_tables=_members("public.orders"),
        ),
        datasource_id="b",
        task_id="old-task",
        probe_status="CONFIRMED",
        **flow.scope,
    )
    await flow.complete("b", "BIZ_DOMAIN")
    new_id = await _stage(
        flow,
        "staging_biz_domain",
        domain_name="Support",
        member_tables=_members("public.orders"),
    )
    message = await _reject_before_writes(flow, "BIZ_DOMAIN", monkeypatch)
    assert all(
        part in message
        for part in ("public.orders", "Sales", "Support", str(old_id), str(new_id))
    )


@pytest.mark.parametrize(
    "formal_source,formal_name,source_id,reject",
    [
        ("public.orders", "display_name", "b", True),
        ("public.other", "public.orders", "b", False),  # 不猜展示名称
        ("SELECT * FROM public.orders", "public.orders", "b", False),  # 不猜 SQL 血缘
        ("public.orders", "public.orders", "a", False),  # 不锁其他源
    ],
)
async def test_domain_members_only_anchor_exact_local_formal_source(
    business_flow, monkeypatch, formal_source, formal_name, source_id, reject
):
    flow = business_flow
    await flow.add_source("a", [])
    await flow.add_source("b", [])
    owner = await _insert(flow, domain, domain_name="Sales")
    await _insert(
        flow,
        dataset,
        datasource_id=source_id,
        domain_id=owner,
        dataset_name=formal_name,
        source=formal_source,
    )
    await _stage(
        flow,
        "staging_biz_domain",
        domain_name="Support",
        member_tables=_members("public.orders"),
    )
    if reject:
        message = await _reject_before_writes(flow, "BIZ_DOMAIN", monkeypatch)
        assert (
            "public.orders" in message
            and "domain_id" in message
            and "正式 id=" in message
        )
    else:
        assert (await flow.complete("b", "BIZ_DOMAIN"))["promoted"] == 1


@pytest.mark.parametrize(
    "members", ["not-json", "{}", '[{"source_table":"  "}]', '["public.orders"]']
)
async def test_invalid_domain_members_fail_before_any_write(
    business_flow, monkeypatch, members
):
    flow = business_flow
    await flow.add_source("b", [])
    await _stage(flow, "staging_biz_domain", domain_name="Sales", member_tables=members)
    assert "member_tables" in await _reject_before_writes(
        flow, "BIZ_DOMAIN", monkeypatch
    )


@pytest.mark.parametrize("foreign_id", [False, True])
async def test_existing_dataset_receives_late_confirmed_fields_without_staged_parent(
    business_flow, foreign_id
):
    flow = business_flow
    values = await _case(flow, "field")
    if foreign_id:
        await flow.add_source("a", [])
        values["dataset_id"] = await _insert(
            flow,
            dataset,
            datasource_id="a",
            domain_id=(await flow.formal(domain))[0]["id"],
            dataset_name="public.orders",
            source="public.orders",
        )
    original_datasets = await flow.formal(dataset)
    first = await _candidate(flow, "field", values, status="SKIPPED")
    assert (await flow.complete("b", "DATASET_FIELDS"))["promoted_dataset_field"] == 0
    await flow.review("b", "DATASET_FIELDS", ids=[first])
    assert (await flow.complete("b", "DATASET_FIELDS"))["promoted_dataset_field"] == 1
    assert (await flow.complete("b", "DATASET_FIELDS"))["promoted_dataset_field"] == 0
    [formal] = await flow.formal(dataset_field)
    local_parent = next(d for d in original_datasets if d["datasource_id"] == "b")
    assert formal["dataset_id"] == local_parent["id"]
    assert await flow.formal(dataset) == original_datasets
    await _candidate(flow, "field", dict(values, field_name="tax"))
    assert (await flow.complete("b", "DATASET_FIELDS"))["promoted_dataset_field"] == 1
    assert {r["field_name"] for r in await flow.formal(dataset_field)} == {
        "amount",
        "tax",
    }


@pytest.mark.parametrize("kind", ["field", "binding", "caliber"])
@pytest.mark.parametrize(
    "parent", ["missing-name", "foreign-id-only", "deleted-id-only", "wrong-domain"]
)
async def test_invalid_parent_never_silently_drops_confirmed_relations(
    business_flow, monkeypatch, kind, parent
):
    flow = business_flow
    values = await _case(flow, kind)
    if parent == "missing-name":
        values.update(dataset_name="missing", dataset_id=999999)
    elif parent == "foreign-id-only":
        await flow.add_source("a", [])
        values.update(
            dataset_name=None,
            dataset_id=await _insert(
                flow,
                dataset,
                datasource_id="a",
                domain_id=(await flow.formal(domain))[0]["id"],
                dataset_name="public.orders",
                source="public.orders",
            ),
        )
    elif parent == "deleted-id-only":
        await flow.session.execute(
            update(dataset)
            .where(dataset.c.id == values["dataset_id"])
            .values(is_deleted=1)
        )
        values["dataset_name"] = None
    else:
        if kind == "field":
            # dataset_field 无 domain_name；名称存在时不能用旧 ID 强行绑定别名。
            values["dataset_name"] = "renamed"
        else:
            other = await _insert(flow, domain, domain_name="Other")
            await flow.session.execute(
                update(dataset)
                .where(dataset.c.id == values["dataset_id"])
                .values(domain_id=other)
            )
    await _candidate(flow, kind, values)
    message = await _reject_before_writes(flow, _CASES[kind][0], monkeypatch)
    assert "Dataset" in message and "dataset_id" in message and "staging id=" in message


@pytest.mark.parametrize("kind", ["field", "binding", "caliber"])
async def test_verified_local_formal_dataset_id_can_resolve_without_name(
    business_flow, kind
):
    flow = business_flow
    values = await _case(flow, kind)
    await _candidate(flow, kind, dict(values, dataset_name=None))
    assert (await flow.complete("b", _CASES[kind][0]))[_CASES[kind][3]] == 1


@pytest.mark.parametrize(
    "old_type,new_type", [(None, "time"), ("time", None), (None, None)]
)
async def test_dimension_missing_type_does_not_overwrite_formal_metadata(
    business_flow, old_type, new_type
):
    flow = business_flow
    values = await _case(flow, "dimension")
    await _candidate(flow, "dimension", dict(values, dimension_type=old_type))
    await flow.complete("b", "DIMENSION_MANAGE")
    before = await flow.formal(dimension_dict)
    await _candidate(flow, "dimension", dict(values, dimension_type=new_type))
    assert (await flow.complete("b", "DIMENSION_MANAGE"))["promoted_dimension"] == 0
    assert await flow.formal(dimension_dict) == before


async def test_metric_time_windows_are_independent(business_flow):
    flow = business_flow
    values = await _case(flow, "caliber")
    await _candidate(flow, "caliber", values)
    await _candidate(
        flow,
        "caliber",
        dict(values, time_window="7d", expr="SUM(tax)", additivity="semi_additive"),
    )
    assert (await flow.complete("b", "METRIC_MANAGE"))["promoted_metric_caliber"] == 2
    assert {r["time_window"] for r in await flow.formal(metric_caliber)} == {"1d", "7d"}


async def test_three_sources_promote_same_names_and_keep_dataset_local_definitions(
    business_flow,
):
    flow = business_flow
    canonical = None
    for source in ("a", "b", "c"):
        await flow.add_source(source, [])
        await _stage(
            flow,
            "staging_biz_domain",
            datasource_id=source,
            domain_name="Sales",
            description="new description " + source,
            member_tables=_members("public.orders", "public.refunds"),
            status="PENDING",
        )
        await flow.review(source, "BIZ_DOMAIN")
        assert (await flow.complete(source, "BIZ_DOMAIN"))["promoted"] == (
            1 if source == "a" else 0
        )
        for table_name in ("public.orders", "public.refunds"):
            await _stage(
                flow,
                "staging_dataset",
                datasource_id=source,
                domain_name="Sales",
                dataset_name=table_name,
                source=table_name,
                status="PENDING",
            )
            await _stage(
                flow,
                "staging_dataset_field",
                datasource_id=source,
                dataset_name=table_name,
                field_name="country",
                data_type="varchar",
                status="PENDING",
            )
        await flow.review(source, "DATASET")
        await flow.complete(source, "DATASET")
        await flow.review(source, "DATASET_FIELDS")
        result = await flow.complete(source, "DATASET_FIELDS")
        assert result["promoted"] == result["promoted_dataset_field"] == 2

        await _stage(
            flow,
            "staging_dimension",
            datasource_id=source,
            domain_name="Sales",
            dimension_name="Country",
            dimension_type="categorical",
            status="PENDING",
        )
        await _stage(
            flow,
            "staging_metric",
            datasource_id=source,
            domain_name="Sales",
            metric_name="Revenue",
            status="PENDING",
        )
        for index, table_name in enumerate(("public.orders", "public.refunds")):
            common = dict(
                datasource_id=source,
                domain_name="Sales",
                dataset_name=table_name,
                status="PENDING",
            )
            await _stage(
                flow,
                "staging_dataset_dimension",
                **common,
                dimension_name="Country",
                expr=f"country_{source}_{index}",
                value_example=source,
            )
            await _stage(
                flow,
                "staging_metric_caliber",
                **common,
                metric_name="Revenue",
                expr=f"SUM(amount_{source}_{index})",
                time_window="1d",
                additivity="additive",
            )
        for stage in ("DIMENSION_CALIBER", "METRIC_CALIBER"):
            await flow.review(source, stage)
            await flow.complete(source, stage)
        dim = await flow.complete(source, "DIMENSION_MANAGE")
        metric = await flow.complete(source, "METRIC_MANAGE")
        assert (
            dim["promoted_dimension"]
            == metric["promoted_metric"]
            == (1 if source == "a" else 0)
        )
        assert (
            dim["promoted_dataset_dimension"] == metric["promoted_metric_caliber"] == 2
        )
        shared = [await flow.formal(t) for t in (domain, dimension_dict, metric_dict)]
        if canonical is None:
            canonical = shared
        assert shared[1:] == canonical[1:]
        assert shared[0][0]["datasource_scope"] == "$$$".join(
            "abc"[: "abc".index(source) + 1]
        )
        assert shared[0][0] == {
            **canonical[0][0],
            **{
                key: shared[0][0][key]
                for key in ("datasource_scope", "updated_by", "updated_at")
            },
        }
        assert (await flow.complete(source, "DIMENSION_MANAGE"))[
            "promoted_dataset_dimension"
        ] == 0
        assert (await flow.complete(source, "METRIC_MANAGE"))[
            "promoted_metric_caliber"
        ] == 0

    datasets = await flow.formal(dataset)
    assert len(datasets) == 6
    assert {(d["datasource_id"], d["dataset_name"]) for d in datasets} == {
        (source, name)
        for source in ("a", "b", "c")
        for name in ("public.orders", "public.refunds")
    }
    bindings, calibers = (
        await flow.formal(dataset_dimension),
        await flow.formal(metric_caliber),
    )
    assert len(bindings) == len(calibers) == len(await flow.formal(dataset_field)) == 6
    for relation in (*bindings, *calibers):
        parent = next(d for d in datasets if d["id"] == relation["dataset_id"])
        assert parent["datasource_id"] in relation["expr"]
        assert relation["domain_id"] == parent["domain_id"] == canonical[0][0]["id"]


@pytest.mark.parametrize("kind", _CASES)
async def test_trim_and_default_normalization_do_not_compare_lineage_or_audit(
    business_flow, kind
):
    flow = business_flow
    values = await _case(flow, kind)
    first = await _candidate(flow, kind, values)
    padded = {k: f" {v} " if isinstance(v, str) else v for k, v in values.items()}
    if kind == "field":
        padded.update(is_primary=None, is_nullable=None)  # 既有默认值 N / Y
    if kind in ("binding", "caliber"):
        padded["column_name"] = "different_lineage"
    second = await _candidate(flow, kind, dict(padded, origin="refine"))
    table = PROFILING_TABLES[STAGE_DEFS[_CASES[kind][1]].table]
    await flow.session.execute(
        update(table).where(table.c.id == first).values(task_id="old-task")
    )
    await flow.session.execute(
        update(table)
        .where(table.c.id == second)
        .values(task_id="new-task", created_by="other-reviewer")
    )
    assert (await flow.complete("b", _CASES[kind][0]))[_CASES[kind][3]] == 1


async def _http_complete(flow, monkeypatch, stage):
    import httpx
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from profiling.routers.probe_router import router
    from semantic_config import db as sc_db
    from semantic_config.errors import register_exception_handlers
    from semantic_config.principal import current_principal

    # 只替换认证和 session factory；真实路由、service、get_db 事务及异常处理不替换。
    factory = async_sessionmaker(flow.session.bind, expire_on_commit=False)
    monkeypatch.setattr(sc_db, "get_session_factory", lambda: factory)
    app = FastAPI()
    app.include_router(router)
    register_exception_handlers(app)
    app.dependency_overrides[current_principal] = lambda: flow.principal
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://profiling.test"
    ) as client:
        response = await client.post(
            f"/api/semantic-config/probe/b/{STAGE_DEFS[stage].path}/complete"
        )
    # 请求结束后的检查使用全新 session，不依赖原会话的内存状态。
    async with factory() as session:
        from types import SimpleNamespace

        persisted = await _state(SimpleNamespace(session=session))
    return response, persisted


@pytest.mark.parametrize(
    "kind,attribute,value",
    [
        ("domain", "description", "different"),
        ("dataset", "source", "public.other"),
        ("field", "data_type", "varchar"),
        ("dimension", "dimension_type", "time"),
        ("metric", "priority", "HIGH"),
        ("binding", "expr", "other_country"),
        ("caliber", "additivity", "non_additive"),
    ],
)
async def test_http_complete_returns_actionable_400_and_preserves_review(
    business_flow, monkeypatch, kind, attribute, value
):
    flow = business_flow
    values = await _case(flow, kind)
    first = await _candidate(flow, kind, values)
    second = await _candidate(flow, kind, dict(values, **{attribute: value}))
    before = await _state(flow)
    await flow.session.commit()
    response, persisted = await _http_complete(flow, monkeypatch, _CASES[kind][0])
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == 400
    assert all(
        part in body["message"]
        for part in (
            attribute,
            "冲突",
            "业务身份",
            f"staging id={first}",
            f"staging id={second}",
            "请统一",
        )
    )
    assert persisted == before


@pytest.mark.parametrize("kind", ["domain", "dataset", "dimension", "metric"])
async def test_http_request_rolls_back_inserts_and_stage_completion_on_failure(
    business_flow, monkeypatch, kind
):
    flow = business_flow
    values = await _case(flow, kind)
    await _candidate(flow, kind, values)
    before = await _state(flow)
    await flow.session.commit()
    original = promote._set_completed
    attempted = []

    async def fail_after_writes(db, datasource_id, stage, principal):
        # 人为模拟写入后的异常，证明请求事务确实回滚，不仅仅是预校验没有执行 INSERT。
        rows = (await db.execute(select(_CASES[kind][2]))).mappings().all()
        assert len(rows) == 1
        attempted.append(rows[0]["id"])
        await original(db, datasource_id, stage, principal)
        raise BadRequestError("模拟写入后失败，整次 complete 必须回滚")

    monkeypatch.setattr(promote, "_set_completed", fail_after_writes)
    response, persisted = await _http_complete(flow, monkeypatch, _CASES[kind][0])
    assert response.status_code == 400
    assert "整次 complete 必须回滚" in response.json()["message"]
    assert attempted
    assert persisted == before


async def test_http_success_commits_new_dataset_field(business_flow, monkeypatch):
    flow = business_flow
    values = await _case(flow, "field")
    await _candidate(flow, "field", values)
    await flow.session.commit()
    response, persisted = await _http_complete(flow, monkeypatch, "DATASET_FIELDS")
    assert response.status_code == 200, response.text
    assert response.json()["promoted"] == 0
    assert response.json()["promoted_dataset_field"] == 1
    assert len(persisted[dataset_field.name]) == 1


@pytest.mark.parametrize(
    "kind,attribute",
    [
        ("dataset", "source"),
        ("binding", "expr"),
        ("caliber", "expr"),
        ("caliber", "time_window"),
    ],
)
@pytest.mark.parametrize("invalid", [None, "", "   "])
async def test_required_local_definition_is_validated_even_when_entity_exists(
    business_flow, monkeypatch, kind, attribute, invalid
):
    flow = business_flow
    values = await _case(flow, kind)
    await _candidate(flow, kind, values)
    await flow.complete("b", _CASES[kind][0])
    await _candidate(flow, kind, dict(values, **{attribute: invalid}))
    message = await _reject_before_writes(flow, _CASES[kind][0], monkeypatch)
    assert attribute in message and "缺少" in message


@pytest.mark.parametrize("kind", ["binding", "caliber"])
async def test_pending_relations_do_not_enter_manage_promotion(business_flow, kind):
    flow = business_flow
    values = await _case(flow, kind)
    await _candidate(flow, kind, values, status="PENDING")
    assert (await flow.complete("b", _CASES[kind][0]))[_CASES[kind][3]] == 0
    assert await flow.formal(_CASES[kind][2]) == []


@pytest.mark.parametrize(
    "stage",
    ["BIZ_DOMAIN", "DATASET", "DATASET_FIELDS", "DIMENSION_CALIBER", "METRIC_CALIBER"],
)
async def test_candidate_stage_terminal_gate_is_unchanged(
    business_flow, monkeypatch, stage
):
    flow = business_flow
    await flow.add_source("b", [])
    await _stage(flow, STAGE_DEFS[stage].table, status="PENDING")
    assert "请确保" in await _reject_before_writes(flow, stage, monkeypatch)


@pytest.mark.parametrize("kind", ["binding", "caliber"])
async def test_new_entity_does_not_reuse_relation_with_null_entity_id(
    business_flow, kind
):
    flow = business_flow
    values = await _case(flow, kind)
    entity_table = dimension_dict if kind == "binding" else metric_dict
    entity_id_column = "dimension_id" if kind == "binding" else "metric_id"
    entity_name_column = "dimension_name" if kind == "binding" else "metric_name"
    # 未创建的新 canonical name，不能因为其临时 ID 是 None 而命中无实体的旧关系。
    new_name = "NewCountry" if kind == "binding" else "NewRevenue"
    await _stage(
        flow,
        "staging_dimension" if kind == "binding" else "staging_metric",
        domain_name="Sales",
        **{entity_name_column: new_name},
    )
    local_payload = {
        "expr": values["expr"],
        "dataset_id": values["dataset_id"],
        entity_id_column: None,
    }
    if kind == "binding":
        local_payload["value_example"] = values["value_example"]
    else:
        local_payload.update(time_window="1d", additivity="additive")
    orphan_id = await _insert(
        flow, _CASES[kind][2], domain_id=values["domain_id"], **local_payload
    )
    await _candidate(flow, kind, dict(values, **{entity_name_column: new_name}))
    assert (await flow.complete("b", _CASES[kind][0]))[_CASES[kind][3]] == 1
    entity = next(
        r for r in await flow.formal(entity_table) if r[entity_name_column] == new_name
    )
    records = await flow.formal(_CASES[kind][2])
    assert (
        next(r for r in records if r["id"] != orphan_id)[entity_id_column]
        == entity["id"]
    )
    assert next(r for r in records if r["id"] == orphan_id)[entity_id_column] is None


@pytest.mark.parametrize(
    "kind,attribute,value",
    [
        ("domain", "description", None),
        ("dimension", "priority", "URGENT"),
        ("metric", "priority", "URGENT"),
    ],
)
async def test_http_complete_enforces_sc_validation_and_rolls_back_entire_batch(
    business_flow,
    monkeypatch,
    kind,
    attribute,
    value,
):
    flow = business_flow
    values = await _case(flow, kind)
    name_key = _CASES[kind][4]
    # 第一项合法，第二项由 service 拒绝；整批写入和阶段状态都必须回滚。
    await _candidate(flow, kind, {**values, name_key: "valid_first"})
    await _candidate(flow, kind, {**values, attribute: value})
    before = await _state(flow)
    await flow.session.commit()
    response, persisted = await _http_complete(flow, monkeypatch, _CASES[kind][0])
    assert response.status_code == 400, response.text
    assert attribute in response.json()["message"]
    assert persisted == before


@pytest.mark.parametrize("description", [None, "", "   "])
async def test_http_complete_accepts_fields_without_description(
    business_flow,
    monkeypatch,
    description,
):
    flow = business_flow
    values = await _case(flow, "field")
    await _candidate(flow, "field", {**values, "description": description})
    await flow.session.commit()

    response, persisted = await _http_complete(flow, monkeypatch, "DATASET_FIELDS")

    assert response.status_code == 200, response.text
    assert response.json()["promoted_dataset_field"] == 1
    [field] = persisted[dataset_field.name]
    assert field["field_name"] == values["field_name"]
    assert field["description"] is None

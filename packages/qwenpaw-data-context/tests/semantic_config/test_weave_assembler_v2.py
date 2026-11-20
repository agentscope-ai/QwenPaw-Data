"""Verify workspace definitions and datasource facts in the weave payload."""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import insert, select

from semantic_config.services import weave_assembler
from semantic_config.services.domain_scope import recompute_domain_scopes
from semantic_config.tables import (
    dataset,
    dataset_dimension,
    dataset_field,
    datasource,
    dimension_dict,
    domain,
    metric_caliber,
    metric_dict,
    physical_table,
    table_column,
    verified_query,
)

from _sc_dbutil import new_memory_factory

TID, WID, DS = "tenant-a", "ws-a", "holo-x"
OTHER_DS = "holo-y"


def _audit(tenant_id: str = TID, workspace_id: str = WID) -> dict:
    return {"tenant_id": tenant_id, "workspace_id": workspace_id, "created_by": "alice"}


@pytest_asyncio.fixture
async def db():
    factory, engine = await new_memory_factory()
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded(db) -> dict:
    """Seed two domains and datasources, plus foreign-tenant records."""

    async def ins(table, **vals) -> int:
        row = await db.execute(insert(table).values(**vals))
        return row.inserted_primary_key[0]

    await ins(
        datasource,
        datasource_id=DS,
        datasource_name="业务库",
        datasource_type="holo",
        config="{}",
        description="主仓",
        **_audit(),
    )

    d_growth = await ins(
        domain,
        domain_name="增长",
        description="用户增长",
        **_audit(),
    )
    # Workspace definitions remain visible without facts from this datasource.
    d_risk = await ins(
        domain,
        domain_name="风控",
        description="风险控制",
        **_audit(),
    )

    ds_order = await ins(
        dataset,
        datasource_id=DS,
        domain_id=d_growth,
        dataset_name="订单明细",
        description="订单宽表",
        source="proj.dwd_order",
        **_audit(),
    )
    ds_retention = await ins(
        dataset,
        datasource_id=DS,
        domain_id=d_growth,
        dataset_name="留存视图",
        description="留存",
        source="SELECT dt, uid FROM proj.dwd_order WHERE dt >= '20260801'",
        **_audit(),
    )
    # 另一数据源的数据集：同域（增长域 scope 因此横跨两个 ds），但不进 DS 的 weave
    external_ds_id = await ins(
        dataset,
        datasource_id=OTHER_DS,
        domain_id=d_growth,
        dataset_name="外部表",
        description="外部",
        source="proj2.other",
        **_audit(),
    )
    await ins(
        dataset,
        datasource_id=OTHER_DS,
        domain_id=d_risk,
        dataset_name="风控表",
        description="风控",
        source="proj.risk",
        **_audit(),
    )
    # 跨租户噪声：数据集落在本 ds、挂本租户域 id，只靠 tenant 过滤拦住
    await ins(
        dataset,
        datasource_id=DS,
        domain_id=d_growth,
        dataset_name="别租户表",
        description="x",
        source="proj.other_tenant",
        **_audit(tenant_id="tenant-b"),
    )

    # P2 物理表（v2.1：table_column 不存 table_name，先声明 physical_table 再以 table_id 绑定）
    pt_order = await ins(
        physical_table,
        datasource_id=DS,
        table_name="proj.dwd_order",
        table_comment="订单宽表",
        **_audit(),
    )
    pt_other = await ins(
        physical_table,
        datasource_id=OTHER_DS,
        table_name="proj2.other",
        table_comment="外部",
        **_audit(),
    )

    # P3 物理列（ds 维度）→ table_id 指向 physical_table
    tc = {}
    for col, dtype, pk, nullable, comment, samples in [
        ("dt", "text", "N", "N", "分区日期", "20260801$$$20260802"),
        ("uid", "bigint", "Y", "N", "用户ID", "1001$$$1002"),
        ("city_name", "text", "N", "Y", "城市", "北京$$$上海"),
        ("ghost", "text", "N", "Y", "无字段对应的物理列", None),
    ]:
        tc[col] = await ins(
            table_column,
            datasource_id=DS,
            table_id=pt_order,
            column_name=col,
            data_type=dtype,
            is_primary=pk,
            is_nullable=nullable,
            column_comment=comment,
            sample_values=samples,
            **_audit(),
        )
    # 其他数据源的物理列 → 不进 payload
    await ins(
        table_column,
        datasource_id=OTHER_DS,
        table_id=pt_other,
        column_name="x",
        data_type="text",
        column_comment="x",
        **_audit(),
    )

    # L3 数据集字段
    for ds_id, fname, desc, dtype, pk, nullable in [
        (ds_order, "dt", "分区字段", "text", "N", "N"),
        (ds_order, "uid", "用户ID", "bigint", "Y", "N"),
        (ds_order, "city_name", "城市；1=北京 2=上海", "text", "N", "Y"),
        (ds_retention, "dt", "日期", "text", "N", "N"),
        (ds_retention, "active_uid", "活跃用户", "bigint", "N", "Y"),
    ]:
        await ins(
            dataset_field,
            dataset_id=ds_id,
            domain_id=d_growth,
            field_name=fname,
            description=desc,
            data_type=dtype,
            is_primary=pk,
            is_nullable=nullable,
            **_audit(),
        )

    # Dimension definitions are workspace-wide; bindings are datasource-specific.
    dim_city = await ins(
        dimension_dict,
        domain_id=d_growth,
        dimension_name="城市",
        description="城市维度",
        synonyms="city$$$都市",
        dimension_type="categorical",
        child_dimensions="区县$$$商圈",
        priority="high",
        **_audit(),
    )
    # Keep this definition, but exclude its binding to the other datasource.
    dim_channel = await ins(
        dimension_dict,
        domain_id=d_growth,
        dimension_name="渠道",
        description="渠道维度",
        synonyms="channel",
        **_audit(),
    )
    # Unbound definitions remain visible.
    await ins(
        dimension_dict,
        domain_id=d_growth,
        dimension_name="设备",
        description="孤立维度",
        synonyms="",
        **_audit(),
    )
    await ins(
        dimension_dict,
        domain_id=d_risk,
        dimension_name="风险等级",
        description="风控维度",
        synonyms="",
        **_audit(),
    )
    # 跨租户噪声：同域 id 也不可见
    dim_other_tenant = await ins(
        dimension_dict,
        domain_id=d_growth,
        dimension_name="别租户维度",
        description="x",
        synonyms="",
        **_audit(tenant_id="tenant-b"),
    )

    # L5 绑定（value_example 取代已删 values 表；datasource_id 为从父 dataset 派生的冗余列）
    await ins(
        dataset_dimension,
        dataset_id=ds_order,
        dimension_id=dim_city,
        domain_id=d_growth,
        datasource_id=DS,
        expr="t.city_name",
        value_example="北京$$$上海$$$全部",
        **_audit(),
    )
    # 挂在 OTHER_DS 数据集上的绑定 → 不进 DS 的 weave
    await ins(
        dataset_dimension,
        dataset_id=external_ds_id,
        dimension_id=dim_city,
        domain_id=d_growth,
        datasource_id=OTHER_DS,
        expr="o.city",
        value_example="广州",
        **_audit(),
    )
    await ins(
        dataset_dimension,
        dataset_id=external_ds_id,
        dimension_id=dim_channel,
        domain_id=d_growth,
        datasource_id=OTHER_DS,
        expr="o.channel",
        value_example="直营",
        **_audit(),
    )
    # 跨租户噪声：绑定挂在本租户数据集 id 上，只靠 tenant 过滤拦住反推
    await ins(
        dataset_dimension,
        dataset_id=ds_order,
        dimension_id=dim_other_tenant,
        domain_id=d_growth,
        datasource_id=DS,
        expr="t.x",
        value_example="x",
        **_audit(tenant_id="tenant-b"),
    )

    # Metric definitions are workspace-wide; calibers are datasource-specific.
    m_dau = await ins(
        metric_dict,
        domain_id=d_growth,
        metric_name="DAU",
        description="日活跃用户",
        synonyms="日活跃$$$DAU",
        priority="high",
        **_audit(),
    )
    # Keep this definition, but exclude its caliber from the other datasource.
    m_external = await ins(
        metric_dict,
        domain_id=d_growth,
        metric_name="外部指标",
        description="外部",
        synonyms="",
        **_audit(),
    )
    await ins(
        metric_dict,
        domain_id=d_risk,
        metric_name="风险数",
        description="风控",
        synonyms="",
        **_audit(),
    )
    # 跨租户噪声：同域 id 也不可见
    m_other_tenant = await ins(
        metric_dict,
        domain_id=d_growth,
        metric_name="别租户指标",
        description="x",
        synonyms="",
        **_audit(tenant_id="tenant-b"),
    )

    # L7 口径（datasource_id 为从父 dataset 派生的冗余列）
    await ins(
        metric_caliber,
        metric_id=m_dau,
        dataset_id=ds_order,
        domain_id=d_growth,
        datasource_id=DS,
        expr="count(distinct uid)",
        time_window="1d",
        additivity="non_additive",
        is_default=1,
        **_audit(),
    )
    await ins(
        metric_caliber,
        metric_id=m_dau,
        dataset_id=ds_retention,
        domain_id=d_growth,
        datasource_id=DS,
        expr="count(distinct active_uid)",
        time_window="7d",
        **_audit(),
    )
    # OTHER_DS 数据集上的口径 → 不进 DS 的 weave
    await ins(
        metric_caliber,
        metric_id=m_dau,
        dataset_id=external_ds_id,
        domain_id=d_growth,
        datasource_id=OTHER_DS,
        expr="count(1)",
        time_window="30d",
        **_audit(),
    )
    await ins(
        metric_caliber,
        metric_id=m_external,
        dataset_id=external_ds_id,
        domain_id=d_growth,
        datasource_id=OTHER_DS,
        expr="count(2)",
        time_window="30d",
        **_audit(),
    )
    # 跨租户噪声：口径挂在本租户数据集 id 上，只靠 tenant 过滤拦住反推
    await ins(
        metric_caliber,
        metric_id=m_other_tenant,
        dataset_id=ds_order,
        domain_id=d_growth,
        datasource_id=DS,
        expr="count(3)",
        time_window="1d",
        **_audit(tenant_id="tenant-b"),
    )

    # L8 黄金查询（v2.1：verified_query 已删 source 列，只存 question / verified_sql）
    await ins(
        verified_query,
        domain_id=d_growth,
        question="昨天的DAU是多少",
        verified_sql="SELECT count(DISTINCT uid) FROM proj.dwd_order WHERE dt = '20260819'",
        **_audit(),
    )
    await ins(
        verified_query,
        domain_id=d_risk,
        question="风险查询",
        verified_sql="SELECT 1",
        **_audit(),
    )

    # 数据集集齐后重算派生缓存列：增长域 → 'holo-x$$$holo-y'，风控域 → 'holo-y'
    await recompute_domain_scopes(
        db, [d_growth, d_risk], tenant_id=TID, workspace_id=WID
    )

    return {
        "db": db,
        "ds_order": ds_order,
        "ds_retention": ds_retention,
        "domain": d_growth,
        "domain_risk": d_risk,
        "ds_external": external_ds_id,
    }


def _growth_domain(payload: dict) -> dict:
    domains = payload["semantic"]["domains"]
    matches = [d for d in domains if d["name"] == "增长"]
    assert len(matches) == 1
    return matches[0]


# ---------------- _fetch_all 重锚定 + payload schema ----------------


async def test_assemble_preserves_workspace_domains_and_datasource_scopes(seeded):
    db = seeded["db"]
    payload = await weave_assembler.assemble(db, DS, "TASK_T1", TID, WID)

    # 12 实体重构后域的锚定介质 = 派生缓存列 datasource_scope（不再是域上的 ds 外键）
    scopes = dict(
        (
            await db.execute(
                select(domain.c.domain_name, domain.c.datasource_scope).where(
                    domain.c.tenant_id == TID
                )
            )
        ).all()
    )
    assert scopes == {"增长": f"{DS}$$${OTHER_DS}", "风控": OTHER_DS}

    dom = _growth_domain(payload)
    assert payload["datasource_id"] == DS
    assert payload["drop_semantic_first"] is True
    assert {d["name"] for d in payload["semantic"]["domains"]} == {"增长", "风控"}
    risk = next(d for d in payload["semantic"]["domains"] if d["name"] == "风控")
    assert "datasets" not in risk
    assert {d["name"] for d in risk["dimensions"]} == {"风险等级"}
    assert {m["name"] for m in risk["metrics"]} == {"风险数"}
    # SPEC 01：display_name 已随兼容列退役，payload 只带 name + description
    assert "display_name" not in dom
    assert dom["description"] == "用户增长"


async def test_dataset_payload_carries_source_and_l3_fields(seeded):
    payload = await weave_assembler.assemble(seeded["db"], DS, "TASK_T2", TID, WID)
    dom = _growth_domain(payload)
    ds_map = {d["name"]: d for d in dom["datasets"]}
    # dataset 按自带 datasource_id 过滤：外部表（OTHER_DS）与别租户表均被排除
    assert set(ds_map) == {"订单明细", "留存视图"}

    order = ds_map["订单明细"]
    assert order["source"] == "proj.dwd_order"
    assert order["description"] == "订单宽表"
    cols = {c["name"]: c for c in order["dataset_fields"]}
    assert set(cols) == {"dt", "uid", "city_name"}
    # L3 字段属性 + ④a 'Y'/'N' → bool
    assert cols["uid"]["description"] == "用户ID"
    assert cols["uid"]["data_type"] == "bigint"
    assert cols["uid"]["is_primary"] is True
    assert cols["uid"]["is_nullable"] is False
    # 表式数据集：样例值从 P3 table_column.sample_values 富化
    assert cols["uid"]["sample_values"] == ["1001", "1002"]
    assert cols["city_name"]["description"] == "城市；1=北京 2=上海"


async def test_sql_dataset_columns_skip_physical_enrichment(seeded):
    payload = await weave_assembler.assemble(seeded["db"], DS, "TASK_T3", TID, WID)
    dom = _growth_domain(payload)
    ret = next(d for d in dom["datasets"] if d["name"] == "留存视图")
    assert ret["source"].upper().startswith("SELECT")
    cols = {c["name"]: c for c in ret["dataset_fields"]}
    assert set(cols) == {"dt", "active_uid"}
    assert "sample_values" not in cols["active_uid"]


async def test_dimensions_and_metrics_without_datasource_filter(seeded):
    """Keep shared definitions without importing another datasource's bindings."""
    payload = await weave_assembler.assemble(seeded["db"], DS, "TASK_T4", TID, WID)
    dom = _growth_domain(payload)

    dims = {d["name"]: d for d in dom["dimensions"]}
    assert set(dims) == {"城市", "渠道", "设备"}
    assert "dimension_bindings" not in dims["渠道"]
    assert "dimension_bindings" not in dims["设备"]
    city = dims["城市"]
    assert city["description"] == "城市维度"
    assert city["aliases"] == ["city", "都市"]
    assert city["dimension_type"] == "categorical"
    assert city["child_dimensions"] == ["区县", "商圈"]
    assert city["priority"] == "high"

    metrics = {m["name"]: m for m in dom["metrics"]}
    assert set(metrics) == {"DAU", "外部指标"}
    assert "metric_calibers" not in metrics["外部指标"]
    assert metrics["DAU"]["aliases"] == ["日活跃", "DAU"]
    assert metrics["DAU"]["priority"] == "high"
    assert metrics["DAU"]["description"] == "日活跃用户"


async def test_bindings_carry_expr_and_value_example(seeded):
    payload = await weave_assembler.assemble(seeded["db"], DS, "TASK_T5", TID, WID)
    dom = _growth_domain(payload)
    city = next(d for d in dom["dimensions"] if d["name"] == "城市")
    assert city["dimension_bindings"] == [
        {
            "dataset": "订单明细",
            "expr": "t.city_name",
            "value_example": ["北京", "上海", "全部"],
        }
    ]


async def test_calibers_nested_under_metrics(seeded):
    payload = await weave_assembler.assemble(seeded["db"], DS, "TASK_T6", TID, WID)
    dom = _growth_domain(payload)
    dau = next(m for m in dom["metrics"] if m["name"] == "DAU")
    cals = dau["metric_calibers"]
    assert {
        "dataset": "订单明细",
        "expr": "count(distinct uid)",
        "time_window": "1d",
        "additivity": "non_additive",
        "is_default": True,
    } in cals
    assert {
        "dataset": "留存视图",
        "expr": "count(distinct active_uid)",
        "time_window": "7d",
    } in cals
    assert len(cals) == 2  # OTHER_DS 数据集上的 30d 口径被排除


async def test_verified_queries_carried_in_payload(seeded):
    """L8 导入路径 = weave payload 携带（用户拍板；W3 入图 ANCHORED_TO）。"""
    payload = await weave_assembler.assemble(seeded["db"], DS, "TASK_T7", TID, WID)
    dom = _growth_domain(payload)
    # v2.1：verified_query 已删 source 列，payload 只带 question / verified_sql（不再发 source_channel）
    assert dom["verified_queries"] == [
        {
            "question": "昨天的DAU是多少",
            "verified_sql": "SELECT count(DISTINCT uid) FROM proj.dwd_order WHERE dt = '20260819'",
        }
    ]


async def test_payload_emits_canonical_segments_only(seeded):
    """Use canonical field names and omit absent bindings and calibers."""
    payload = await weave_assembler.assemble(seeded["db"], DS, "TASK_TC", TID, WID)
    dom = _growth_domain(payload)

    for ds in dom["datasets"]:
        assert "columns" not in ds, f"旧段名 columns 残留: {ds}"
        assert "dataset_fields" in ds, f"缺正名段 dataset_fields: {ds}"
    for dim in dom["dimensions"]:
        assert "bindings" not in dim, f"旧段名 bindings 残留: {dim}"
        assert ("dimension_bindings" in dim) == (dim["name"] == "城市")
    for met in dom["metrics"]:
        assert "calibers" not in met and "formulas" not in met, (
            f"旧段名 calibers/formulas 残留: {met}"
        )
        assert ("metric_calibers" in met) == (met["name"] == "DAU")
    for vq in dom["verified_queries"]:
        # v2.1：verified_query 已删 source 列；payload 也不再发 source_channel
        assert "source" not in vq, f"旧字段 source 残留: {vq}"
        assert "source_channel" not in vq, f"source_channel 不应再出现: {vq}"


async def test_tables_section_groups_physical_columns_per_datasource(seeded):
    payload = await weave_assembler.assemble(seeded["db"], DS, "TASK_T8", TID, WID)
    tables = {t["name"]: t for t in payload["tables"]}
    assert set(tables) == {"proj.dwd_order"}
    cols = {c["name"]: c for c in tables["proj.dwd_order"]["columns"]}
    assert set(cols) == {"dt", "uid", "city_name", "ghost"}
    assert cols["uid"]["is_primary"] is True
    assert cols["uid"]["is_nullable"] is False
    assert cols["uid"]["comment"] == "用户ID"
    assert cols["uid"]["sample_values"] == ["1001", "1002"]
    assert cols["uid"]["data_type"] == "bigint"


async def test_scope_isolation_across_tenants(seeded):
    """Exclude foreign-tenant facts even when they reference visible parents."""
    payload = await weave_assembler.assemble(seeded["db"], DS, "TASK_T9", TID, WID)
    dom = _growth_domain(payload)
    assert "别租户表" not in {d["name"] for d in dom["datasets"]}
    assert "别租户维度" not in {d["name"] for d in dom["dimensions"]}
    assert "别租户指标" not in {m["name"] for m in dom["metrics"]}

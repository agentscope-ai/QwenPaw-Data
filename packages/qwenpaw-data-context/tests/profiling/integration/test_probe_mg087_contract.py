"""mg-087 v2.1 契约守卫：metric_caliber 三元组去重 + 多值列 $$$ 往返。

两条都是「照参考实现对齐」的落地证据：

1. promote 的口径去重键 = (metric_id, dataset_id, time_window) —— 与存储唯一索引
   ``uq_caliber_scope_key`` / ``metric_caliber_repo.exists_key`` 同键。交接文档
   写的「(metric_id, time_window)」漏了 dataset_id，照文档写会把同一度量在两个
   数据集上的口径误判为重复丢一条 —— 本用例钉住正确键。
2. profiling 写入的多值列（synonyms / value_example）必须能被
   ``weave_assembler._split_triple_dollar`` 拆回同样多的元素 —— v2.1 存储面
   多值统一 ``$$$``（W1 契约），逗号串会静默并成一条污染检索。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from qwenpaw_data_auth import Principal
from sqlalchemy import text

from profiling.db import init_probe_db
from profiling.models.probe import ROW_CONFIRMED
from profiling.repositories import staging_repo
from profiling.services import promote
from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.services.weave_assembler import _split_triple_dollar

from profiling.algo.inputs.probe_facts_input import StaticProbeFactsInput


def _principal(user="u1", ws="w1", tenant="t1", role="tenant_admin") -> Principal:
    return Principal(
        user_id=user,
        tenant_id=tenant,
        workspace_ids=[ws],
        active_workspace_id=ws,
        role=role,
    )


async def _open_conn():
    from semantic_config.engine import get_session_factory

    return get_session_factory()()


def _isolated_db():
    """Use an explicit temporary SQLite URL instead of the deployment database."""
    settings = get_settings()
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    originals = settings.db_url
    settings.db_url = "sqlite+aiosqlite:///" + tmp.name
    return settings, originals, tmp.name


async def _scalar(conn, sql) -> int:
    res = await conn.execute(text(sql))
    val = res.scalar()
    return val if val is not None else 0


def test_metric_caliber_dedup_key_includes_dataset_and_window():
    async def _run():
        settings, originals, tmp_path = _isolated_db()
        try:
            await init_db()
            await init_probe_db()
            conn = await _open_conn()
            admin = _principal()
            ds = "mg087-caliber-key"
            common = {
                "datasource_id": ds,
                "tenant_id": admin.tenant_id,
                "workspace_id": admin.active_workspace_id,
                "created_by": admin.user_id,
                "task_id": None,
            }
            # 域 + 两个数据集（source 必填）
            await conn.execute(
                text(
                    "INSERT INTO domain (domain_name, tenant_id, workspace_id, is_deleted)"
                    " VALUES ('销售', 't1', 'w1', 0)"
                )
            )
            dom_id = await _scalar(
                conn, "SELECT id FROM domain WHERE domain_name='销售'"
            )
            for name in ("orders", "orders_daily"):
                await conn.execute(
                    text(
                        "INSERT INTO dataset (datasource_id, domain_id, dataset_name,"
                        " source, tenant_id, workspace_id, is_deleted)"
                        f" VALUES ('{ds}', {dom_id}, '{name}', 'public.{name}', 't1', 'w1', 0)"
                    )
                )
            ds1 = await _scalar(
                conn, "SELECT id FROM dataset WHERE dataset_name='orders'"
            )
            ds2 = await _scalar(
                conn, "SELECT id FROM dataset WHERE dataset_name='orders_daily'"
            )

            # 同一度量四条口径：同数据集同窗口重复 ×2（应去重）、
            # 同数据集异窗口、异数据集同窗口（都应落库）
            rows = [
                (ds1, "orders", "1d"),
                (ds1, "orders", "1d"),  # 重复 → 跳过
                (ds1, "orders", "30d"),  # 异窗口 → 落库
                (
                    ds2,
                    "orders_daily",
                    "1d",
                ),  # 异数据集 → 落库（文档漏 dataset_id 会误杀这条）
            ]
            await staging_repo.insert_row(
                conn,
                "staging_metric",
                {
                    "domain_id": dom_id,
                    "domain_name": "销售",
                    "metric_name": "销售额",
                    "description": "d",
                    "synonyms": "GMV$$$营收",
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            for dsid, dsname, window in rows:
                await staging_repo.insert_row(
                    conn,
                    "staging_metric_caliber",
                    {
                        "domain_id": dom_id,
                        "domain_name": "销售",
                        "dataset_id": dsid,
                        "dataset_name": dsname,
                        "metric_name": "销售额",
                        "expr": "SUM(amt)",
                        "time_window": window,
                        "additivity": "additive",
                    },
                    probe_status=ROW_CONFIRMED,
                    **common,
                )
            await conn.commit()

            result = await promote._complete_metric_manage(
                conn, ds, "METRIC_MANAGE", admin
            )
            await conn.commit()
            assert result["promoted_metric"] == 1
            assert result["promoted_metric_caliber"] == 3, (
                f"三元组去重应落 3 条（重复 1 条跳过），实得 {result['promoted_metric_caliber']}"
            )
            assert (
                await _scalar(
                    conn,
                    "SELECT COUNT(*) FROM metric_formula_lib WHERE is_deleted=0 "
                    "AND (expr IS NULL OR expr='' OR time_window IS NULL OR time_window='')",
                )
                == 0
            )
            await conn.close()
        finally:
            settings.db_url = originals
            Path(tmp_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_multi_value_columns_roundtrip_triple_dollar():
    async def _run():
        settings, originals, tmp_path = _isolated_db()
        try:
            await init_db()
            await init_probe_db()
            conn = await _open_conn()
            admin = _principal()
            ds = "mg087-multi-value"
            common = {
                "datasource_id": ds,
                "tenant_id": admin.tenant_id,
                "workspace_id": admin.active_workspace_id,
                "created_by": admin.user_id,
                "task_id": None,
            }
            await conn.execute(
                text(
                    "INSERT INTO domain (domain_name, tenant_id, workspace_id, is_deleted)"
                    " VALUES ('运营', 't1', 'w1', 0)"
                )
            )
            dom_id = await _scalar(
                conn, "SELECT id FROM domain WHERE domain_name='运营'"
            )
            await conn.execute(
                text(
                    "INSERT INTO dataset (datasource_id, domain_id, dataset_name,"
                    " source, tenant_id, workspace_id, is_deleted)"
                    f" VALUES ('{ds}', {dom_id}, 'events', 'public.events', 't1', 'w1', 0)"
                )
            )
            dsid = await _scalar(
                conn, "SELECT id FROM dataset WHERE dataset_name='events'"
            )

            await staging_repo.insert_row(
                conn,
                "staging_dimension",
                {
                    "domain_id": dom_id,
                    "domain_name": "运营",
                    "dimension_name": "渠道",
                    "description": "d",
                    "synonyms": "channel$$$来源渠道$$$渠道来源",
                    "dimension_type": "categorical",
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await staging_repo.insert_row(
                conn,
                "staging_dataset_dimension",
                {
                    "domain_id": dom_id,
                    "domain_name": "运营",
                    "dataset_id": dsid,
                    "dataset_name": "events",
                    "dimension_name": "渠道",
                    "expr": "channel",
                    "value_example": "全部$$$app$$$web$$$h5",
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await conn.commit()

            await promote._complete_dimension_manage(
                conn, ds, "DIMENSION_MANAGE", admin
            )
            await conn.commit()

            res = await conn.execute(
                text(
                    "SELECT synonyms FROM dimension_dict WHERE dimension_name='渠道' AND is_deleted=0"
                )
            )
            synonyms = res.scalar()
            assert _split_triple_dollar(synonyms) == [
                "channel",
                "来源渠道",
                "渠道来源",
            ], f"synonyms $$$ 往返失败: {synonyms!r}"
            res = await conn.execute(
                text(
                    "SELECT value_example, expr, domain_id FROM dataset_dimension WHERE is_deleted=0"
                )
            )
            value_example, expr, bound_domain = res.one()
            assert _split_triple_dollar(value_example) == [
                "全部",
                "app",
                "web",
                "h5",
            ], f"value_example $$$ 往返失败: {value_example!r}"
            assert expr == "channel"
            # domain_id 派生自父数据集（对齐 dataset_dimension_service.create）
            assert bound_domain == dom_id
            await conn.close()
        finally:
            settings.db_url = originals
            Path(tmp_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_reverse_read_anchors_on_expr():
    """W1 反向读回：staged 口径行经 StaticProbeFactsInput 回读成 CaliberSnapshot，
    column_name 锚点 = expr（写出去那条路的逆）；time_window/additivity/
    value_example 原样带回 —— 锚点断了不抛异常，只会让续跑判定失效、
    每次探查重复调 LLM，所以必须有测试钉住。"""
    facts = {
        "staged_dimension_caliber": [
            {
                "dataset_name": "orders",
                "domain_name": "销售",
                "dimension_name": "渠道",
                "expr": "channel",
                "value_example": "全部$$$app$$$web",
                "probe_status": "CONFIRMED",
                "source_type": "ALGO",
            },
            # date 身份经内部血缘列 column_role 保住（缺陷 A 回归：丢了它
            # DICT 会把时间维度全判 categorical）
            {
                "dataset_name": "orders",
                "domain_name": "销售",
                "dimension_name": "日期",
                "expr": "order_date",
                "value_example": None,
                "column_role": "date",
                "probe_status": "CONFIRMED",
                "source_type": "ALGO",
            },
        ],
        "staged_metric_caliber": [
            {
                "dataset_name": "orders",
                "domain_name": "销售",
                "metric_name": "销售额",
                "expr": "SUM(amt)",
                "time_window": "30d",
                "additivity": "additive",
                "column_name": "amt",
                "probe_status": "PENDING",
                "source_type": "ALGO",
            },
            # 老行没有血缘列 column_name → 退回 expr 锚点
            {
                "dataset_name": "orders",
                "domain_name": "销售",
                "metric_name": "订单数",
                "expr": "COUNT(DISTINCT order_id)",
                "time_window": "1d",
                "additivity": "additive",
                "probe_status": "PENDING",
                "source_type": "ALGO",
            },
        ],
        "staged_dataset": [
            {
                "dataset_name": "orders",
                "domain_name": "销售",
                "description": "订单",
                "source": "SELECT * FROM public.orders",
                "probe_status": "CONFIRMED",
                "source_type": "ALGO",
            },
        ],
        "staged_dataset_fields": [
            {
                "dataset_name": "orders",
                "field_name": "channel",
                "data_type": "text",
                "is_primary": "N",
                "is_nullable": "Y",
                "description": "渠道编码",
                "probe_status": "CONFIRMED",
                "source_type": "ALGO",
            },
        ],
    }
    reader = StaticProbeFactsInput(facts)

    snap = asyncio.run(reader.facts_of("columns_caliber", "staged")())
    cal = snap.calibers
    dim = cal[cal["column_role"] == "dimension"].iloc[0]
    assert dim["column_name"] == "channel", "维度侧锚点应取 expr"
    assert list(dim["value_example"]) == ["全部", "app", "web"], "$$$ 回读拆分失败"
    date_rows = cal[cal["column_role"] == "date"]
    assert len(date_rows) == 1, "date 角色身份在回读中丢失（缺陷 A）"
    assert date_rows.iloc[0]["dimension_name"] == "日期"
    mets = cal[cal["column_role"] == "metric"].set_index("metric_name")
    # 血缘列 column_name 优先：DICT 的 synonyms/归一要的是物理列，不是 SUM(...)
    assert mets.loc["销售额", "column_name"] == "amt", (
        "指标侧应优先取血缘列 column_name"
    )
    assert mets.loc["销售额", "expr"] == "SUM(amt)"
    assert (
        mets.loc["销售额", "time_window"] == "30d"
        and mets.loc["销售额", "additivity"] == "additive"
    )
    assert mets.loc["订单数", "column_name"] == "COUNT(DISTINCT order_id)", (
        "无血缘列时退回 expr 锚点"
    )
    assert mets.loc["销售额", "probe_status"] == "PENDING", (
        "行级状态应随快照带回（续跑判定依据）"
    )

    ds = asyncio.run(reader.facts_of("dataset", "staged")())
    meta = ds.table_meta.iloc[0]
    # source 是 SQL → 内部血缘列 sql_content=源文本、parents=None
    assert meta["sql_content"].startswith("SELECT"), "SQL 式 source 应回填 sql_content"
    assert meta["parents"] is None
    cols = ds.column_meta_of("orders")
    assert list(cols["column_name"]) == ["channel"]
    assert list(cols["column_comment"]) == ["渠道编码"], "L3 description 应充当列注释"


def test_promote_resolves_dataset_id_by_name_after_upload_wash():
    """缺陷 C 回归：upload 覆盖重灌/人工新增行只带名称不带 dataset_id ——
    promote 必须按 (domain, dataset_name) 兜底解析；解析不出要指名道姓 400，
    绝不静默跳过（维度侧）也绝不落 NULL 孤儿行（指标侧，写得进读不出）。"""

    async def _run():
        settings, originals, tmp_path = _isolated_db()
        try:
            await init_db()
            await init_probe_db()
            conn = await _open_conn()
            admin = _principal()
            ds = "mg087-idwash"
            common = {
                "datasource_id": ds,
                "tenant_id": admin.tenant_id,
                "workspace_id": admin.active_workspace_id,
                "created_by": admin.user_id,
                "task_id": None,
            }
            await conn.execute(
                text(
                    "INSERT INTO domain (domain_name, tenant_id, workspace_id, is_deleted)"
                    " VALUES ('销售', 't1', 'w1', 0)"
                )
            )
            dom_id = await _scalar(
                conn, "SELECT id FROM domain WHERE domain_name='销售'"
            )
            await conn.execute(
                text(
                    "INSERT INTO dataset (datasource_id, domain_id, dataset_name,"
                    " source, tenant_id, workspace_id, is_deleted)"
                    f" VALUES ('{ds}', {dom_id}, 'orders', 'public.orders', 't1', 'w1', 0)"
                )
            )
            expected_dsid = await _scalar(
                conn, "SELECT id FROM dataset WHERE dataset_name='orders'"
            )

            # 维度线：dataset_id=None（模拟导出→上传把 id 洗掉）
            await staging_repo.insert_row(
                conn,
                "staging_dimension",
                {
                    "domain_id": dom_id,
                    "domain_name": "销售",
                    "dimension_name": "渠道",
                    "description": "d",
                    "synonyms": "channel",
                    "dimension_type": "categorical",
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await staging_repo.insert_row(
                conn,
                "staging_dataset_dimension",
                {
                    "domain_id": dom_id,
                    "domain_name": "销售",
                    "dataset_id": None,
                    "dataset_name": "orders",
                    "dimension_name": "渠道",
                    "expr": "channel",
                    "value_example": None,
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            # 指标线：同样洗掉 dataset_id
            await staging_repo.insert_row(
                conn,
                "staging_metric",
                {
                    "domain_id": dom_id,
                    "domain_name": "销售",
                    "metric_name": "销售额",
                    "description": "d",
                    "synonyms": None,
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await staging_repo.insert_row(
                conn,
                "staging_metric_caliber",
                {
                    "domain_id": dom_id,
                    "domain_name": "销售",
                    "dataset_id": None,
                    "dataset_name": "orders",
                    "metric_name": "销售额",
                    "expr": "SUM(amt)",
                    "time_window": "1d",
                    "additivity": "additive",
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await conn.commit()

            r1 = await promote._complete_dimension_manage(
                conn, ds, "DIMENSION_MANAGE", admin
            )
            r2 = await promote._complete_metric_manage(conn, ds, "METRIC_MANAGE", admin)
            await conn.commit()
            assert r1["promoted_dataset_dimension"] == 1, "维度绑定被静默跳过（缺陷 C）"
            assert r2["promoted_metric_caliber"] == 1
            # 绑定与口径都必须解析到真实 dataset_id，零 NULL 孤儿
            assert (
                await _scalar(
                    conn,
                    "SELECT COUNT(*) FROM dataset_dimension WHERE dataset_id IS NULL",
                )
                == 0
            )
            assert (
                await _scalar(
                    conn,
                    "SELECT COUNT(*) FROM metric_formula_lib WHERE dataset_id IS NULL",
                )
                == 0
            )
            got = await _scalar(
                conn, "SELECT dataset_id FROM metric_formula_lib WHERE is_deleted=0"
            )
            assert got == expected_dsid

            # 数据集不存在 → 指名道姓 400，不静默
            await staging_repo.insert_row(
                conn,
                "staging_metric_caliber",
                {
                    "domain_id": dom_id,
                    "domain_name": "销售",
                    "dataset_id": None,
                    "dataset_name": "ghost_ds",
                    "metric_name": "销售额",
                    "expr": "SUM(x)",
                    "time_window": "7d",
                    "additivity": "additive",
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await conn.commit()
            import pytest
            from semantic_config.errors import BadRequestError

            with pytest.raises(BadRequestError):
                await promote._complete_metric_manage(conn, ds, "METRIC_MANAGE", admin)
            await conn.close()
        finally:
            settings.db_url = originals
            Path(tmp_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_caliber_stage_excel_roundtrip_preserves_identity_columns():
    """缺陷 C/D 回归：DIMENSION_CALIBER 导出→原样上传，dataset_id 与
    column_role（date 身份）必须原样保住 —— upload 是覆盖重灌，白名单外
    的列会被洗成 NULL。"""

    async def _run():
        settings, originals, tmp_path = _isolated_db()
        try:
            await init_db()
            await init_probe_db()
            conn = await _open_conn()
            admin = _principal()
            ds = "mg087-cal-roundtrip"
            from profiling.services import upload_service
            from profiling.stages import def_by_stage
            from profiling.repositories import probe_stage_repo

            await conn.execute(
                text(
                    "INSERT INTO datasource (datasource_id, datasource_name, datasource_type,"
                    " config, tenant_id, workspace_id, is_deleted)"
                    f" VALUES ('{ds}', 'x', 'MYSQL', '{{}}', 't1', 'w1', 0)"
                )
            )
            await probe_stage_repo.ensure_stages(
                conn,
                ds,
                tenant_id=admin.tenant_id,
                workspace_id=admin.active_workspace_id,
                created_by=admin.user_id,
            )
            sdef = def_by_stage("DIMENSION_CALIBER")
            common = {
                "datasource_id": ds,
                "tenant_id": admin.tenant_id,
                "workspace_id": admin.active_workspace_id,
                "created_by": admin.user_id,
                "task_id": None,
            }
            await staging_repo.insert_row(
                conn,
                sdef.table,
                {
                    "domain_id": 7,
                    "domain_name": "销售",
                    "dataset_id": 42,
                    "dataset_name": "orders",
                    "dimension_name": "日期",
                    "expr": "DATE(order_ts)",
                    "value_example": None,
                    "column_role": "date",
                    "column_name": "order_ts",
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await conn.commit()

            exported = await upload_service.export_stage(conn, sdef, ds, admin)
            result = await upload_service.upload_stage(
                conn, sdef, ds, "dimension-caliber.xlsx", exported, admin
            )
            await conn.commit()
            assert result["success"] is True
            rows = await staging_repo.list_all(
                conn, sdef.table, ds, admin.tenant_id, admin.active_workspace_id
            )
            assert len(rows) == 1
            row = rows[0]
            assert row["dataset_id"] == 42, "dataset_id 在往返中被洗掉（缺陷 C）"
            assert row["column_role"] == "date", "column_role 在往返中被洗掉（缺陷 D）"
            assert row["column_name"] == "order_ts", "血缘列 column_name 在往返中被洗掉"
            assert row["domain_id"] == 7
            await conn.close()
        finally:
            settings.db_url = originals
            Path(tmp_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_promote_rejects_blank_name_keys():
    """缺陷 F 回归：名称键列（NOT NULL 去重自然键）NULL/空白必须 400 指名道姓 ——
    NULL 打穿到库是 IntegrityError 500 丢上下文，空白落成 '' 是无名实体占键。"""

    async def _run():
        settings, originals, tmp_path = _isolated_db()
        try:
            await init_db()
            await init_probe_db()
            conn = await _open_conn()
            admin = _principal()
            ds = "mg087-blank-name"
            common = {
                "datasource_id": ds,
                "tenant_id": admin.tenant_id,
                "workspace_id": admin.active_workspace_id,
                "created_by": admin.user_id,
                "task_id": None,
            }
            import pytest
            from semantic_config.errors import BadRequestError

            # 数据集名空白（模拟上传 '   ' 被 _coerce 归 ''）→ 400 非静默无名实体
            await staging_repo.insert_row(
                conn,
                "staging_dataset",
                {
                    "domain_id": 1,
                    "domain_name": "销售",
                    "dataset_name": "",
                    "description": "d",
                    "source": "public.orders",
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await conn.commit()
            with pytest.raises(BadRequestError):
                await promote._complete_dataset(conn, ds, "DATASET", admin)

            # 维度名 NULL（管理阶段无候选态门槛，PENDING 也晋升）→ 400 非 500
            await staging_repo.insert_row(
                conn,
                "staging_dimension",
                {
                    "domain_id": 1,
                    "domain_name": "销售",
                    "dimension_name": None,
                    "description": "d",
                },
                **common,
            )
            await conn.commit()
            with pytest.raises(BadRequestError):
                await promote._complete_dimension_manage(
                    conn, ds, "DIMENSION_MANAGE", admin
                )
            await conn.close()
        finally:
            settings.db_url = originals
            Path(tmp_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_promote_reresolves_dangling_dataset_id():
    """缺陷 G 回归：staging 带来的 dataset_id 悬空（数据集删除重建 id 变更）时，
    promote 必须回落名称解析写真实 id —— 盲信悬空 id 是「写得进读不出」。"""

    async def _run():
        settings, originals, tmp_path = _isolated_db()
        try:
            await init_db()
            await init_probe_db()
            conn = await _open_conn()
            admin = _principal()
            ds = "mg087-dangling"
            common = {
                "datasource_id": ds,
                "tenant_id": admin.tenant_id,
                "workspace_id": admin.active_workspace_id,
                "created_by": admin.user_id,
                "task_id": None,
            }
            await conn.execute(
                text(
                    "INSERT INTO domain (domain_name, tenant_id, workspace_id, is_deleted)"
                    " VALUES ('销售', 't1', 'w1', 0)"
                )
            )
            dom_id = await _scalar(
                conn, "SELECT id FROM domain WHERE domain_name='销售'"
            )
            await conn.execute(
                text(
                    "INSERT INTO dataset (datasource_id, domain_id, dataset_name,"
                    " source, tenant_id, workspace_id, is_deleted)"
                    f" VALUES ('{ds}', {dom_id}, 'orders', 'public.orders', 't1', 'w1', 0)"
                )
            )
            real_id = await _scalar(
                conn, "SELECT id FROM dataset WHERE dataset_name='orders'"
            )

            await staging_repo.insert_row(
                conn,
                "staging_metric",
                {
                    "domain_id": dom_id,
                    "domain_name": "销售",
                    "metric_name": "销售额",
                    "description": "d",
                    "synonyms": None,
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await staging_repo.insert_row(
                conn,
                "staging_metric_caliber",
                {
                    "domain_id": dom_id,
                    "domain_name": "销售",
                    "dataset_id": real_id + 9999,  # 悬空 id，名称可解析
                    "dataset_name": "orders",
                    "metric_name": "销售额",
                    "expr": "SUM(amt)",
                    "time_window": "1d",
                    "additivity": "additive",
                },
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await conn.commit()
            r = await promote._complete_metric_manage(conn, ds, "METRIC_MANAGE", admin)
            await conn.commit()
            assert r["promoted_metric_caliber"] == 1
            got = await _scalar(
                conn, "SELECT dataset_id FROM metric_formula_lib WHERE is_deleted=0"
            )
            assert got == real_id, f"悬空 id 未被回落名称解析纠正：{got}"
            await conn.close()
        finally:
            settings.db_url = originals
            Path(tmp_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_caliber_lineage_falls_back_to_sql_content():
    """缺陷 H 回归：SQL 派生数据集经 DB 事实往返读回后 parents=None ——
    CALIBER 血缘必须从 sql_content 解析回退，否则日志证据被整体滤掉。"""
    import pandas as pd
    from profiling.algo.evidence import TableEvidence
    from profiling.algo.handlers.caliber import CaliberHandler
    from profiling.algo.snapshots import TableSnapshot

    meta = pd.DataFrame(
        [
            {
                "table_name": "revenue_view",
                "table_comment": None,
                "parents": None,
                "sql_content": "SELECT o.amt, u.region FROM dwd.orders o JOIN dwd.users u ON o.uid=u.id",
            },
            {
                "table_name": "orders",
                "table_comment": None,
                "parents": "orders",
                "sql_content": None,
            },
        ]
    )
    working = TableEvidence(snapshot=TableSnapshot.build(meta))
    handler = CaliberHandler(None, dialect="postgres")
    lineage = handler._parent_lineage(working, {"revenue_view", "orders"})
    assert lineage.get("orders") is not None and "revenue_view" in lineage["orders"], (
        f"sql_content 血缘回退失败: {lineage}"
    )
    assert "users" in lineage and lineage["users"] == ["revenue_view"]
    # 直通表 parents 直读，恒等映射不受影响
    assert "orders" in lineage and "orders" in lineage["orders"]


def test_passthrough_dataset_source_uses_physical_full_name():
    """L2.source / P2.table_name 契约：直通数据集的 source 必须是物理**全名**
    （PG=schema.table，BQ=table_fullname）—— weave 用 (source, field_name)
    去 P3 富化样例值，短名对不上就静默落空。sqlite 无 schema 保持裸名。"""
    from types import SimpleNamespace
    import pandas as pd
    from profiling.algo.handlers.dataset import DatasetHandler
    from profiling.algo.snapshots import TableSnapshot

    meta = pd.DataFrame(
        [
            {"table_name": "orders", "table_comment": None, "table_schema": "public"},
            {
                "table_name": "ga_sessions_*",
                "table_comment": None,
                "table_fullname": "bigquery-public-data.google_analytics_sample.ga_sessions_*",
            },
            {"table_name": "tracks", "table_comment": None},
        ]
    )
    cols = {
        n: pd.DataFrame(
            [
                {
                    "table_name": n,
                    "column_name": "id",
                    "data_type": "int",
                    "is_primary": "Y",
                    "is_nullable": "N",
                    "column_comment": None,
                }
            ]
        )
        for n in ("orders", "ga_sessions_*", "tracks")
    }
    evidence = SimpleNamespace(
        tables=SimpleNamespace(snapshot=TableSnapshot.build(meta, cols)),
        domains=SimpleNamespace(domain_of=lambda name: "sales"),
    )
    handler = DatasetHandler(None, derived_from_tables_include_patterns=("%",))
    rows, _ = handler._derived_from_tables(evidence, set())
    source = {r["table_name"]: r["source"] for r in rows}
    assert source["orders"] == "public.orders"
    assert (
        source["ga_sessions_*"]
        == "bigquery-public-data.google_analytics_sample.ga_sessions_*"
    )
    assert source["tracks"] == "tracks"
    # 血缘列仍是短名（算法内部证据按短名键控）
    assert {r["parents"] for r in rows} == {"orders", "ga_sessions_*", "tracks"}

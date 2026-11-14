"""Excel 导入链 · 12-sheet 新契约（规范 v2.1，卡片 mgw2-import）。

覆盖：
- A1-A5 原批量语义（整份文件父子 id 串接、重导走 update 不插重、同 sheet 自然键
  后写覆盖、insert/update 混合、引用缺失全量报错+整体回滚）——契约换 12-sheet；
- table sheet 校验+暂存（S1：门控 P3 引用与 L2 表式 source）与 physical_join 跳过（S2）；
- domain 映射（S3：模板 domain_id→存储 domain_name 自然键，模板 domain_name→display_name）；
- 横切列（source_channel/updated_at）仅 L8 落库（⑥a）；
- 布尔 1/0→'Y'/'N'（④a）、is_default 空→NULL（⑦a）、多值 $$$ 原样存（D1/约束 4）；
- 12 实体重构（SPEC 00 §12.2）：落库表为 domain / dataset / dimension_dict / metric_dict，
  数据集数据源列名 datasource_id 且 upsert 键为 (datasource_id, dataset_name)，导入末尾
  统一重算 domain.datasource_scope（§4.2）。

列契约唯一权威源：`semantic_config.template_contract`（Task A 交付）。
"""

from __future__ import annotations

import io
from typing import Any

from qwenpaw_data_auth import Principal
from openpyxl import Workbook
from sqlalchemy import func, insert, select, update

from semantic_config.config import get_settings
from semantic_config.db import init_db, now_iso
from semantic_config.engine import get_session_factory
from semantic_config.services import excel_import_service
from semantic_config.tables import (
    dataset,
    dataset_dimension,
    dataset_field,
    datasource,
    dimension_dict,
    domain,
    metric_caliber,
    metric_dict,
    physical_join,
    physical_table,
    table_column,
    verified_query,
)


def _principal() -> Principal:
    return Principal(
        user_id="alice",
        tenant_id="tenant-a",
        workspace_ids=["ws-a"],
        active_workspace_id="ws-a",
        role="tenant_admin",
    )


def _xlsx(sheets: dict[str, tuple[list[str], list[list[Any]]]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, (header, rows) in sheets.items():
        ws = wb.create_sheet(title=name)
        ws.append(header)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# header 逐列取自 template_contract 业务列（12-sheet 新契约）。
# 各 sheet 里的 datasource_id / dataset.source_datasource 是旧模板残留列：归属数据源一律
# 由请求参数绑定，导入侧把它们抹掉后统一改写（SPEC 00 §12.2），此处保留以覆盖该行为。
HDR = {
    "datasource": [
        "datasource_id",
        "datasource_name",
        "datasource_type",
        "description",
    ],
    "table": ["datasource_id", "table_name", "table_comment", "partition_keys"],
    "table_column": [
        "datasource_id",
        "table_name",
        "column_name",
        "data_type",
        "is_primary",
        "is_nullable",
        "column_comment",
        "sample_values",
    ],
    "physical_join": [
        "datasource_id",
        "left_table",
        "left_columns",
        "right_table",
        "right_columns",
        "join_type",
    ],
    "domain": ["domain_id", "domain_name", "description", "datasource_scope"],
    "dataset": [
        "domain_id",
        "dataset_name",
        "description",
        "source_datasource",
        "source",
    ],
    "dataset_field": [
        "domain_id",
        "dataset_name",
        "field_name",
        "description",
        "data_type",
        "is_primary",
        "is_nullable",
    ],
    "dimension": [
        "domain_id",
        "dimension_name",
        "description",
        "synonyms",
        "dimension_role",
        "child_dimensions",
        "priority",
    ],
    "dimension_binding": [
        "domain_id",
        "dimension_name",
        "dataset_name",
        "expr",
        "value_example",
    ],
    "metric": ["domain_id", "metric_name", "description", "synonyms", "priority"],
    "metric_caliber": [
        "domain_id",
        "metric_name",
        "dataset_name",
        "expr",
        "time_window",
        "additivity",
        "is_default",
    ],
    "verified_query": ["domain_id", "question", "verified_sql"],
}


def _full_sheets(
    *,
    dataset_description: str = "评论表",
    extra_dataset_rows: list[list[Any]] | None = None,
) -> dict[str, tuple[list[str], list[list[Any]]]]:
    """12 sheet 齐全、各 1 行的最小工作簿（P2 声明 public.dws_a，L2 表式 source 指向它）。"""
    dataset_rows = [["chatapp", "dws_a", dataset_description, "pg-1", "public.dws_a"]]
    dataset_rows += extra_dataset_rows or []
    return {
        "datasource": (
            HDR["datasource"],
            [["pg-1", "PG 测试库", "postgresql", "测试库"]],
        ),
        "table": (HDR["table"], [["pg-1", "public.dws_a", "明细宽表", None]]),
        "table_column": (
            HDR["table_column"],
            [["pg-1", "public.dws_a", "user_id", "text", 1, 0, "用户", None]],
        ),
        # v2.1：physical_join 现真正落库并严格校验左右表/列声明。自连接 public.dws_a
        # 只依赖已声明的 dws_a.user_id，可在各用例覆盖 table_column 后仍恒成立。
        "physical_join": (
            HDR["physical_join"],
            [
                [
                    "pg-1",
                    "public.dws_a",
                    "user_id",
                    "public.dws_a",
                    "user_id",
                    "left_outer",
                ]
            ],
        ),
        "domain": (HDR["domain"], [["chatapp", "聊天应用", "业务域", "pg-1"]]),
        "dataset": (HDR["dataset"], dataset_rows),
        "dataset_field": (
            HDR["dataset_field"],
            [["chatapp", "dws_a", "user_id", "用户ID", "text", 1, 0]],
        ),
        "dimension": (
            HDR["dimension"],
            [["chatapp", "country", "国家", "地区", "categorical", None, None]],
        ),
        "dimension_binding": (
            HDR["dimension_binding"],
            [["chatapp", "country", "dws_a", "ip_country", "中国$$$印度"]],
        ),
        "metric": (HDR["metric"], [["chatapp", "DAU", "日活", "日活跃用户", None]]),
        "metric_caliber": (
            HDR["metric_caliber"],
            [
                [
                    "chatapp",
                    "DAU",
                    "dws_a",
                    "count(distinct user_id)",
                    "1d",
                    "additive",
                    None,
                ]
            ],
        ),
        "verified_query": (
            HDR["verified_query"],
            [["chatapp", "昨天的 DAU 多少", "select count(1) from dws_a"]],
        ),
    }


async def _setup_db(tmp_path, monkeypatch, *datasource_ids: str) -> None:
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.setattr(
        get_settings(),
        "db_url",
        "sqlite+aiosqlite:///" + str(tmp_path / "semantic_config.db"),
    )
    await init_db()
    # SPEC 01：导入前数据源须已存在（Excel 内不再新建数据源）。
    await _seed_datasources(*(datasource_ids or ("pg-1",)))


async def _seed_datasources(*datasource_ids: str) -> None:
    session = get_session_factory()()
    try:
        ts = now_iso()
        for code in datasource_ids:
            await session.execute(
                insert(datasource).values(
                    datasource_id=code,
                    datasource_name=code,
                    datasource_type="postgresql",
                    config="{}",
                    tenant_id="tenant-a",
                    workspace_id="ws-a",
                    created_by="alice",
                    updated_by="alice",
                    created_at=ts,
                    updated_at=ts,
                )
            )
        await session.commit()
    finally:
        await session.close()


async def _import(content: bytes, datasource_id: str = "pg-1") -> dict:
    """SPEC 01：归属数据源来自请求参数，工作簿内的数据源列一律忽略。"""
    session = get_session_factory()()
    try:
        return await excel_import_service.import_excel(
            session, "semantic.xlsx", content, _principal(), datasource_id=datasource_id
        )
    finally:
        await session.close()


async def _count(table) -> int:
    session = get_session_factory()()
    try:
        res = await session.execute(select(func.count()).select_from(table))
        return int(res.scalar_one())
    finally:
        await session.close()


async def _one(table):
    """取表中唯一一行（断言前置：调用方已确认只有一行）。"""
    session = get_session_factory()()
    try:
        res = await session.execute(select(table))
        return res.mappings().one()
    finally:
        await session.close()


async def _rows(table, order_by=None):
    session = get_session_factory()()
    try:
        stmt = select(table)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        res = await session.execute(stmt)
        return res.mappings().all()
    finally:
        await session.close()


# ---------------- A1-A5：原批量语义在新契约下保持 ----------------


async def test_import_rejects_numeric_dimension_priority(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch)
    sheets = _full_sheets()
    sheets["dimension"][1][0][-1] = 50
    del sheets["dimension_binding"]

    result = await _import(_xlsx(sheets))

    assert result["success"] is False
    [error] = result["errors"]
    assert error["sheet"] == "dimension"
    assert error["row"] == 2
    assert "priority" in error["message"]
    assert await _count(dataset) == 0


async def test_batch_import_wires_parent_ids_across_sheets(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch)

    result = await _import(_xlsx(_full_sheets()))

    assert result["success"] is True, result
    assert result["errors"] == []
    # summary 键 = 契约正名 sheet（SPEC 01：datasource 由请求参数绑定不入 summary；
    # domain→biz_domain / dataset_field→dataset_column / dimension_binding→dataset_dimension /
    # metric_caliber→metric_formula 归一到正名）。
    assert result["summary"] == {
        "biz_domain": 1,
        "table": 1,
        "table_column": 1,
        "physical_join": 1,
        "dataset": 1,
        "dataset_column": 1,
        "dimension": 1,
        "dataset_dimension": 1,
        "metric": 1,
        "metric_formula": 1,
        "verified_query": 1,
    }

    domain_row = await _one(domain)
    dataset_row = await _one(dataset)
    field_row = await _one(dataset_field)
    dimension_row = await _one(dimension_dict)
    binding_row = await _one(dataset_dimension)
    metric_row = await _one(metric_dict)
    caliber_row = await _one(metric_caliber)
    column_row = await _one(table_column)
    vq_row = await _one(verified_query)

    # v2.1：domain sheet 的 domain_name 列 → domain.domain_name（自然键）；
    # domain_id 列 → domain.import_id（跨表关联稳定引用键，display_name 已退役）
    assert domain_row["domain_name"] == "聊天应用"
    assert domain_row["import_id"] == "chatapp"
    # 12 实体重构：业务域已与数据源解耦，datasource_id 列退役、改为派生缓存 datasource_scope
    assert "datasource_id" not in domain_row

    # 批量写入后回读的映射必须把子行挂到正确的父 id 上
    assert dataset_row["domain_id"] == domain_row["id"]
    assert (
        dataset_row["datasource_id"] == "pg-1"
    )  # 12 实体重构：数据集数据源列名 datasource_id
    assert dataset_row["source"] == "public.dws_a"
    # 维度/指标字典是域级对象：挂 domain_id、不挂 datasource_id
    assert dimension_row["domain_id"] == domain_row["id"]
    assert metric_row["domain_id"] == domain_row["id"]
    assert "datasource_id" not in dimension_row
    assert "datasource_id" not in metric_row
    assert field_row["dataset_id"] == dataset_row["id"]
    assert field_row["domain_id"] == domain_row["id"]
    assert binding_row["dataset_id"] == dataset_row["id"]
    assert binding_row["dimension_id"] == dimension_row["id"]
    assert binding_row["domain_id"] == domain_row["id"]
    assert caliber_row["metric_id"] == metric_row["id"]
    assert caliber_row["dataset_id"] == dataset_row["id"]
    assert caliber_row["domain_id"] == domain_row["id"]
    assert vq_row["domain_id"] == domain_row["id"]

    # table_column 已删 table_name，改按 table_id 绑定 physical_table（(table_id, column_name) 自然键）
    table_row = await _one(physical_table)
    assert table_row["table_name"] == "public.dws_a"
    assert column_row["datasource_id"] == "pg-1"
    assert column_row["table_id"] == table_row["id"]

    # 转换规则：模板布尔 1/0 → 存储 'Y'/'N'（④a）；多值原样；is_default 空→NULL（⑦a）
    assert column_row["is_primary"] == "Y"
    assert column_row["is_nullable"] == "N"
    assert field_row["is_primary"] == "Y"
    assert field_row["is_nullable"] == "N"
    assert binding_row["value_example"] == "中国$$$印度"
    assert caliber_row["time_window"] == "1d"
    assert caliber_row["is_default"] is None
    # v2.1：priority 存储统一大写，默认 MEDIUM
    assert metric_row["priority"] == "MEDIUM"
    # v2.1：verified_query 已删 source 列（横切列不再落库）
    assert "source" not in vq_row

    # 审计列
    assert dataset_row["tenant_id"] == "tenant-a"
    assert dataset_row["workspace_id"] == "ws-a"
    assert dataset_row["created_by"] == "alice"


async def test_reimport_updates_in_place_without_duplicating(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch)

    first = await _import(_xlsx(_full_sheets()))
    assert first["success"] is True, first
    original = await _one(dataset)

    second = await _import(_xlsx(_full_sheets(dataset_description="改过的注释")))
    assert second["success"] is True, second

    for table in (
        datasource,
        domain,
        dataset,
        table_column,
        dataset_field,
        dimension_dict,
        dataset_dimension,
        metric_dict,
        metric_caliber,
        verified_query,
    ):
        assert await _count(table) == 1, table.name

    updated = await _one(dataset)
    assert updated["id"] == original["id"]
    assert updated["description"] == "改过的注释"
    assert updated["created_at"] == original["created_at"]
    assert updated["updated_by"] == "alice"


async def test_duplicate_natural_key_in_one_sheet_keeps_last_row(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets(
        extra_dataset_rows=[
            ["chatapp", "dws_a", "后一行覆盖", "pg-1", "select 2"],
        ],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is True, result
    # 文件内两行同自然键（同数据源同名）：只落一行，后写覆盖（与逐行 upsert 等价）
    assert await _count(dataset) == 1
    assert result["summary"]["dataset"] == 2
    row = await _one(dataset)
    assert row["description"] == "后一行覆盖"
    assert row["source"] == "select 2"


async def test_one_import_mixes_insert_and_update_rows(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch)

    assert (await _import(_xlsx(_full_sheets())))["success"] is True
    original = await _one(dataset)

    # 第二次导入：已存在的 dws_a 改注释（走 update），同时新增 dws_b（走 insert）
    sheets = _full_sheets(
        dataset_description="改过的注释",
        extra_dataset_rows=[["chatapp", "dws_b", "新增表", "pg-1", "select 3"]],
    )
    result = await _import(_xlsx(sheets))
    assert result["success"] is True, result

    rows = await _rows(dataset, order_by=dataset.c.dataset_name)
    assert [r["dataset_name"] for r in rows] == ["dws_a", "dws_b"]
    assert rows[0]["id"] == original["id"]
    assert rows[0]["description"] == "改过的注释"
    assert rows[1]["description"] == "新增表"
    assert rows[1]["domain_id"] == rows[0]["domain_id"]


async def test_missing_reference_reports_every_bad_row_and_rolls_back(
    tmp_path, monkeypatch
):
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets()
    # dataset 两行都引用不存在的业务域；dataset 之后的依赖 sheet 因此也解析不到数据集
    sheets["dataset"] = (
        HDR["dataset"],
        [
            ["NoSuchDomain", "dws_a", "坏行 1", "pg-1", "public.dws_a"],
            ["NoSuchDomain", "dws_b", "坏行 2", "pg-1", "select 2"],
        ],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is False
    assert result["summary"] == {}
    bad_dataset_rows = [e for e in result["errors"] if e["sheet"] == "dataset"]
    # 不再只报第一条：两行都要出现，且带上 Excel 行号
    assert [e["row"] for e in bad_dataset_rows] == [2, 3]
    assert all("NoSuchDomain" in e["message"] for e in bad_dataset_rows)
    # 级联：依赖 dws_a 的 sheet 也逐行报错（全量收集）
    assert {e["sheet"] for e in result["errors"]} >= {
        "dataset",
        "dataset_field",
        "dimension_binding",
        "metric_caliber",
    }

    # 整体回滚：先前 sheet 已写入的行也不得留下（datasource 为导入前预置，不参与回滚）
    for table in (domain, dataset, table_column, metric_dict):
        assert await _count(table) == 0, table.name
    assert await _count(datasource) == 1  # SPEC 01：预置数据源保留


# ---------------- 新契约行为（S1/S2/S3/④a/⑥a/⑦a/D1） ----------------


async def test_table_sheet_gates_table_column_and_table_source_dataset(
    tmp_path, monkeypatch
):
    """S1：本次含 table sheet 时，P3 引用与 L2 表式 source 必须已声明。"""
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets()
    sheets["table_column"] = (
        HDR["table_column"],
        [["pg-1", "public.nope", "user_id", "text", 1, 0, None, None]],
    )
    sheets["dataset"] = (
        HDR["dataset"],
        [["chatapp", "dws_a", "评论表", "pg-1", "public.nope2"]],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is False
    tc_errors = [e for e in result["errors"] if e["sheet"] == "table_column"]
    ds_errors = [e for e in result["errors"] if e["sheet"] == "dataset"]
    assert len(tc_errors) == 1 and "public.nope" in tc_errors[0]["message"]
    assert len(ds_errors) == 1 and "public.nope2" in ds_errors[0]["message"]
    # 整体回滚
    assert await _count(table_column) == 0
    assert await _count(dataset) == 0


async def test_table_sheet_absent_skips_p2_validation(tmp_path, monkeypatch):
    """S1：部分导入（无 table sheet）时不对 L2 表式 source 做 P2 声明校验。

    v2.1：table_column/physical_join 均以 table_id 绑定 physical_table，无 table sheet
    时无法声明物理表 → 这两张 sheet 不能单独出现；因此本用例移除它们，只验证
    「无 table sheet 时表式 source 不被门控」这一部分导入语义。
    """
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets()
    del sheets["table"]
    del sheets["table_column"]
    del sheets["physical_join"]
    sheets["dataset"] = (
        HDR["dataset"],
        [["chatapp", "dws_a", "评论表", "pg-1", "public.absent2"]],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is True, result
    assert await _count(table_column) == 0
    assert (await _one(dataset))["source"] == "public.absent2"


async def test_physical_join_validated_and_stored(tmp_path, monkeypatch):
    """S2（v2.1）：physical_join 现严格校验左右表/列声明并真正落库。

    左右表名 → physical_table.id、左右列名 → table_column.id 反解后入库。此处声明
    两张表两列，验证跨表 join 行成功落库。"""
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets()
    # 声明第二张表 dim_user 及其 user_id 列，供 physical_join 右侧引用。
    sheets["table"] = (
        HDR["table"],
        [
            ["pg-1", "public.dws_a", "明细宽表", None],
            ["pg-1", "public.dim_user", "用户维表", None],
        ],
    )
    sheets["table_column"] = (
        HDR["table_column"],
        [
            ["pg-1", "public.dws_a", "user_id", "text", 1, 0, "用户", None],
            ["pg-1", "public.dim_user", "user_id", "text", 1, 0, "用户", None],
        ],
    )
    sheets["physical_join"] = (
        HDR["physical_join"],
        [
            [
                "pg-1",
                "public.dws_a",
                "user_id",
                "public.dim_user",
                "user_id",
                "left_outer",
            ]
        ],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is True, result
    assert result["summary"]["physical_join"] == 1
    # v2.1：physical_join 落库一行，左右表 id 分别指向两张 physical_table
    join_row = await _one(physical_join)
    tables = {t["table_name"]: t["id"] for t in await _rows(physical_table)}
    assert join_row["left_table_id"] == tables["public.dws_a"]
    assert join_row["right_table_id"] == tables["public.dim_user"]
    assert join_row["join_type"] == "left_outer"
    assert await _count(dataset) == 1


async def test_unknown_meta_and_legacy_sheets_skipped(tmp_path, monkeypatch):
    """S9：_meta/未知 sheet/旧 8-sheet 名一律跳过（旧格式零兼容）。"""
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets()
    sheets["_meta"] = (["key", "value"], [["datasource_code", "pg-1"]])
    # 旧 8-sheet/已删表名（如 values）+ 任意未知 sheet 一律跳过（旧格式零兼容）。
    sheets["values"] = (["datasource_id", "dimension_name"], [["pg-1", "旧格式维值"]])
    sheets["legacy_sheet"] = (["a"], [[1]])
    result = await _import(_xlsx(sheets))

    assert result["success"] is True, result
    assert set(result["summary"]) == {
        "biz_domain",
        "table",
        "table_column",
        "physical_join",
        "dataset",
        "dataset_column",
        "dimension",
        "dataset_dimension",
        "metric",
        "metric_formula",
        "verified_query",
    }
    # 未知 sheet 未污染：业务域仍只来自新契约 domain sheet
    assert (await _one(domain))["domain_name"] == "聊天应用"


async def test_cross_cutting_columns_ignored(tmp_path, monkeypatch):
    """⑥a/约束 5（v2.1）：横切列 source_channel/updated_at 一律忽略、不落库、不报错。

    verified_query 已删 source 列，横切列不再有任何落点；各 sheet 带上这些列时应被
    静默忽略，实体照常入库。"""
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets()
    sheets["domain"] = (
        HDR["domain"] + ["source_channel", "updated_at"],
        [
            [
                "chatapp",
                "聊天应用",
                "业务域",
                "pg-1",
                "human",
                "2026-08-20T00:00:00",
            ]
        ],
    )
    sheets["metric"] = (
        HDR["metric"] + ["source_channel", "updated_at"],
        [
            [
                "chatapp",
                "DAU",
                "日活",
                "日活跃用户",
                None,
                "profiling",
                "2026-08-20T00:00:00",
            ]
        ],
    )
    sheets["verified_query"] = (
        HDR["verified_query"] + ["source_channel", "updated_at"],
        [
            [
                "chatapp",
                "昨天的 DAU 多少",
                "select count(1) from dws_a",
                "feedback",
                "2026-08-20T00:00:00",
            ]
        ],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is True, result
    vq_row = await _one(verified_query)
    assert vq_row["question"] == "昨天的 DAU 多少"
    assert "source" not in vq_row  # 横切列不再落库（source 列已删）
    assert await _count(metric_dict) == 1  # metric 的横切列被忽略且不报错
    assert (await _one(domain))["domain_name"] == "聊天应用"


async def test_missing_required_header_column_reports_sheet_level_error(
    tmp_path, monkeypatch
):
    """S8：header 缺必填列 = sheet 级错误（row=1），参与整体回滚。"""
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets()
    sheets["metric"] = (
        ["domain_id", "description"],
        [["chatapp", "日活"]],
    )  # 缺 metric_name
    result = await _import(_xlsx(sheets))

    assert result["success"] is False
    assert result["summary"] == {}
    metric_errors = [e for e in result["errors"] if e["sheet"] == "metric"]
    assert metric_errors == [
        {"sheet": "metric", "row": 1, "message": "缺少必填列: metric_name"}
    ]
    assert await _count(metric_dict) == 0
    assert await _count(datasource) == 1  # SPEC 01：预置数据源不参与回滚


async def test_is_default_null_when_blank_and_boolean_yn_conversions(
    tmp_path, monkeypatch
):
    """④a/⑦a：is_default 1→Integer 1、空→NULL；is_primary/is_nullable 1/0→'Y'/'N'。"""
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets()
    sheets["metric_caliber"] = (
        HDR["metric_caliber"],
        [
            [
                "chatapp",
                "DAU",
                "dws_a",
                "count(distinct user_id)",
                "1d",
                "additive",
                1,
            ],
            [
                "chatapp",
                "DAU",
                "dws_a",
                "count(distinct user_id)",
                "7d",
                "additive",
                None,
            ],
        ],
    )
    sheets["table_column"] = (
        HDR["table_column"],
        [["pg-1", "public.dws_a", "user_id", "text", 0, 1, "用户", None]],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is True, result
    calibers = await _rows(metric_caliber, order_by=metric_caliber.c.time_window)
    assert [c["time_window"] for c in calibers] == ["1d", "7d"]
    assert calibers[0]["is_default"] == 1
    assert calibers[1]["is_default"] is None
    column_row = await _one(table_column)
    assert column_row["is_primary"] == "N"
    assert column_row["is_nullable"] == "Y"


async def test_datasource_sheet_ignored_and_existing_config_untouched(
    tmp_path, monkeypatch
):
    """SPEC 01（S11）：数据源由请求参数绑定；工作簿 datasource sheet 一律忽略——
    既不新建 pg-2，也不改动既有 pg-1 的 config/名称（凭证不入 Excel）。"""
    await _setup_db(tmp_path, monkeypatch)

    # 预置数据源已由 _setup_db 插入（pg-1）；置一个非空 config，验证导入不覆盖
    session = get_session_factory()()
    try:
        await session.execute(
            update(datasource)
            .where(datasource.c.datasource_id == "pg-1")
            .values(config='{"host": "holo.example.com"}', datasource_name="旧名")
        )
        await session.commit()
    finally:
        await session.close()

    sheets = _full_sheets()
    # datasource sheet 试图改 pg-1、并新增 pg-2 —— 均应被忽略
    sheets["datasource"] = (
        HDR["datasource"],
        [
            ["pg-1", "PG 测试库", "postgresql", "测试库"],
            ["pg-2", "第二库", "postgresql", None],
        ],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is True, result
    rows = await _rows(datasource, order_by=datasource.c.datasource_id)
    assert [r["datasource_id"] for r in rows] == ["pg-1"]  # pg-2 未被创建
    assert rows[0]["config"] == '{"host": "holo.example.com"}'  # config 不被触碰
    assert rows[0]["datasource_name"] == "旧名"  # 名称也不被 Excel 改写


async def test_verified_query_upsert_key_is_domain_and_question(tmp_path, monkeypatch):
    """S12：upsert 键 = (domain_id, question)；两域同问题并存，同域同问题更新。"""
    await _setup_db(tmp_path, monkeypatch)

    assert (await _import(_xlsx(_full_sheets())))["success"] is True
    original = await _one(verified_query)

    sheets = _full_sheets()
    sheets["domain"] = (
        HDR["domain"],
        [
            ["chatapp", "聊天应用", "业务域", "pg-1"],
            ["other", "其他域", "第二个域", "pg-1"],
        ],
    )
    sheets["verified_query"] = (
        HDR["verified_query"],
        [
            ["chatapp", "昨天的 DAU 多少", "select 2"],  # 同域同问题 → update
            ["other", "昨天的 DAU 多少", "select 3"],  # 异域同问题 → insert
        ],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is True, result
    rows = await _rows(verified_query, order_by=verified_query.c.id)
    assert len(rows) == 2
    assert rows[0]["id"] == original["id"]
    assert rows[0]["verified_sql"] == "select 2"
    assert rows[1]["question"] == "昨天的 DAU 多少"
    assert rows[1]["domain_id"] != rows[0]["domain_id"]


async def test_multi_value_and_value_example_stored_verbatim(tmp_path, monkeypatch):
    """D1/约束 4：$$$ 多值与 value_example 原样存，保序不拆不重排；空值→NULL。"""
    await _setup_db(tmp_path, monkeypatch)

    sheets = _full_sheets()
    sheets["table_column"] = (
        HDR["table_column"],
        [["pg-1", "public.dws_a", "user_id", "text", 1, 0, "用户", "a$$$b$$$c"]],
    )
    sheets["dimension"] = (
        HDR["dimension"],
        [
            [
                "chatapp",
                "country",
                "国家",
                "地区$$$区域",
                "categorical",
                "province$$$city",
                None,
            ],
            ["chatapp", "city", "城市", None, "categorical", None, "high"],
        ],
    )
    sheets["dimension_binding"] = (
        HDR["dimension_binding"],
        [
            ["chatapp", "country", "dws_a", "ip_country", "俄罗斯$$$印度$$$中国"],
            ["chatapp", "city", "dws_a", "ip_city", None],  # 空 value_example → NULL
        ],
    )
    result = await _import(_xlsx(sheets))

    assert result["success"] is True, result
    assert (await _one(table_column))["sample_values"] == "a$$$b$$$c"

    dims = {d["dimension_name"]: d for d in await _rows(dimension_dict)}
    assert dims["country"]["synonyms"] == "地区$$$区域"
    assert dims["country"]["child_dimensions"] == "province$$$city"
    # v2.1：priority 存储统一大写（默认 MEDIUM，模板 high → HIGH）
    assert dims["country"]["priority"] == "MEDIUM"
    assert dims["city"]["priority"] == "HIGH"
    assert dims["city"]["synonyms"] is None

    bindings = await _rows(dataset_dimension)
    values = sorted(b["value_example"] for b in bindings if b["value_example"])
    assert values == ["俄罗斯$$$印度$$$中国"]
    assert any(b["value_example"] is None for b in bindings)


# ---------------- 12 实体重构：domain.datasource_scope 派生（SPEC 00 §4.2） ----------------


async def test_import_recomputes_domain_datasource_scope(tmp_path, monkeypatch):
    """§4.2：导入末尾统一重算 datasource_scope = 该域下未删数据集的 datasource_id 去重拼接。

    请求参数绑定 pg-2，而工作簿 domain sheet 的 datasource_scope 列写着旧值 pg-1：
    scope 只能来自数据集派生，不能是工作簿列的原样透传。两个数据集同源 → 去重后单值。
    """
    await _setup_db(tmp_path, monkeypatch, "pg-2")

    sheets = _full_sheets(
        extra_dataset_rows=[
            ["chatapp", "dws_b", "同源第二个数据集", "pg-1", "select 2"]
        ],
    )
    result = await _import(_xlsx(sheets), datasource_id="pg-2")

    assert result["success"] is True, result
    # 归属数据源一律取请求参数，工作簿内的数据源列被抹掉
    datasets = await _rows(dataset, order_by=dataset.c.dataset_name)
    assert [d["datasource_id"] for d in datasets] == ["pg-2", "pg-2"]

    domain_row = await _one(domain)
    assert [d["domain_id"] for d in datasets] == [domain_row["id"], domain_row["id"]]
    assert domain_row["datasource_scope"] == "pg-2"


async def test_reimport_moving_dataset_clears_stale_scope_on_old_domain(
    tmp_path, monkeypatch
):
    """数据集改挂业务域时，**原**域的 datasource_scope 也要重算，不能残留旧值。

    原域在二次导入的工作簿里已不出现（不进 maps.domain），靠写前快照的归属域补进
    重算集合；否则原域会一直"看起来"还含本数据源，读侧的入围域集合就会多带一个域。
    """
    await _setup_db(tmp_path, monkeypatch)
    assert (await _import(_xlsx(_full_sheets())))["success"] is True

    old_domain = await _one(domain)
    assert old_domain["datasource_scope"] == "pg-1"

    # 二次导入：同名数据集 dws_a 改挂到新业务域 other，工作簿里不再出现原域。
    moved = {
        "domain": (HDR["domain"], [["other", "另一个域", "业务域", None]]),
        "dataset": (HDR["dataset"], [["other", "dws_a", "评论表", "pg-1", "select 1"]]),
    }
    assert (await _import(_xlsx(moved)))["success"] is True

    rows = await _rows(domain, order_by=domain.c.domain_name)
    scope_by_name = {r["domain_name"]: r["datasource_scope"] for r in rows}
    assert scope_by_name["另一个域"] == "pg-1"
    assert scope_by_name["聊天应用"] == "", scope_by_name


async def test_caliber_rows_reject_cross_domain_binding(tmp_path, monkeypatch):
    """§9-Q7：口径行的数据集与所引业务域必须同域，跨域绑定报行错误并整体回滚。

    数据集唯一键已改为"同数据源内名唯一"、不再按域解析，因此必须显式挡住这种组合，
    否则口径会把 A 域的字典绑到 B 域的数据集上。
    """
    await _setup_db(tmp_path, monkeypatch)
    assert (await _import(_xlsx(_full_sheets())))["success"] is True
    baseline = await _count(dataset_dimension)

    # other 域下引用 chatapp 域的数据集 dws_a：dataset 解析得到，但归属域不匹配。
    cross = {
        "domain": (HDR["domain"], [["other", "另一个域", "业务域", None]]),
        "dimension": (
            HDR["dimension"],
            [["other", "country", "国家", None, "categorical", None, None]],
        ),
        "dimension_binding": (
            HDR["dimension_binding"],
            [["other", "country", "dws_a", "ip_country", None]],
        ),
    }
    result = await _import(_xlsx(cross))

    assert result["success"] is False
    assert len(result["errors"]) == 1, result["errors"]
    err = result["errors"][0]
    # 注：_prep_dimension_binding 报错用的是旧别名 dimension_binding，而同 sheet 的必填列
    # 校验报的是正名 dataset_dimension——既有的标签不一致，此处按现状断言。
    assert err["sheet"] == "dimension_binding"
    assert "业务域不一致" in err["message"], err
    # 整体回滚：失败批次不留下任何新绑定，也没新建业务域。
    assert await _count(dataset_dimension) == baseline
    assert {r["domain_name"] for r in await _rows(domain)} == {"聊天应用"}

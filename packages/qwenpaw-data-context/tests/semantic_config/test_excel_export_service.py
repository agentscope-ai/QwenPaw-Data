"""导出服务 round-trip：确认 ``excel_export_service`` 覆盖导入契约的全部 11 sheet。

重点验证此前被导成空表的 4 个 sheet 在导出面被完整还原：
- 物理层 table / table_column / physical_join（库存 id → 导出反解回名称）；
- verified_query（按 domain_name 引用业务域，round-trip 无损）；
并顺带核对布尔列 'Y'/'N' → 模板面 1/0（④a）与 $$$ 多值反解保序。

另盯住 12 实体重构后的 by-datasource 取数口径（SPEC 00 §12.3）——域与字典都不再
直挂数据源，导出范围改为派生推导，故额外验证：
- 逻辑层各 sheet 也是回灌不动点；
- 跨数据源共享的业务域只带出本数据源那部分数据集/口径；
- 本数据源下无任何口径的孤立字典不导出。
"""

from __future__ import annotations

import io
from typing import Any

from openpyxl import Workbook, load_workbook
from qwenpaw_data_auth import Principal
from sqlalchemy import insert, select, update

from semantic_config.config import get_settings
from semantic_config.db import init_db, now_iso
from semantic_config.engine import get_session_factory
from semantic_config.services import excel_export_service, excel_import_service
from semantic_config.tables import datasource, dimension_dict


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


async def _setup_db(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.setattr(
        get_settings(),
        "db_url",
        "sqlite+aiosqlite:///" + str(tmp_path / "semantic_config.db"),
    )
    await init_db()
    session = get_session_factory()()
    try:
        ts = now_iso()
        await session.execute(
            insert(datasource).values(
                datasource_id="pg-1",
                datasource_name="pg-1",
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


async def _import(content: bytes) -> dict:
    session = get_session_factory()()
    try:
        return await excel_import_service.import_excel(
            session, "semantic.xlsx", content, _principal(), datasource_id="pg-1"
        )
    finally:
        await session.close()


async def _export() -> Workbook:
    session = get_session_factory()()
    try:
        content = await excel_export_service.export_excel(session, "pg-1", _principal())
    finally:
        await session.close()
    return load_workbook(io.BytesIO(content), data_only=True)


def _rows(ws) -> tuple[list[str], list[dict[str, Any]]]:
    data = list(ws.iter_rows(values_only=True))
    header = [str(h) if h is not None else "" for h in data[0]]
    out = []
    for raw in data[1:]:
        if raw is None or all(c is None for c in raw):
            continue
        out.append(
            {header[i]: (raw[i] if i < len(raw) else None) for i in range(len(header))}
        )
    return header, out


def _full_workbook() -> bytes:
    return _xlsx(
        {
            # biz_domain 附带 domain_id 列 → 落 import_id，供 verified_query 新协议引用。
            "biz_domain": (
                ["domain_name", "description", "domain_id"],
                [["销售域", "销售业务域", "dom-1"]],
            ),
            "table": (
                ["table_name", "table_comment", "partition_keys"],
                [
                    ["public.orders", "订单表", "ds$$$hr"],
                    ["public.users", "用户表", None],
                ],
            ),
            "table_column": (
                [
                    "table_name",
                    "column_name",
                    "data_type",
                    "is_primary",
                    "is_nullable",
                    "column_comment",
                    "sample_values",
                ],
                [
                    ["public.orders", "order_id", "bigint", 1, 0, "主键", "1$$$2"],
                    ["public.orders", "user_id", "bigint", 0, 1, "用户", None],
                    ["public.users", "user_id", "bigint", 1, 0, "主键", None],
                ],
            ),
            "physical_join": (
                [
                    "left_table",
                    "left_columns",
                    "right_table",
                    "right_columns",
                    "join_type",
                ],
                [["public.orders", "user_id", "public.users", "user_id", "left_outer"]],
            ),
            "dataset": (
                ["domain_name", "dataset_name", "description", "source"],
                [["销售域", "订单集", "订单数据集", "SELECT 1"]],
            ),
            "dataset_column": (
                [
                    "domain_name",
                    "dataset_name",
                    "field_name",
                    "description",
                    "data_type",
                    "is_primary",
                    "is_nullable",
                ],
                [["销售域", "订单集", "amount", "金额", "bigint", 0, 1]],
            ),
            "dimension": (
                [
                    "domain_name",
                    "dimension_name",
                    "description",
                    "synonyms",
                    "dimension_type",
                    "child_dimensions",
                    "priority",
                ],
                [
                    [
                        "销售域",
                        "地区",
                        "地区维度",
                        "区域",
                        "categorical",
                        "省$$$市",
                        "HIGH",
                    ]
                ],
            ),
            "dataset_dimension": (
                [
                    "domain_name",
                    "dimension_name",
                    "dataset_name",
                    "expr",
                    "value_example",
                ],
                [["销售域", "地区", "订单集", "region_col", "北京$$$上海"]],
            ),
            "metric": (
                ["domain_name", "metric_name", "description", "synonyms", "priority"],
                [["销售域", "GMV", "成交额", "销售额", "HIGH"]],
            ),
            "metric_formula": (
                [
                    "domain_name",
                    "metric_name",
                    "dataset_name",
                    "expr",
                    "time_window",
                    "additivity",
                    "is_default",
                ],
                [["销售域", "GMV", "订单集", "sum(amount)", "1d", "ADDITIVE", 1]],
            ),
            "verified_query": (
                ["domain_id", "question", "verified_sql"],
                [["dom-1", "昨天GMV", "SELECT sum(amount) FROM 订单集"]],
            ),
        }
    )


async def test_export_covers_physical_layer_and_verified_query(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch)
    result = await _import(_full_workbook())
    assert result["success"] is True, result["errors"]

    wb = await _export()

    # table：全量表 + 注释 + $$$ 分区键
    _, tables = _rows(wb["table"])
    assert {r["table_name"] for r in tables} == {"public.orders", "public.users"}
    orders = next(r for r in tables if r["table_name"] == "public.orders")
    assert orders["table_comment"] == "订单表"
    assert orders["partition_keys"] == "ds$$$hr"

    # table_column：table_id 反解回 table_name；'Y'/'N' → 1/0
    _, cols = _rows(wb["table_column"])
    assert len(cols) == 3
    oc = next(
        r
        for r in cols
        if r["column_name"] == "order_id" and r["table_name"] == "public.orders"
    )
    assert oc["is_primary"] == 1 and oc["is_nullable"] == 0
    assert oc["sample_values"] == "1$$$2"

    # physical_join：table_id / column_id 反解回名称
    _, joins = _rows(wb["physical_join"])
    assert joins == [
        {
            "left_table": "public.orders",
            "left_columns": "user_id",
            "right_table": "public.users",
            "right_columns": "user_id",
            "join_type": "left_outer",
        }
    ]

    # verified_query：导出按 domain_name 引用（表头首列改为 domain_name）
    vq_header, vqs = _rows(wb["verified_query"])
    assert vq_header[0] == "domain_name"
    assert vqs == [
        {
            "domain_name": "销售域",
            "question": "昨天GMV",
            "verified_sql": "SELECT sum(amount) FROM 订单集",
        }
    ]


async def test_export_reimports_losslessly(tmp_path, monkeypatch):
    """导出物二次回灌 → 再导出，物理层与 verified_query 逐 sheet 相等（不动点）。"""
    await _setup_db(tmp_path, monkeypatch)
    assert (await _import(_full_workbook()))["success"] is True

    wb1 = await _export()
    buf = io.BytesIO()
    wb1.save(buf)
    assert (await _import(buf.getvalue()))["success"] is True
    wb2 = await _export()

    for sheet in ("table", "table_column", "physical_join", "verified_query"):
        assert _rows(wb1[sheet])[1] == _rows(wb2[sheet])[1], sheet


async def test_export_reimports_dimension_with_legacy_numeric_priority(
    tmp_path, monkeypatch
):
    await _setup_db(tmp_path, monkeypatch)
    assert (await _import(_full_workbook()))["success"] is True
    async with get_session_factory()() as db:
        await db.execute(update(dimension_dict).values(priority="50"))
        await db.commit()

    workbook = await _export()
    _, [dimension] = _rows(workbook["dimension"])
    assert dimension["priority"] == "NORMAL"
    async with get_session_factory()() as db:
        assert (
            await db.execute(select(dimension_dict.c.priority))
        ).scalar_one() == "50"

    content = io.BytesIO()
    workbook.save(content)
    assert (await _import(content.getvalue()))["success"] is True


async def test_logical_layer_reimports_losslessly(tmp_path, monkeypatch):
    """逻辑层同样是不动点：域/数据集/字典/口径按 §12.3 取数后二次回灌不漂移。

    12 实体重构把域与字典的取数改成了派生口径（域按 datasource_scope、字典由口径
    反推），这里盯住"派生出来的那一份仍能原样回灌"，否则导出物就不是可交付的模板。
    """
    await _setup_db(tmp_path, monkeypatch)
    assert (await _import(_full_workbook()))["success"] is True

    wb1 = await _export()
    buf = io.BytesIO()
    wb1.save(buf)
    assert (await _import(buf.getvalue()))["success"] is True
    wb2 = await _export()

    for sheet in (
        "biz_domain",
        "dataset",
        "dataset_column",
        "dimension",
        "dataset_dimension",
        "metric",
        "metric_formula",
    ):
        rows1, rows2 = _rows(wb1[sheet])[1], _rows(wb2[sheet])[1]
        assert rows1 == rows2, sheet
        assert rows1, f"{sheet} 导出为空，取数口径漏了整段"


async def _seed_second_datasource() -> None:
    session = get_session_factory()()
    try:
        ts = now_iso()
        await session.execute(
            insert(datasource).values(
                datasource_id="pg-2",
                datasource_name="pg-2",
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


async def test_export_scopes_shared_domain_to_own_datasource(tmp_path, monkeypatch):
    """业务域可跨数据源共享；导出只带本数据源那部分数据集与口径（§12.3 ②③⑤）。"""
    await _setup_db(tmp_path, monkeypatch)
    await _seed_second_datasource()
    assert (await _import(_full_workbook()))["success"] is True

    # 同一个业务域下，往 pg-2 再导一个数据集 + 一条维度口径。
    other = _xlsx(
        {
            "biz_domain": (["domain_name", "description"], [["销售域", "销售业务域"]]),
            "dataset": (
                ["domain_name", "dataset_name", "description", "source"],
                [["销售域", "退款集", "退款数据集", "SELECT 2"]],
            ),
            "dimension": (
                ["domain_name", "dimension_name", "description", "priority"],
                [["销售域", "退款原因", "退款原因维度", "LOW"]],
            ),
            "dataset_dimension": (
                [
                    "domain_name",
                    "dimension_name",
                    "dataset_name",
                    "expr",
                    "value_example",
                ],
                [["销售域", "退款原因", "退款集", "reason_col", None]],
            ),
        }
    )
    session = get_session_factory()()
    try:
        result = await excel_import_service.import_excel(
            session, "other.xlsx", other, _principal(), datasource_id="pg-2"
        )
    finally:
        await session.close()
    assert result["success"] is True, result["errors"]

    wb = await _export()  # 仍导 pg-1

    # 业务域入围（datasource_scope 同时含 pg-1/pg-2），但只出现一次。
    assert [r["domain_name"] for r in _rows(wb["biz_domain"])[1]] == ["销售域"]
    # pg-2 的数据集不串到 pg-1 的导出物里。
    assert {r["dataset_name"] for r in _rows(wb["dataset"])[1]} == {"订单集"}
    # 字典由本数据源口径反推：pg-2 上才有口径的"退款原因"不该出现。
    assert {r["dimension_name"] for r in _rows(wb["dimension"])[1]} == {"地区"}
    assert {r["dataset_name"] for r in _rows(wb["dataset_dimension"])[1]} == {"订单集"}


async def test_export_skips_dict_without_caliber_on_this_datasource(
    tmp_path, monkeypatch
):
    """§12.3 ④⑤：字典由口径反推，本数据源下无任何口径的孤立字典不导出。"""
    await _setup_db(tmp_path, monkeypatch)
    orphan = _xlsx(
        {
            "biz_domain": (["domain_name", "description"], [["销售域", "销售业务域"]]),
            "dataset": (
                ["domain_name", "dataset_name", "description", "source"],
                [["销售域", "订单集", "订单数据集", "SELECT 1"]],
            ),
            # 两个维度字典，只有"地区"有口径；"渠道"是孤立字典。
            "dimension": (
                ["domain_name", "dimension_name", "description", "priority"],
                [
                    ["销售域", "地区", "地区维度", "HIGH"],
                    ["销售域", "渠道", "渠道维度", "LOW"],
                ],
            ),
            "dataset_dimension": (
                [
                    "domain_name",
                    "dimension_name",
                    "dataset_name",
                    "expr",
                    "value_example",
                ],
                [["销售域", "地区", "订单集", "region_col", None]],
            ),
            # 指标同理：GMV 有口径，PV 没有。
            "metric": (
                ["domain_name", "metric_name", "description", "priority"],
                [
                    ["销售域", "GMV", "成交额", "HIGH"],
                    ["销售域", "PV", "浏览量", "LOW"],
                ],
            ),
            "metric_formula": (
                [
                    "domain_name",
                    "metric_name",
                    "dataset_name",
                    "expr",
                    "time_window",
                    "additivity",
                    "is_default",
                ],
                [["销售域", "GMV", "订单集", "sum(amount)", "1d", "ADDITIVE", 1]],
            ),
        }
    )
    assert (await _import(orphan))["success"] is True

    wb = await _export()
    assert {r["dimension_name"] for r in _rows(wb["dimension"])[1]} == {"地区"}
    assert {r["metric_name"] for r in _rows(wb["metric"])[1]} == {"GMV"}

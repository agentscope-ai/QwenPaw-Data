"""SPEC 01：按数据源导出正式语义 Excel（11 张契约 sheet，不含数据源列）。

导出面与导入契约（``template_contract.IMPORT_ORDER``）严格对齐，覆盖导入支持的
全部 sheet，保证导出物可被 ``excel_import_service`` 完整回灌（round-trip）：

- 逻辑层（biz_domain / dataset / dataset_column / dimension / dataset_dimension /
  metric / metric_formula）：父对象按 ``domain_name`` 等名称引用（旧协议形态）。
- 各 sheet 的取数口径按 SPEC 00 §12.3 分层（12 实体重构后域与字典已不挂数据源）：
  ①业务域取 ``datasource_scope`` 命中本数据源的域集合 D；②数据集/列按自带
  ``datasource_id``；③口径经父数据集归属；④⑤字典由 ③⑤ 的口径反推 id 去重回查；
  ⑥黄金查询按 ``domain_id IN D``。故"本数据源下无任何口径的孤立字典"不导出。
- 物理层（table / table_column / physical_join）：库里以 id 互相绑定，导出时反解回
  名称——table_column 反解 ``table_id → table_name``，physical_join 反解
  ``left/right_table_id → 表名``、``left/right_column_ids（$$$ 多值）→ 列名``。
- verified_query：契约已统一按 ``domain_name`` 引用业务域（SPEC 00 §12.1），与其余
  逻辑层 sheet 一致——``domain_name`` 必随 biz_domain 导出、round-trip 不丢，
  不依赖易失的 import_id。

布尔列（is_primary / is_nullable）存储面为 'Y'/'N'，导出还原为模板面 1/0（④a）。
"""

from __future__ import annotations

from io import BytesIO

from qwenpaw_data_auth import Principal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.errors import BadRequestError
from semantic_config.principal import require_admin
from semantic_config.repositories import datasource_repo
from semantic_config.services.domain_scope import (
    domain_ids_for_datasource,
    scope_contains,
)
from semantic_config.tables import (
    dataset,
    dataset_dimension,
    dataset_field,
    dimension_dict,
    domain,
    metric_caliber,
    metric_dict,
    physical_join,
    physical_table,
    table_column,
    verified_query,
)
from semantic_config.template_contract import (
    IMPORT_ORDER,
    MULTI_VALUE_SEPARATOR,
    SHEET_SPECS,
)
from semantic_config.template_generator import build_blank_workbook


def _cell(v):
    if v is None:
        return None
    return v


def _yn_to_10(v):
    if v is None or v == "":
        return None
    if v in (1, "1", "Y", "y", True):
        return 1
    if v in (0, "0", "N", "n", False):
        return 0
    return v


def _tenant_ws(principal: Principal) -> tuple[str, str]:
    return principal.tenant_id, principal.active_workspace_id


def _ids_to_names(ids_str: str | None, name_by_id: dict[int, str]) -> str | None:
    """把 $$$ 拼接的 id 串反解回 $$$ 拼接的名称串（保序，跳过缺失/非数字项）。"""
    if not ids_str:
        return None
    names: list[str] = []
    for raw in str(ids_str).split(MULTI_VALUE_SEPARATOR):
        raw = raw.strip()
        if not raw:
            continue
        try:
            cid = int(raw)
        except ValueError:
            continue
        name = name_by_id.get(cid)
        if name is not None:
            names.append(name)
    return MULTI_VALUE_SEPARATOR.join(names) if names else None


async def export_excel(
    db: AsyncSession, datasource_id: str, principal: Principal
) -> bytes:
    """导出当前数据源正式语义；sheet/列与导入契约一致，不含数据源编码列。"""
    require_admin(principal)
    if not datasource_id or not str(datasource_id).strip():
        raise BadRequestError("datasource_id 不能为空")
    datasource_id = str(datasource_id).strip()
    tid, wid = _tenant_ws(principal)
    ds = await datasource_repo.find_by_datasource_id(db, datasource_id, tid, wid)
    if ds is None:
        raise BadRequestError(f"数据源不存在: datasource_id={datasource_id}")

    rows_by_sheet = await _collect_rows(db, datasource_id, tid, wid)
    wb = build_blank_workbook(datasource_code=datasource_id)
    for name in IMPORT_ORDER:
        ws = wb[name]
        headers = list(SHEET_SPECS[name].header())
        for row in rows_by_sheet.get(name, []):
            ws.append([_cell(row.get(h)) for h in headers])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _collect_rows(
    db: AsyncSession, datasource_id: str, tid: str, wid: str
) -> dict[str, list[dict]]:
    # ① 业务域：域已与数据源解耦，改按派生列 datasource_scope 命中本数据源的域集合 D
    #    （即"该域下至少有一个本数据源的数据集"）。D 同时是 ⑥ 黄金查询的过滤依据。
    domain_ids = domain_ids_for_datasource(datasource_id, tid, wid)
    domains = (
        (
            await db.execute(
                select(domain.c.id, domain.c.domain_name, domain.c.description)
                .where(
                    domain.c.tenant_id == tid,
                    domain.c.workspace_id == wid,
                    domain.c.is_deleted == 0,
                    scope_contains(datasource_id),
                )
                .order_by(domain.c.id)
            )
        )
        .mappings()
        .all()
    )

    # ② 数据集 / 数据集列：数据集自带 datasource_id，直接过滤。
    datasets = (
        (
            await db.execute(
                select(
                    dataset.c.id,
                    dataset.c.domain_id,
                    domain.c.domain_name,
                    dataset.c.dataset_name,
                    dataset.c.description,
                    dataset.c.source,
                )
                .select_from(dataset.join(domain, domain.c.id == dataset.c.domain_id))
                .where(
                    dataset.c.tenant_id == tid,
                    dataset.c.workspace_id == wid,
                    dataset.c.is_deleted == 0,
                    dataset.c.datasource_id == datasource_id,
                )
                .order_by(dataset.c.id)
            )
        )
        .mappings()
        .all()
    )

    columns = (
        (
            await db.execute(
                select(
                    domain.c.domain_name,
                    dataset.c.dataset_name,
                    dataset_field.c.field_name,
                    dataset_field.c.description,
                    dataset_field.c.data_type,
                    dataset_field.c.is_primary,
                    dataset_field.c.is_nullable,
                )
                .select_from(
                    dataset_field.join(
                        dataset, dataset.c.id == dataset_field.c.dataset_id
                    ).join(domain, domain.c.id == dataset.c.domain_id)
                )
                .where(
                    dataset_field.c.tenant_id == tid,
                    dataset_field.c.workspace_id == wid,
                    dataset_field.c.is_deleted == 0,
                    dataset.c.datasource_id == datasource_id,
                )
                .order_by(dataset_field.c.id)
            )
        )
        .mappings()
        .all()
    )

    # ③ 维度口径：口径经父数据集归属本数据源。以 dataset.datasource_id 为准而非口径表
    #    的冗余列——冗余列本就派生自 dataset，取父表可免受历史脏值影响，结果一致。
    dim_binding_scope = (
        dataset_dimension.c.tenant_id == tid,
        dataset_dimension.c.workspace_id == wid,
        dataset_dimension.c.is_deleted == 0,
        dataset.c.datasource_id == datasource_id,
    )
    dim_bindings = (
        (
            await db.execute(
                select(
                    domain.c.domain_name,
                    dimension_dict.c.dimension_name,
                    dataset.c.dataset_name,
                    dataset_dimension.c.expr,
                    dataset_dimension.c.value_example,
                )
                .select_from(
                    dataset_dimension.join(
                        dataset, dataset.c.id == dataset_dimension.c.dataset_id
                    )
                    .join(
                        dimension_dict,
                        dimension_dict.c.id == dataset_dimension.c.dimension_id,
                    )
                    .join(domain, domain.c.id == dataset_dimension.c.domain_id)
                )
                .where(*dim_binding_scope)
                .order_by(dataset_dimension.c.id)
            )
        )
        .mappings()
        .all()
    )

    # ④ 维度字典：字典已无数据源，由 ③ 命中的口径反推 dimension_id 去重回查。
    #    语义是"本数据源实际用到的字典"，在本数据源下无任何口径的孤立字典不导出
    #    （导入是 upsert，不会误删，SPEC 00 §12.3）。
    bound_dimension_ids = (
        select(dataset_dimension.c.dimension_id)
        .select_from(
            dataset_dimension.join(
                dataset, dataset.c.id == dataset_dimension.c.dataset_id
            )
        )
        .where(*dim_binding_scope)
        .distinct()
        .scalar_subquery()
    )
    dims = (
        (
            await db.execute(
                select(
                    domain.c.domain_name,
                    dimension_dict.c.dimension_name,
                    dimension_dict.c.description,
                    dimension_dict.c.synonyms,
                    dimension_dict.c.dimension_type,
                    dimension_dict.c.child_dimensions,
                    dimension_dict.c.priority,
                )
                .select_from(
                    dimension_dict.join(
                        domain, domain.c.id == dimension_dict.c.domain_id
                    )
                )
                .where(
                    dimension_dict.c.tenant_id == tid,
                    dimension_dict.c.workspace_id == wid,
                    dimension_dict.c.is_deleted == 0,
                    dimension_dict.c.id.in_(bound_dimension_ids),
                )
                .order_by(dimension_dict.c.id)
            )
        )
        .mappings()
        .all()
    )

    # ⑤ 指标口径 + 指标字典：同 ③④ 口径（原按 metric_lib.datasource_id 过滤已不成立）。
    formula_scope = (
        metric_caliber.c.tenant_id == tid,
        metric_caliber.c.workspace_id == wid,
        metric_caliber.c.is_deleted == 0,
        dataset.c.datasource_id == datasource_id,
    )
    formulas = (
        (
            await db.execute(
                select(
                    domain.c.domain_name,
                    metric_dict.c.metric_name,
                    dataset.c.dataset_name,
                    metric_caliber.c.expr,
                    metric_caliber.c.time_window,
                    metric_caliber.c.additivity,
                    metric_caliber.c.is_default,
                )
                .select_from(
                    metric_caliber.join(
                        metric_dict, metric_dict.c.id == metric_caliber.c.metric_id
                    )
                    .join(dataset, dataset.c.id == metric_caliber.c.dataset_id)
                    .join(domain, domain.c.id == metric_caliber.c.domain_id)
                )
                .where(*formula_scope)
                .order_by(metric_caliber.c.id)
            )
        )
        .mappings()
        .all()
    )

    bound_metric_ids = (
        select(metric_caliber.c.metric_id)
        .select_from(
            metric_caliber.join(dataset, dataset.c.id == metric_caliber.c.dataset_id)
        )
        .where(*formula_scope)
        .distinct()
        .scalar_subquery()
    )
    metrics = (
        (
            await db.execute(
                select(
                    domain.c.domain_name,
                    metric_dict.c.metric_name,
                    metric_dict.c.description,
                    metric_dict.c.synonyms,
                    metric_dict.c.priority,
                )
                .select_from(
                    metric_dict.join(domain, domain.c.id == metric_dict.c.domain_id)
                )
                .where(
                    metric_dict.c.tenant_id == tid,
                    metric_dict.c.workspace_id == wid,
                    metric_dict.c.is_deleted == 0,
                    metric_dict.c.id.in_(bound_metric_ids),
                )
                .order_by(metric_dict.c.id)
            )
        )
        .mappings()
        .all()
    )

    # ---- 物理层（table / table_column / physical_join）：库存 id，导出反解回名称 ----
    tables = (
        (
            await db.execute(
                select(
                    physical_table.c.table_name,
                    physical_table.c.table_comment,
                    physical_table.c.partition_keys,
                )
                .where(
                    physical_table.c.tenant_id == tid,
                    physical_table.c.workspace_id == wid,
                    physical_table.c.is_deleted == 0,
                    physical_table.c.datasource_id == datasource_id,
                )
                .order_by(physical_table.c.id)
            )
        )
        .mappings()
        .all()
    )

    table_cols = (
        (
            await db.execute(
                select(
                    physical_table.c.table_name,
                    table_column.c.column_name,
                    table_column.c.data_type,
                    table_column.c.is_primary,
                    table_column.c.is_nullable,
                    table_column.c.column_comment,
                    table_column.c.sample_values,
                )
                .select_from(
                    table_column.join(
                        physical_table, physical_table.c.id == table_column.c.table_id
                    )
                )
                .where(
                    table_column.c.tenant_id == tid,
                    table_column.c.workspace_id == wid,
                    table_column.c.is_deleted == 0,
                    table_column.c.datasource_id == datasource_id,
                )
                .order_by(table_column.c.id)
            )
        )
        .mappings()
        .all()
    )

    # physical_join 存 table_id / table_column.id，导出需 id→名称字典反解。
    tbl_name_by_id = {
        int(rid): name
        for rid, name in (
            await db.execute(
                select(physical_table.c.id, physical_table.c.table_name).where(
                    physical_table.c.tenant_id == tid,
                    physical_table.c.workspace_id == wid,
                    physical_table.c.is_deleted == 0,
                    physical_table.c.datasource_id == datasource_id,
                )
            )
        ).all()
    }
    col_name_by_id = {
        int(cid): name
        for cid, name in (
            await db.execute(
                select(table_column.c.id, table_column.c.column_name).where(
                    table_column.c.tenant_id == tid,
                    table_column.c.workspace_id == wid,
                    table_column.c.is_deleted == 0,
                    table_column.c.datasource_id == datasource_id,
                )
            )
        ).all()
    }

    joins = (
        (
            await db.execute(
                select(
                    physical_join.c.left_table_id,
                    physical_join.c.left_column_ids,
                    physical_join.c.right_table_id,
                    physical_join.c.right_column_ids,
                    physical_join.c.join_type,
                )
                .where(
                    physical_join.c.tenant_id == tid,
                    physical_join.c.workspace_id == wid,
                    physical_join.c.is_deleted == 0,
                    physical_join.c.datasource_id == datasource_id,
                )
                .order_by(physical_join.c.id)
            )
        )
        .mappings()
        .all()
    )

    # ⑥ 黄金查询：以 ① 的业务域集合 D 作为 domain_id IN D 过滤，按 domain_name 导出。
    vqueries = (
        (
            await db.execute(
                select(
                    domain.c.domain_name,
                    verified_query.c.question,
                    verified_query.c.verified_sql,
                )
                .select_from(
                    verified_query.join(
                        domain, domain.c.id == verified_query.c.domain_id
                    )
                )
                .where(
                    verified_query.c.tenant_id == tid,
                    verified_query.c.workspace_id == wid,
                    verified_query.c.is_deleted == 0,
                    verified_query.c.domain_id.in_(domain_ids),
                )
                .order_by(verified_query.c.id)
            )
        )
        .mappings()
        .all()
    )

    return {
        "biz_domain": [
            {"domain_name": r["domain_name"], "description": r["description"]}
            for r in domains
        ],
        "table": [
            {
                "table_name": r["table_name"],
                "table_comment": r["table_comment"],
                "partition_keys": r["partition_keys"],
            }
            for r in tables
        ],
        "table_column": [
            {
                "table_name": r["table_name"],
                "column_name": r["column_name"],
                "data_type": r["data_type"],
                "is_primary": _yn_to_10(r["is_primary"]),
                "is_nullable": _yn_to_10(r["is_nullable"]),
                "column_comment": r["column_comment"],
                "sample_values": r["sample_values"],
            }
            for r in table_cols
        ],
        "physical_join": [
            {
                "left_table": tbl_name_by_id.get(int(r["left_table_id"])),
                "left_columns": _ids_to_names(r["left_column_ids"], col_name_by_id),
                "right_table": tbl_name_by_id.get(int(r["right_table_id"])),
                "right_columns": _ids_to_names(r["right_column_ids"], col_name_by_id),
                "join_type": r["join_type"],
            }
            for r in joins
        ],
        "verified_query": [
            {
                "domain_name": r["domain_name"],
                "question": r["question"],
                "verified_sql": r["verified_sql"],
            }
            for r in vqueries
        ],
        "dataset": [
            {
                "domain_name": r["domain_name"],
                "dataset_name": r["dataset_name"],
                "description": r["description"],
                "source": r["source"],
            }
            for r in datasets
        ],
        "dataset_column": [
            {
                "domain_name": r["domain_name"],
                "dataset_name": r["dataset_name"],
                "field_name": r["field_name"],
                "description": r["description"],
                "data_type": r["data_type"],
                "is_primary": _yn_to_10(r["is_primary"]),
                "is_nullable": _yn_to_10(r["is_nullable"]),
            }
            for r in columns
        ],
        "dimension": [
            {
                "domain_name": r["domain_name"],
                "dimension_name": r["dimension_name"],
                "description": r["description"],
                "synonyms": r["synonyms"],
                "dimension_type": r["dimension_type"],
                "child_dimensions": r["child_dimensions"],
                "priority": r["priority"],
            }
            for r in dims
        ],
        "dataset_dimension": [
            {
                "domain_name": r["domain_name"],
                "dimension_name": r["dimension_name"],
                "dataset_name": r["dataset_name"],
                "expr": r["expr"],
                "value_example": r["value_example"],
            }
            for r in dim_bindings
        ],
        "metric": [
            {
                "domain_name": r["domain_name"],
                "metric_name": r["metric_name"],
                "description": r["description"],
                "synonyms": r["synonyms"],
                "priority": r["priority"],
            }
            for r in metrics
        ],
        "metric_formula": [
            {
                "domain_name": r["domain_name"],
                "metric_name": r["metric_name"],
                "dataset_name": r["dataset_name"],
                "expr": r["expr"],
                "time_window": r["time_window"],
                "additivity": r["additivity"],
                "is_default": r["is_default"],
            }
            for r in formulas
        ],
    }

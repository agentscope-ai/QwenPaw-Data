"""Assemble workspace definitions and datasource facts for CM import.

mgw2-weave（W2 Task B）重锚定 12-sheet 新存储：

- 物理层三表 ``physical_table`` / ``table_column`` / ``physical_join`` 均按 ``datasource_id`` 过滤：
  - ``tables`` 段一行一 ``physical_table``（表名/table_comment/partition_keys），列经
    ``table_column.table_id`` 关联挂入（``table_column`` 已无 table_name）；
  - 顶层 ``physical_join`` 段：左右表 ``physical_table.id``、左右列 ``table_column.id``（$$$ 多值）
    反解成表名/列名，CM 图侧据此建 join 边；
- ``dataset`` is scoped by ``datasource_id``; domains cover the workspace.
- ``dataset_field`` / ``dataset_dimension`` / ``metric_caliber`` 锚定到本 ds 数据集 id 集；
- ``dimension_dict`` / ``metric_dict`` / ``verified_query`` cover all workspace
  domains, including definitions with no dataset binding or metric caliber.
- ``dataset_dimension.value_example``（$$$ 多值）取代已删的 values 表；
- ``verified_query``（L8）随 payload 携带（question/verified_sql；source 列已删，不再发 source_channel）；
- ``dataset.source`` 单列②b 内容自判：``SELECT``/``WITH`` 起头 = SQL，否则 = 表名；
  表式数据集的字段样例值从 P3 ``table_column.sample_values`` 富化（表名经 table_id 反解匹配）。

图写入面（节点/边/key/label）保持现拓扑不重建。

mgw6-import-rename：payload emit 面切 v2.1 正名契约（dataset_fields /
dimension_bindings / metric_calibers / source_channel）。
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.errors import BadRequestError
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

MODE_FULL = "FULL"
MODE_INCREMENTAL = "INCREMENTAL"


def _blank_to_none(s):
    if s is None:
        return None
    t = str(s).strip()
    return t or None


def _split_by(s, sep):
    if not s or not str(s).strip():
        return None
    parts = [p.strip() for p in str(s).split(sep)]
    parts = [p for p in parts if p]
    return parts or None


def _split_triple_dollar(s):
    return _split_by(s, "$$$")


def _prune(d: dict) -> dict:
    """对齐 Java 的 @JsonInclude(NON_NULL)：去掉 None / 空 list 的键（布尔 False 保留）。"""
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, list) and len(v) == 0:
            continue
        out[k] = v
    return out


def _is_sql_source(source) -> bool:
    """②b dataset.source 内容自判：``SELECT``/``WITH`` 起头（大小写不敏感）= SQL，否则表名。

    CM 导入消费 source 字段；此处用于判定是否可以按物理表富化样例值。
    """
    t = str(source or "").strip().upper()
    return t.startswith("SELECT") or t.startswith("WITH")


def _yn(value, default: bool) -> bool:
    """④a 存储 'Y'/'N' → 布尔；值缺失/异常时落 default（is_primary→False，is_nullable→True）。"""
    if value is None:
        return default
    t = str(value).strip().upper()
    if t == "Y":
        return True
    if t == "N":
        return False
    return default


def _cm_priority(value) -> str | None:
    """SPEC HIGH/MEDIUM/LOW → CM high/normal。"""
    if value is None or str(value).strip() == "":
        return None
    if str(value).strip().upper() == "HIGH":
        return "high"
    return "normal"


def _cm_dimension_role(dimension_type) -> str | None:
    """SPEC dimension_type → CM 接口字段 dimension_role（categorical/time 归一）。"""
    t = str(dimension_type or "").strip()
    if not t:
        return None
    low = t.lower()
    if low in {"time", "categorical"}:
        return low
    if "时间" in t or "time" in low or "date" in low or "日期" in t:
        return "time"
    return "categorical"


def _cm_additivity(value) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    u = str(value).strip().upper().replace("-", "_")
    mapping = {
        "ADDITIVE": "additive",
        "SEMI_ADDITIVE": "semi_additive",
        "NON_ADDITIVE": "non_additive",
    }
    return mapping.get(u, str(value).strip().lower())


async def _select_where(db: AsyncSession, t, tenant_id: str, workspace_id: str, *conds):
    res = await db.execute(
        select(t)
        .where(
            t.c.tenant_id == tenant_id,
            t.c.workspace_id == workspace_id,
            t.c.is_deleted == 0,
            *conds,
        )
        .order_by(t.c.id)
    )
    return res.mappings().all()


async def _select_in(db: AsyncSession, t, col, ids, tenant_id: str, workspace_id: str):
    if not ids:
        return []
    return await _select_where(db, t, tenant_id, workspace_id, col.in_(ids))


async def _fetch_all(
    db: AsyncSession, ds_code: str, tenant_id: str, workspace_id: str
) -> dict:
    """Read shared definitions independently of datasource-specific bindings."""
    datasets = await _select_where(
        db, dataset, tenant_id, workspace_id, dataset.c.datasource_id == ds_code
    )
    physical_tables = await _select_where(
        db,
        physical_table,
        tenant_id,
        workspace_id,
        physical_table.c.datasource_id == ds_code,
    )
    physical_columns = await _select_where(
        db,
        table_column,
        tenant_id,
        workspace_id,
        table_column.c.datasource_id == ds_code,
    )
    physical_joins = await _select_where(
        db,
        physical_join,
        tenant_id,
        workspace_id,
        physical_join.c.datasource_id == ds_code,
    )
    domains = await _select_where(db, domain, tenant_id, workspace_id)
    dataset_ids = sorted({d["id"] for d in datasets})
    domain_ids = [d["id"] for d in domains]
    fields = await _select_in(
        db,
        dataset_field,
        dataset_field.c.dataset_id,
        dataset_ids,
        tenant_id,
        workspace_id,
    )
    bindings = await _select_in(
        db,
        dataset_dimension,
        dataset_dimension.c.dataset_id,
        dataset_ids,
        tenant_id,
        workspace_id,
    )
    calibers = await _select_in(
        db,
        metric_caliber,
        metric_caliber.c.dataset_id,
        dataset_ids,
        tenant_id,
        workspace_id,
    )
    dimensions = await _select_in(
        db,
        dimension_dict,
        dimension_dict.c.domain_id,
        domain_ids,
        tenant_id,
        workspace_id,
    )
    metrics = await _select_in(
        db, metric_dict, metric_dict.c.domain_id, domain_ids, tenant_id, workspace_id
    )
    queries = await _select_in(
        db,
        verified_query,
        verified_query.c.domain_id,
        domain_ids,
        tenant_id,
        workspace_id,
    )
    return {
        "domains": domains,
        "datasets": datasets,
        "physical_tables": physical_tables,
        "physical_columns": physical_columns,
        "physical_joins": physical_joins,
        "fields": fields,
        "bindings": bindings,
        "calibers": calibers,
        "dimensions": dimensions,
        "metrics": metrics,
        "verified_queries": queries,
    }


async def assemble(
    db: AsyncSession,
    datasource_id: str,
    task_id: str,
    tenant_id: str,
    workspace_id: str,
    *,
    drop_semantic_first: bool = True,
) -> dict:
    """Combine workspace definitions with the selected datasource's facts.

    payload 对齐 12-sheet 实体：顶层 ``tables``（P2/P3）+ ``semantic.domains``
    （L1 → L2/L3/L4/L5/L6/L7/L8 嵌套）。顶层结构对齐 CM 的 SemanticImportRequest
    （context_manager/metadata/contract.py）。

    ``drop_semantic_first`` 透传到 CM 载荷：FULL 模式先清空该 datasource 的语义节点
    再重建；INCREMENTAL 模式不删，仅 upsert（payload 未提及的节点保留）。
    """
    ds = (
        (
            await db.execute(
                select(datasource).where(
                    datasource.c.datasource_id == datasource_id,
                    datasource.c.tenant_id == tenant_id,
                    datasource.c.workspace_id == workspace_id,
                    datasource.c.is_deleted == 0,
                )
            )
        )
        .mappings()
        .first()
    )
    if ds is None:
        raise BadRequestError(f"数据源不存在: datasource_id={datasource_id}")

    data = await _fetch_all(db, datasource_id, tenant_id, workspace_id)
    datasets = data["datasets"]

    datasets_by_domain = defaultdict(list)
    for d in datasets:
        if d["domain_id"] is not None:
            datasets_by_domain[d["domain_id"]].append(d)
    fields_by_dataset = defaultdict(list)
    for f in data["fields"]:
        fields_by_dataset[f["dataset_id"]].append(f)
    dims_by_domain = defaultdict(list)
    for d in data["dimensions"]:
        dims_by_domain[d["domain_id"]].append(d)
    bindings_by_dim = defaultdict(list)
    for b in data["bindings"]:
        bindings_by_dim[b["dimension_id"]].append(b)
    metrics_by_domain = defaultdict(list)
    for m in data["metrics"]:
        metrics_by_domain[m["domain_id"]].append(m)
    calibers_by_metric = defaultdict(list)
    for c in data["calibers"]:
        calibers_by_metric[c["metric_id"]].append(c)
    queries_by_domain = defaultdict(list)
    for v in data["verified_queries"]:
        queries_by_domain[v["domain_id"]].append(v)

    dataset_id_to_name = {d["id"]: d["dataset_name"] for d in datasets}
    # P2/P3 反解映射：table_id→表名、table_column.id→列名（physical_join / 富化用）。
    table_id_to_name = {t["id"]: t["table_name"] for t in data["physical_tables"]}
    col_id_to_name = {c["id"]: c["column_name"] for c in data["physical_columns"]}
    cols_by_table = defaultdict(list)
    for c in data["physical_columns"]:
        cols_by_table[c["table_id"]].append(c)
    # P3 物理列索引：(两段全名, 列名) → 行；表名经 table_id 反解（table_column 无 table_name）。
    physical_index = {
        (table_id_to_name.get(c["table_id"]), c["column_name"]): c
        for c in data["physical_columns"]
    }

    def build_tables():
        """P2 物理表（表名/注释/分区键）+ 其 P3 列（经 table_id 关联）。"""
        out = []
        for t in data["physical_tables"]:
            out.append(
                _prune(
                    {
                        "name": t["table_name"],
                        "comment": t["table_comment"],
                        "partition_keys": _split_triple_dollar(t["partition_keys"]),
                        "columns": [
                            _prune(
                                {
                                    "name": c["column_name"],
                                    "data_type": c["data_type"],
                                    "is_primary": _yn(c["is_primary"], False),
                                    "is_nullable": _yn(c["is_nullable"], True),
                                    "comment": c["column_comment"],
                                    "sample_values": _split_triple_dollar(
                                        c["sample_values"]
                                    ),
                                }
                            )
                            for c in cols_by_table.get(t["id"], [])
                        ]
                        or None,
                    }
                )
            )
        return out

    def _join_col_names(id_str):
        """$$$ 多值的 table_column.id → 列名列表（缺失/非数字剔除）。"""
        ids = _split_triple_dollar(id_str) or []
        names = [col_id_to_name.get(int(i)) for i in ids if str(i).strip().isdigit()]
        return [n for n in names if n] or None

    def build_physical_joins():
        """物理连接：左右表 id→表名、左右列 id→列名（$$$ 多值）反解。顶层字段。"""
        out = []
        for j in data["physical_joins"]:
            out.append(
                _prune(
                    {
                        "left_table": table_id_to_name.get(j["left_table_id"]),
                        "left_columns": _join_col_names(j["left_column_ids"]),
                        "right_table": table_id_to_name.get(j["right_table_id"]),
                        "right_columns": _join_col_names(j["right_column_ids"]),
                        "join_type": j["join_type"],
                    }
                )
            )
        return out

    def build_dataset_fields(dset):
        """L3 dataset_field → 正名段 dataset_fields；表式来源从 P3 富化样例值（D2 落点）。"""
        sql_source = _is_sql_source(dset["source"])
        out = []
        for f in fields_by_dataset.get(dset["id"], []):
            samples = None
            if not sql_source:
                phys = physical_index.get((dset["source"], f["field_name"]))
                if phys is not None:
                    samples = _split_triple_dollar(phys["sample_values"])
            out.append(
                _prune(
                    {
                        "name": f["field_name"],
                        "description": f["description"],
                        "data_type": f["data_type"],
                        "is_primary": _yn(f["is_primary"], False),
                        "is_nullable": _yn(f["is_nullable"], True),
                        "sample_values": samples,
                    }
                )
            )
        return out or None

    def build_datasets(dsets):
        out = []
        for d in dsets:
            out.append(
                _prune(
                    {
                        "name": d["dataset_name"],
                        "description": d["description"],
                        "source": d["source"],
                        # mgw6-import-rename：正名段 dataset_fields（旧名 columns 退役）
                        "dataset_fields": build_dataset_fields(d),
                    }
                )
            )
        return out or None

    def build_dimension_bindings(binds):
        out = []
        for b in binds:
            out.append(
                _prune(
                    {
                        "dataset": dataset_id_to_name.get(b["dataset_id"]),
                        "expr": b["expr"],
                        "value_example": _split_triple_dollar(
                            b.get("value_example") or b.get("value_example")
                        ),
                    }
                )
            )
        return out or None

    def build_dimensions(dims):
        out = []
        for d in dims:
            out.append(
                _prune(
                    {
                        "name": d["dimension_name"],
                        "description": d["description"],
                        # v2.1 多值统一 $$$（W1 契约）；CM DimensionPayload 用 aliases
                        "aliases": _split_triple_dollar(d["synonyms"]),
                        "dimension_type": _cm_dimension_role(
                            d.get("dimension_type") or d.get("dimension_role")
                        ),
                        "child_dimensions": _split_triple_dollar(d["child_dimensions"]),
                        "priority": _cm_priority(d["priority"]),
                        # mgw6-import-rename：正名段 dimension_bindings（旧名 bindings 退役）
                        "dimension_bindings": build_dimension_bindings(
                            bindings_by_dim.get(d["id"], [])
                        ),
                    }
                )
            )
        return out or None

    def build_metric_calibers(cals):
        out = []
        for c in cals:
            out.append(
                _prune(
                    {
                        "dataset": dataset_id_to_name.get(c["dataset_id"]),
                        "expr": c["expr"],
                        # time_window 可空（导入侧空值落空串）：空则 blank→None 由 _prune 剔除。
                        "time_window": _blank_to_none(c["time_window"]),
                        "additivity": _cm_additivity(c["additivity"]),
                        "is_default": None
                        if c["is_default"] is None
                        else bool(c["is_default"]),
                    }
                )
            )
        return out or None

    def build_metrics(ms):
        out = []
        for m in ms:
            out.append(
                _prune(
                    {
                        "name": m["metric_name"],
                        "description": m["description"],
                        "aliases": _split_triple_dollar(m["synonyms"]),
                        "priority": _cm_priority(m["priority"]),
                        # mgw6-import-rename：正名段 metric_calibers（旧名 calibers/formulas 退役）
                        "metric_calibers": build_metric_calibers(
                            calibers_by_metric.get(m["id"], [])
                        ),
                    }
                )
            )
        return out or None

    def build_verified_queries(rows):
        # verified_query 重构后已删 source 列：只带 question / verified_sql。
        out = []
        for v in rows:
            out.append(
                _prune(
                    {
                        "question": v["question"],
                        "verified_sql": v["verified_sql"],
                    }
                )
            )
        return out or None

    domain_nodes = []
    for dom in data["domains"]:
        domain_nodes.append(
            _prune(
                {
                    "name": dom["domain_name"],
                    # 协议 v0.3：语义层图节点 key 的 domain 段一律用 config 权威分配的
                    # domain_id（全局唯一、稳定、不随改名变化）。存储 import_id 优先
                    # （Excel 域编码），无 import_id 回退存储主键 id 的字符串形态。
                    "domain_id": str(dom.get("import_id") or "").strip()
                    or str(dom["id"]),
                    "description": dom["description"],
                    "datasets": build_datasets(datasets_by_domain.get(dom["id"], [])),
                    "dimensions": build_dimensions(dims_by_domain.get(dom["id"], [])),
                    "metrics": build_metrics(metrics_by_domain.get(dom["id"], [])),
                    "verified_queries": build_verified_queries(
                        queries_by_domain.get(dom["id"], [])
                    ),
                }
            )
        )

    # 顶层对齐 context_manager.metadata.SemanticImportRequest。
    return {
        "datasource_id": ds["datasource_id"],
        "datasource": {
            "datasource_name": ds["datasource_name"],
            "datasource_type": ds["datasource_type"],
            "description": ds.get("description") or "",
        },
        "tables": build_tables(),
        "physical_joins": build_physical_joins(),
        "semantic": _prune({"domains": domain_nodes or None}),
        "drop_semantic_first": drop_semantic_first,
        "task_id": task_id,
    }

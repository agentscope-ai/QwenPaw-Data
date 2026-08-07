"""语义层 + Bridge ingester（``graph_topology_v4.md``）。

读取 ``metrics_dict.yaml``，按下列顺序写入 Neo4j：

1. ``Domain``               (每个 ``domains[]``)
2. ``Dimension`` + ``DimensionValue`` + ``HAS_VALUE`` + ``HAS_PARENT`` + ``MAPS_TO_COLUMN``
3. ``Caliber`` + ``FILTER_ON``
4. ``Metric`` + ``HAS_METRIC`` + 同义词 / aliases 全文检索字段
5. ``Formula`` + ``HAS_FORMULA`` + ``OF_VIEW`` (Dataset) + ``USES_COLUMN`` (DatasetColumn / Column)
6. ``Dataset`` + ``HAS_DATASET`` + ``CONTAINS_TABLE``（由公式 dataset 派生）
7. ``Metric -[:ANALYZED_BY]-> Dimension``
8. ``Metric -[:DERIVED_FROM]-> Metric``

约束：

- 写入 ``Domain``/``Dimension``/``Metric``/...时统一加 ``zone='metadata'``。
- ``status: pending`` / ``status: review`` 的项**只**建节点，**不**建跨表桥（防 ghost）。
  这与 metrics_dict.yaml §5「验证清单」一致。
- 桥边端点用 ``MATCH``（不是 MERGE）：物理层不存在的列默默丢弃，不创建悬空 Column。
- v3.1：``ingest_semantic`` 接受 ``profile`` 注入，用于 ``dataset_short`` 的截断正则。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from .keys import (
    DEFAULT_DB_ID,
    DEFAULT_SCHEMA,
    METADATA_ZONE,
    caliber_key,
    column_key,
    dataset_column_key,
    dataset_key,
    dataset_short,
    datasource_key,
    logical_dataset_name,
    dim_key,
    dim_value_key,
    domain_key,
    formula_key,
    metric_key,
    split_qualified_column,
    split_qualified_table,
    table_key,
)
from .profile import DatasetProfile
from .semantic_derive import (
    TOPLINE_LITERALS_DEFAULT,
    TOPLINE_LITERALS_RICH,
    backfill_dimension_supplement,
    backfill_dimension_values_from_columns,
    backfill_metric_units,
    derive_analyzed_by_from_topology,
    derive_calibers_from_formulas,
    derive_column_time_grain,
    derive_granularity_partitions,
    derive_partitions_from_comments,
    derive_partitions_from_data,
    derive_view_alias_synonyms,
    parse_topline_filters,
    sync_column_to_dataset_column,
)
from .semantic_fields import (
    anomaly_rules_to_json,
    dim_value_rows,
    metric_role_from_props,
)

log = get_logger("graph.semantic")


# ---------------------------------------------------------------------- #
# datasource_id post-pass
# ---------------------------------------------------------------------- #
def _backfill_datasource_id(
    driver: Driver, datasource_id: str, domains: Optional[list[str]] = None
) -> None:
    """Deprecated: datasource_id is now written on every node at creation time
    (single-source invariant — one ``ingest_semantic`` / ``write_*`` call = one
    datasource, threaded into every key + ``SET datasource_id``). This post-pass
    heuristic is redundant. Kept as a no-op so existing call sites remain valid.
    """
    log.debug("datasource_id backfill skipped: nodes are datasource-scoped at write time")
    return


# ---------------------------------------------------------------------- #
# 数据加载
# ---------------------------------------------------------------------- #
def load_metrics_dict(path: Path) -> dict[str, Any]:
    """读 ``metrics_dict.yaml``。延迟 import PyYAML，避免顶层依赖。"""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load metrics_dict.yaml. Add `pyyaml>=6.0` to requirements.txt."
        ) from exc
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"metrics_dict.yaml top-level must be a mapping, got {type(data).__name__}")
    return data


# ---------------------------------------------------------------------- #
# 中间结构（与 YAML 字段同名，便于一对一传给 Cypher UNWIND）
# ---------------------------------------------------------------------- #
@dataclass
class _PendingBridge:
    """暂存「待建桥」记录，用于 status=pending/review 时跳过的旁路日志。"""

    kind: str
    detail: str


# ---------------------------------------------------------------------- #
# 工具：参数归一化
# ---------------------------------------------------------------------- #
def _is_skipped_status(status: Optional[str]) -> bool:
    """``pending`` / ``review`` 只建节点不建桥（与 metrics_dict.yaml §5 对齐）。"""
    return (status or "").strip().lower() in {"pending", "review"}


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        # 兼容 ``$$$`` 拼接的多值字符串（xlsx 导出习惯）
        parts = v.split("$$$") if "$$$" in v else [v]
        return [p.strip() for p in parts if p.strip()]
    if isinstance(v, (list, tuple)):
        out: list[str] = []
        for x in v:
            if x is None:
                continue
            s = str(x)
            # list 里的元素也可能含 ``$$$``
            for p in (s.split("$$$") if "$$$" in s else [s]):
                p = p.strip()
                if p:
                    out.append(p)
        return out
    return [str(v)]


def _bool_flag(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "y")


# ---------------------------------------------------------------------- #
# 写入入口
# ---------------------------------------------------------------------- #
def ingest_semantic(
    driver: Driver,
    metrics_dict_path: Path,
    *,
    db_id: str = DEFAULT_DB_ID,
    schema: str = DEFAULT_SCHEMA,
    profile: Optional[DatasetProfile] = None,
    via_dataset_column: bool = False,  # deprecated, kept for caller compat; always prefers DatasetColumn now
    datasource_id: str = "",
    datasource_name: str = "",
    table_db_map: Optional[dict] = None,
) -> None:
    """读 YAML → 全部 MERGE 到 Neo4j。``db_id`` / ``schema`` 用于补 metrics_dict.yaml 中省略的 db 前缀。
    ``profile`` 用于 ``dataset_short`` 的截断正则（None 时使用 appdata 内置正则，向后兼容）。

    ``datasource_id`` 标识数据源归属；未传时从 registry 自动解析。
    写入完成后通过 post-pass 统一 SET 到所有语义层节点。

    Formula USES_COLUMN / Dimension MAPS_TO 始终优先连接 DatasetColumn（语义列）；
    若语义列不存在则降级连物理 Column 并输出 warning。
    """
    # 解析 datasource_id
    ds_id = (datasource_id or "").strip()
    if not ds_id:
        from .datasource_registry import db_id_to_datasource
        ds = db_id_to_datasource(db_id)
        ds_id = ds.datasource_id if ds else ""
    data = load_metrics_dict(metrics_dict_path)
    domains = list(data.get("domains") or [])

    log.info(
        "metrics_dict loaded: version=%r domains=%d",
        data.get("version"),
        len(domains),
    )

    pending: list[_PendingBridge] = []

    # ---- DataSource 顶层节点 ----
    dsrc_name = (datasource_name or ds_id or "").strip()
    dsrc_key_str = datasource_key(dsrc_name) if dsrc_name else ""
    if dsrc_name:
        from .datasource_registry import try_resolve
        dsrc_info = try_resolve(dsrc_name)
        with neo4j_session(driver) as s:
            s.execute_write(
                _write_datasource_node,
                dsrc_key=dsrc_key_str,
                datasource_name=dsrc_name,
                display_name=dsrc_info.display_name if dsrc_info else dsrc_name,
                db_type=dsrc_info.db_type if dsrc_info else "",
            )

    # ---- Dataset 节点（必须在 Dimension MAPS_TO 之前创建） ----
    all_datasets: list[dict] = []
    for dom in domains:
        dom_name = str(dom.get("name") or "").strip()
        if not dom_name:
            continue
        for ds in dom.get("datasets") or []:
            if isinstance(ds, dict) and ds.get("name"):
                all_datasets.append(ds)
    if all_datasets:
        write_datasets(
            driver,
            datasets=all_datasets,
            db_id=db_id,
            schema=schema,
            datasource_id=ds_id,
        )

    # ---- DatasetColumn 节点（必须在 Dimension MAPS_TO 之前创建） ----
    all_ds_cols: list[dict] = []
    for dom in domains:
        for dc in dom.get("dataset_columns") or []:
            if isinstance(dc, dict) and dc.get("col_name"):
                all_ds_cols.append(dc)
    if all_ds_cols:
        write_dataset_columns(
            driver,
            dataset_columns=all_ds_cols,
            db_id=db_id,
            schema=schema,
            datasource_id=ds_id,
        )

    with neo4j_session(driver) as s:
        # 逐 domain
        for dom in domains:
            _write_domain(s, dom, db_id=db_id, schema=schema, pending=pending, profile=profile, dsrc_key=dsrc_key_str, table_db_map=table_db_map or {}, datasource_id=ds_id)

    if pending:
        log.warning(
            "skipped %d cross-table bridges due to pending/review status:\n  %s",
            len(pending),
            "\n  ".join(f"[{p.kind}] {p.detail}" for p in pending[:20]),
        )

    # ---- post-pass（legacy）：从历史遗留的 Formula.partition_predicate 里抓 `<col> = '<topline>'`
    # 折射成 Table.partition_columns / is_multidim + Column.granularity_role / topline_value。
    # 新写入流程已不再写 partition_predicate，该步逐步退化为 no-op。
    derive_granularity_partitions(driver)

    # ---- 数据驱动 post-pass：直接探 PG 的 distinct 值找漏标的 topline 列。
    # partition_predicate 已停止写入；靠 PG 的真实数据兜底。
    # PG 不可达时静默跳过（不影响图层主流程）。
    try:
        derive_partitions_from_data(driver, schema=schema)
    except Exception as exc:  # noqa: BLE001
        log.warning("derive_partitions_from_data failed: %s (continuing)", exc)

    # ---- 注释驱动 post-pass：扫 ddl.txt 写进 PG 的 col 注释（pg_description 已经
    # 反射进 Column.description）找 "含 全部" / "含 汇总" / summary_dimension 标记。
    # 这一步即使 PG 不可达也能跑（数据来自 Neo4j 上已经写好的 description 字段），
    # 是 derive_partitions_from_data 的注释侧补充。
    try:
        derive_partitions_from_comments(driver)
    except Exception as exc:  # noqa: BLE001
        log.warning("derive_partitions_from_comments failed: %s (continuing)", exc)

    # ---- DDL 文本兜底：从 ddl.txt 解析 view 块，把 `<src_col> AS "<alias>"` 的
    # 业务别名（中文指标名为主）反挂到源 Column 的 synonyms 列表。这一步是补
    # "ddl.txt 信息没充分提取" 的核心 —— 锚点检索的 fulltext + vector 召回都会
    # 受益（用户问 "Studio DAU" 时能命中 landingpagevisit_usercnt_1d 这种物理列）。
    ddl_path = _resolve_ddl_path(profile)
    if ddl_path is not None:
        try:
            derive_view_alias_synonyms(driver, ddl_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("derive_view_alias_synonyms failed: %s (continuing)", exc)

    # ---- 命名约定：从列名后缀（_1d / _30d / _acc / _td / _fy / ...）抽时间粒度。
    # 给 LLM 一个明确的「这列是日粒度 / 月粒度 / 累计 / 财年累计」标签，避免把
    # active_usercnt_1d 误当成 30d 列；Decision LLM 也可以据此过滤掉与问题时间跨度
    # 不匹配的候选指标。
    try:
        derive_column_time_grain(driver)
    except Exception as exc:  # noqa: BLE001
        log.warning("derive_column_time_grain failed: %s (continuing)", exc)

    try:
        sync_column_to_dataset_column(driver)
    except Exception as exc:  # noqa: BLE001
        log.warning("sync_column_to_dataset_column failed: %s (continuing)", exc)

    try:
        derive_calibers_from_formulas(driver)
    except Exception as exc:  # noqa: BLE001
        log.warning("derive_calibers_from_formulas failed: %s (continuing)", exc)

    try:
        backfill_metric_units(driver)
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill_metric_units failed: %s (continuing)", exc)

    try:
        derive_analyzed_by_from_topology(driver)
    except Exception as exc:  # noqa: BLE001
        log.warning("derive_analyzed_by_from_topology failed: %s (continuing)", exc)

    try:
        backfill_dimension_supplement(driver)
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill_dimension_supplement failed: %s (continuing)", exc)

    try:
        backfill_dimension_values_from_columns(driver)
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill_dimension_values_from_columns failed: %s (continuing)", exc)

    # ---- datasource_id post-pass：按本次 ingest 的 domain 范围精确打标
    if ds_id:
        domain_names = [
            str(d.get("name")).strip()
            for d in domains
            if isinstance(d, dict) and str(d.get("name") or "").strip()
        ]
        _backfill_datasource_id(driver, ds_id, domain_names)


def _resolve_physical_keys(
    datasource_id: str, fallback_db_id: str, fallback_schema: str
) -> tuple[str, str]:
    """按 dataset 的 datasource_id 解析物理层 (db_id, schema)。

    空或无法解析时回退到 caller 提供的默认值。用于 write_datasets /
    write_dataset_columns 里按行构造 table_key / column_key，保证
    CONTAINS_TABLE / DERIVED_FROM 边的 key 与物理层 Table/Column 节点 key 一致。
    """
    ds_id = (datasource_id or "").strip()
    if not ds_id:
        return fallback_db_id, fallback_schema
    try:
        from .datasource_registry import resolve
        info = resolve(ds_id)
        return info.primary_db_id, fallback_schema
    except Exception:
        return fallback_db_id, fallback_schema


def _default_db_for_ref(
    table_db_map: Optional[dict], domain: str, ref: str, fallback_db: str, *, is_column: bool
) -> str:
    """按引用里的表名解析 ``default_db``，让 formula/dimension/caliber 的列解析
    跟随 dataset 所属数据源（如 ODPS=warehouse_odps），而非写死全局 ``db_id``。

    ``ref`` 已经是 ``[db.][schema.]table[.column]`` 形式：列引用的表名是倒数第二段、
    表引用的表名是最后一段。``table_db_map`` **按 (domain, 表名) 寻址**——同名物理表可能
    同时存在于不同域/数据源（如 Studio/Holo 与 Ops/ODPS 复用同一表名），按域消歧才能
    选对 db。命中返回其物理 db，否则回退 ``fallback_db``（Holo 表回退到 app_db，
    与既有行为一致）。若 ``ref`` 已带 db 前缀，``split_qualified_*`` 会优先用显式 db。"""
    if not table_db_map:
        return fallback_db
    parts = [p for p in str(ref).split(".") if p]
    base = (parts[-2] if len(parts) >= 2 else "") if is_column else (parts[-1] if parts else "")
    if not base:
        return fallback_db
    return table_db_map.get((domain, base), fallback_db)


def write_domains(driver: Driver, *, domains: list[dict], datasource_id: str = "") -> None:
    """Upsert :Domain nodes (nodes only — metrics/dims/formulas handled by ingest_semantic).

    Must run before ``write_datasets`` so the OPTIONAL MATCH on Domain in
    ``_write_dataset_tx`` finds existing nodes and creates HAS_DATASET edges
    for ALL datasets, including pure dimension tables not referenced by any formula.
    """
    if not domains:
        return
    count = 0
    with neo4j_session(driver) as s:
        for dom in domains:
            name = str(dom.get("name") or "").strip()
            if not name:
                continue
            s.execute_write(
                _write_domain_node,
                dom_key=str(dom.get("key") or domain_key(name, datasource_id)),
                name=name,
                description=str(dom.get("description") or ""),
                status=str(dom.get("status") or "stable"),
                display_name=str(dom.get("display_name") or name),
                aliases=_str_list(dom.get("aliases")),
                datasource_id=datasource_id,
            )
            count += 1
    log.info("write_domains: upserted %d domain(s)", count)


def write_datasets(
    driver: Driver,
    *,
    datasets: list[dict],
    db_id: str = DEFAULT_DB_ID,
    schema: str = DEFAULT_SCHEMA,
    datasource_id: str = "",
) -> None:
    """Upsert :Dataset 节点。

    与 ``_write_formula`` 的 lazy 创建不同：那里只能 set name/domain/parents；
    这里把 BI 维护的 description/sql/parents/dataset_type 一并写入，并按
    parents 字段建 :Dataset-[:CONTAINS_TABLE]->:Table 边（_#1/_#2 变体与 base
    各自有节点，共享同一物理表）。

    ``datasource_id`` 未传时从 registry 自动解析；写完后 post-pass SET 到所有语义节点。
    """
    if not datasets:
        return
    with neo4j_session(driver) as s:
        for d in datasets:
            name = str(d.get("name") or "").strip()
            dom = str(d.get("domain") or "").strip()
            phys = str(d.get("physical_table") or "").strip()
            if not (name and dom):
                continue
            ds_ds_id = (datasource_id or str(d.get("datasource_id") or "")).strip()
            ds_k = dataset_key(dom, name, ds_ds_id)
            tbl_db, tbl_sch = _resolve_physical_keys(ds_ds_id, db_id, schema)
            tbl_k = table_key(tbl_db, tbl_sch, phys, ds_ds_id) if phys else ""
            qt = f"{tbl_db}.{tbl_sch}.{phys}" if phys else ""
            s.execute_write(
                _write_dataset_tx,
                ds_key=ds_k,
                name=name,
                domain=dom,
                domain_key_str=domain_key(dom, ds_ds_id),
                description=str(d.get("description") or ""),
                dataset_type=str(d.get("dataset_type") or "OLAP") or "OLAP",
                sql=str(d.get("sql") or ""),
                parents=str(d.get("parents") or ""),
                tbl_key=tbl_k,
                filter_summary=str(d.get("filter_summary") or ""),
                datasource_id=ds_ds_id,
                qualified_table=qt,
            )
    log.info("write_datasets: upserted %d dataset(s)", len(datasets))

    # datasource_id post-pass：按本次写入涉及的 domain 范围精确打标
    ds_id = (datasource_id or "").strip()
    if not ds_id:
        from .datasource_registry import db_id_to_datasource
        ds = db_id_to_datasource(db_id)
        ds_id = ds.datasource_id if ds else ""
    if ds_id:
        domain_names = sorted({
            str(d.get("domain")).strip()
            for d in datasets
            if str(d.get("domain") or "").strip()
        })
        _backfill_datasource_id(driver, ds_id, domain_names)


def _write_dataset_tx(
    tx,
    *,
    ds_key: str,
    name: str,
    domain: str,
    domain_key_str: str,
    description: str,
    dataset_type: str,
    sql: str,
    parents: str,
    tbl_key: str,
    filter_summary: str,
    datasource_id: str,
    qualified_table: str = "",
) -> None:
    tx.run(
        """
        MERGE (ds:Dataset {key: $ds_key})
          ON CREATE SET ds.name = $name, ds.domain = $domain,
                        ds.description = $description, ds.dataset_type = $dataset_type,
                        ds.sql = $sql, ds.parents = $parents, ds.zone = 'metadata',
                        ds.filter_summary = $filter_summary, ds.datasource_id = $datasource_id,
                        ds.qualified_table = $qualified_table
          ON MATCH  SET ds.name = $name, ds.domain = $domain,
                        ds.description = $description, ds.dataset_type = $dataset_type,
                        ds.sql = $sql, ds.parents = $parents, ds.zone = 'metadata',
                        ds.filter_summary = $filter_summary, ds.datasource_id = $datasource_id,
                        ds.qualified_table = $qualified_table
        WITH ds, $domain_key_str AS domk
        OPTIONAL MATCH (dom:Domain {key: domk})
        FOREACH (_ IN CASE WHEN dom IS NULL THEN [] ELSE [dom] END |
            MERGE (dom)-[:HAS_DATASET]->(ds)
        )
        WITH ds
        OPTIONAL MATCH (t:Table {key: $tbl_key})
        FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [t] END |
            MERGE (ds)-[:CONTAINS_TABLE]->(t)
        )
        """,
        ds_key=ds_key,
        name=name,
        domain=domain,
        description=description,
        dataset_type=dataset_type,
        sql=sql,
        parents=parents,
        tbl_key=tbl_key,
        domain_key_str=domain_key_str,
        filter_summary=filter_summary,
        datasource_id=datasource_id,
        qualified_table=qualified_table,
    )


def write_dataset_columns(
    driver: Driver,
    *,
    dataset_columns: list[dict],
    db_id: str = DEFAULT_DB_ID,
    schema: str = DEFAULT_SCHEMA,
    datasource_id: str = "",
) -> None:
    """Upsert :DatasetColumn 节点 + HAS_COLUMN / DERIVED_FROM 边（dataset 路径专用）。"""
    if not dataset_columns:
        return
    with neo4j_session(driver) as s:
        for dc in dataset_columns:
            domain = str(dc.get("domain") or "").strip()
            ds_name = str(dc.get("dataset_name") or "").strip()
            col_name = str(dc.get("col_name") or "").strip()
            phys_table = str(dc.get("physical_table") or "").strip()
            if not (domain and ds_name and col_name):
                continue
            dc_ds_id = (datasource_id or str(dc.get("datasource_id") or "")).strip()
            dscol_k = dataset_column_key(domain, ds_name, col_name, dc_ds_id)
            ds_k = dataset_key(domain, ds_name, dc_ds_id)
            col_db, col_sch = _resolve_physical_keys(dc_ds_id, db_id, schema)
            col_k = column_key(col_db, col_sch, phys_table, col_name, dc_ds_id) if phys_table else ""
            display_name = str(dc.get("display_name") or col_name)
            aliases_raw = list(dc.get("aliases") or [])
            aliases = [a for a in aliases_raw if a and a != display_name]
            enum_desc = dc.get("enum_value_descriptions") or {}
            enum_desc_json = json.dumps(enum_desc, ensure_ascii=False) if enum_desc else "{}"
            composed_of = list(dc.get("composed_of") or [])
            s.execute_write(
                _write_dataset_column_tx,
                dscol_key=dscol_k,
                name=col_name,
                display_name=display_name,
                aliases=aliases,
                description=str(dc.get("description") or ""),
                column_type=str(dc.get("column_type") or ""),
                sample_values=list(dc.get("sample_values") or []),
                enum_value_descriptions=enum_desc_json,
                data_type=str(dc.get("data_type") or "text"),
                dataset_key_val=f"{domain}.{ds_name}",
                ds_key=ds_k,
                col_key=col_k,
                composite=bool(composed_of),
                composite_desc=str(dc.get("composite_desc") or ""),
                datasource_id=dc_ds_id,
            )
            for comp in composed_of:
                comp_col = str(comp.get("column") or "").strip()
                comp_role = str(comp.get("role") or "").strip()
                if not comp_col:
                    continue
                try:
                    cdb, csch, ctbl, cname = split_qualified_column(
                        comp_col, default_db=col_db,
                    )
                except ValueError:
                    continue
                s.execute_write(
                    _write_dataset_column_composed_of_tx,
                    dscol_key=dscol_k,
                    col_key=column_key(cdb, csch, ctbl, cname, dc_ds_id),
                    role=comp_role,
                )
    log.info("write_dataset_columns: upserted %d DatasetColumn node(s)", len(dataset_columns))


def _write_dataset_column_tx(
    tx,
    *,
    dscol_key: str,
    name: str,
    display_name: str,
    aliases: list[str],
    description: str,
    column_type: str,
    sample_values: list[str],
    enum_value_descriptions: str,
    data_type: str,
    dataset_key_val: str,
    ds_key: str,
    col_key: str,
    composite: bool = False,
    composite_desc: str = "",
    datasource_id: str = "",
) -> None:
    tx.run(
        """
        MERGE (dc:DatasetColumn {key: $dscol_key})
          ON CREATE SET dc.name = $name, dc.display_name = $display_name,
                        dc.aliases = $aliases, dc.description = $description,
                        dc.column_type = $column_type,
                        dc.sample_values = $sample_values,
                        dc.enum_value_descriptions = $enum_value_descriptions,
                        dc.data_type = $data_type,
                        dc.dataset_key = $dataset_key_val,
                        dc.composite = $composite,
                        dc.composite_desc = $composite_desc,
                        dc.datasource_id = $datasource_id,
                        dc.zone = 'metadata'
          ON MATCH  SET dc.name = $name, dc.display_name = $display_name,
                        dc.aliases = $aliases, dc.description = $description,
                        dc.column_type = $column_type,
                        dc.sample_values = $sample_values,
                        dc.enum_value_descriptions = $enum_value_descriptions,
                        dc.data_type = $data_type,
                        dc.dataset_key = $dataset_key_val,
                        dc.composite = $composite,
                        dc.composite_desc = $composite_desc,
                        dc.datasource_id = $datasource_id,
                        dc.zone = 'metadata'
        WITH dc
        OPTIONAL MATCH (ds:Dataset {key: $ds_key})
        FOREACH (_ IN CASE WHEN ds IS NULL THEN [] ELSE [ds] END |
            MERGE (ds)-[:HAS_COLUMN]->(dc)
        )
        WITH dc
        OPTIONAL MATCH (col:Column {key: $col_key})
        FOREACH (_ IN CASE WHEN col IS NULL THEN [] ELSE [col] END |
            MERGE (dc)-[:DERIVED_FROM]->(col)
        )
        """,
        dscol_key=dscol_key,
        name=name,
        display_name=display_name,
        aliases=aliases,
        description=description,
        column_type=column_type,
        sample_values=sample_values,
        enum_value_descriptions=enum_value_descriptions,
        data_type=data_type,
        dataset_key_val=dataset_key_val,
        ds_key=ds_key,
        col_key=col_key,
        composite=composite,
        composite_desc=composite_desc,
        datasource_id=datasource_id,
    )


def _write_dataset_column_composed_of_tx(
    tx, *, dscol_key: str, col_key: str, role: str,
) -> None:
    """``DatasetColumn -[:COMPOSED_OF {role}]-> Column``。"""
    tx.run(
        """
        MATCH (dc:DatasetColumn {key: $dscol_key})
        OPTIONAL MATCH (col:Column {key: $col_key})
        FOREACH (_ IN CASE WHEN col IS NULL THEN [] ELSE [col] END |
            MERGE (dc)-[r:COMPOSED_OF]->(col)
            ON CREATE SET r.role = $role
            ON MATCH  SET r.role = $role
        )
        """,
        dscol_key=dscol_key,
        col_key=col_key,
        role=role,
    )


def _resolve_ddl_path(profile: Optional[DatasetProfile]) -> Optional[Path]:
    """优先用 profile.ddl_path；否则探 ``data/test/ddl.txt`` 这个仓库内默认路径。

    返回 None 时上层 caller 直接跳过 view alias 提取
    """
    p = getattr(profile, "ddl_path", None) if profile is not None else None
    if p and Path(p).exists():
        return Path(p)
    # 兜底：仓库默认 appdata DDL
    here = Path(__file__).resolve().parent.parent.parent.parent  # 包根（src 上一级）
    fallback = here / "data" / "test" / "ddl.txt"
    if fallback.exists() and (profile is None or profile.name == "appdata"):
        return fallback
    return None


# ---------------------------------------------------------------------- #
# 派生 post-pass 已拆到 :mod:`semantic_derive`，本模块仅 re-export 给老 import 路径用。
# ---------------------------------------------------------------------- #

def _write_domain(session, dom: dict, *, db_id: str, schema: str, pending: list[_PendingBridge], profile: Optional[DatasetProfile] = None, dsrc_key: str = "", table_db_map: Optional[dict] = None, datasource_id: str = "") -> None:
    table_db_map = table_db_map or {}
    dom_name = str(dom.get("name") or "").strip()
    dom_key_str = str(dom.get("key") or domain_key(dom_name, datasource_id))
    if not dom_name:
        log.warning("domain without name, skipped: key=%r", dom_key_str)
        return

    # ---- Domain 节点 ----
    session.execute_write(
        _write_domain_node,
        dom_key=dom_key_str,
        name=dom_name,
        description=dom.get("description") or "",
        status=str(dom.get("status") or "stable"),
        display_name=str(dom.get("display_name") or dom_name),
        aliases=_str_list(dom.get("aliases")),
        datasource_id=datasource_id,
    )

    # ---- DataSource → HAS_DOMAIN → Domain ----
    if dsrc_key:
        session.execute_write(
            _write_datasource_has_domain,
            dsrc_key=dsrc_key,
            dom_key=dom_key_str,
        )

    # ---- 维度（先于 metric，方便 ANALYZED_BY 端点都已存在）----
    dimensions = list(dom.get("dimensions") or [])
    for dim in dimensions:
        _write_dimension(
            session,
            dim,
            domain=dom_name,
            domain_key_str=dom_key_str,
            db_id=db_id,
            schema=schema,
            pending=pending,
            table_db_map=table_db_map,
            datasource_id=datasource_id,
        )
    # 二次遍历：父维度 HAS_PARENT（YAML 里 parent 是名字，不是 key）
    for dim in dimensions:
        parent_name = (dim.get("parent") or "").strip() if dim.get("parent") else ""
        if not parent_name:
            continue
        session.execute_write(
            _write_dim_parent,
            child_key=str(dim.get("key") or dim_key(dom_name, str(dim["name"]), datasource_id)),
            parent_key=dim_key(dom_name, parent_name, datasource_id),
        )

    # ---- 口径 (Caliber) ----
    for cal in dom.get("calibers") or []:
        _write_caliber(
            session,
            cal,
            domain=dom_name,
            domain_key_str=dom_key_str,
            db_id=db_id,
            schema=schema,
            pending=pending,
            table_db_map=table_db_map,
            datasource_id=datasource_id,
        )

    # ---- 指标 + 公式 ----
    for met in dom.get("metrics") or []:
        _write_metric(
            session,
            met,
            domain=dom_name,
            domain_key_str=dom_key_str,
            db_id=db_id,
            schema=schema,
            pending=pending,
            profile=profile,
            table_db_map=table_db_map,
            datasource_id=datasource_id,
        )

    # 二次遍历：DERIVED_FROM（保证两端 metric 都已经写好）
    for met in dom.get("metrics") or []:
        derived = list(met.get("derived_from") or [])
        if not derived:
            continue
        m_name = str(met.get("name") or "")
        m_key = str(met.get("key") or metric_key(dom_name, m_name, datasource_id))
        edges: list[dict] = []
        for ref in derived:
            if not isinstance(ref, dict):
                continue
            tgt_key = ref.get("metric_key")
            if not tgt_key and ref.get("metric_name"):
                tgt_key = metric_key(dom_name, str(ref["metric_name"]), datasource_id)
            if not tgt_key:
                continue
            edges.append(
                {
                    "tgt_key": str(tgt_key),
                    "relation_type": str(ref.get("relation_type") or "ratio_decompose"),
                    "role": str(ref.get("role") or ""),
                }
            )
        if edges:
            session.execute_write(_write_metric_derived_from, src_key=m_key, edges=edges)


# ---------------------------------------------------------------------- #
# 子写入函数
# ---------------------------------------------------------------------- #


def _write_datasource_node(
    tx,
    *,
    dsrc_key: str,
    datasource_name: str,
    display_name: str = "",
    db_type: str = "",
) -> None:
    tx.run(
        """
        MERGE (dsrc:DataSource {key: $dsrc_key})
          ON CREATE SET dsrc.datasource_name = $datasource_name,
                        dsrc.display_name = $display_name,
                        dsrc.db_type = $db_type,
                        dsrc.zone = 'metadata'
          ON MATCH  SET dsrc.display_name = $display_name,
                        dsrc.db_type = $db_type,
                        dsrc.zone = 'metadata'
        """,
        dsrc_key=dsrc_key,
        datasource_name=datasource_name,
        display_name=display_name or datasource_name,
        db_type=db_type,
    )


def _write_datasource_has_domain(tx, *, dsrc_key: str, dom_key: str) -> None:
    tx.run(
        """
        MATCH (dsrc:DataSource {key: $dsrc_key})
        MATCH (dom:Domain {key: $dom_key})
        MERGE (dsrc)-[:HAS_DOMAIN]->(dom)
        """,
        dsrc_key=dsrc_key,
        dom_key=dom_key,
    )


def _write_domain_node(
    tx,
    *,
    dom_key: str,
    name: str,
    description: str,
    status: str,
    display_name: str = "",
    aliases: Optional[list[str]] = None,
    datasource_id: str = "",
) -> None:
    tx.run(
        """
        MERGE (d:Domain {key: $dom_key})
          ON CREATE SET d.name = $name, d.display_name = $display_name,
                        d.description = $description, d.aliases = $aliases,
                        d.status = $status, d.datasource_id = $datasource_id,
                        d.zone = 'metadata'
          ON MATCH  SET d.name = $name, d.display_name = $display_name,
                        d.description = $description, d.aliases = $aliases,
                        d.status = $status, d.datasource_id = $datasource_id,
                        d.zone = 'metadata'
        """,
        dom_key=dom_key,
        name=name,
        display_name=display_name or name,
        description=description,
        aliases=aliases or [],
        status=status,
        datasource_id=datasource_id,
    )


def _write_dimension(
    session,
    dim: dict,
    *,
    domain: str,
    domain_key_str: str,
    db_id: str,
    schema: str,
    pending: list[_PendingBridge],
    table_db_map: Optional[dict] = None,
    datasource_id: str = "",
) -> None:
    name = str(dim.get("name") or "").strip()
    if not name:
        return
    d_key = str(dim.get("key") or dim_key(domain, name, datasource_id))
    status = str(dim.get("status") or "stable")
    skip_bridge = _is_skipped_status(status)

    session.execute_write(
        _write_dim_node,
        dim_key=d_key,
        domain=domain,
        domain_key=domain_key_str,
        name=name,
        dimension_type=str(dim.get("dimension_type") or "OLAP维度"),
        data_type=str(dim.get("data_type") or "text"),
        synonyms=_str_list(dim.get("synonyms")),
        hierarchy_level=int(dim.get("hierarchy_level") or 0),
        description=str(dim.get("description") or ""),
        status=status,
        dataset_name=str(dim.get("dataset_name") or ""),
        is_display_dimension=_bool_flag(dim.get("is_display_dimension"), default=True),
        is_contribution_dimension=_bool_flag(dim.get("is_contribution_dimension"), default=True),
        datasource_id=datasource_id,
    )

    maps = dim.get("maps_to_columns") or []
    if maps and not skip_bridge:
        edges = []
        for m in maps:
            qcol = str(m.get("column") or "").strip()
            if not qcol:
                continue
            try:
                cdb, csch, ctbl, cname = split_qualified_column(
                    qcol,
                    default_db=_default_db_for_ref(table_db_map, domain, qcol, db_id, is_column=True),
                )
            except ValueError:
                continue
            ds_name = m.get("dataset_name") or logical_dataset_name(ctbl)
            edges.append(
                {
                    "dscol_key": dataset_column_key(domain, ds_name, cname, datasource_id),
                    "col_key": column_key(cdb, csch, ctbl, cname, datasource_id),
                    "binding_type": str(m.get("binding_type") or dim.get("dimension_type") or ""),
                    "expr": str(m.get("expr") or ""),
                    "filter": str(m.get("filter") or ""),
                    "col_repr": qcol,
                }
            )
        if edges:
            session.execute_write(_write_dim_maps_to_prefer_semantic, dim_key=d_key, edges=edges)
    elif maps and skip_bridge:
        pending.append(_PendingBridge("MAPS_TO_COLUMN", f"{d_key} status={status}"))

    values = dim.get("values") or []
    rows = dim_value_rows(domain, name, values, datasource_id=datasource_id)
    if rows:
        session.execute_write(_write_dim_values, dim_key=d_key, rows=rows, datasource_id=datasource_id)


def _write_dim_node(
    tx,
    *,
    dim_key: str,
    domain: str,
    domain_key: str,
    name: str,
    dimension_type: str,
    data_type: str,
    synonyms: list[str],
    hierarchy_level: int,
    description: str,
    status: str,
    dataset_name: str = "",
    is_display_dimension: bool = True,
    is_contribution_dimension: bool = True,
    datasource_id: str = "",
) -> None:
    tx.run(
        """
        MERGE (d:Dimension {key: $dim_key})
          ON CREATE SET d.domain = $domain, d.name = $name,
                        d.dimension_type = $dimension_type, d.data_type = $data_type,
                        d.aliases = $synonyms, d.hierarchy_level = $hierarchy_level,
                        d.description = $description, d.dataset_name = $dataset_name,
                        d.is_display_dimension = $is_display_dimension,
                        d.is_contribution_dimension = $is_contribution_dimension,
                        d.status = $status, d.datasource_id = $datasource_id,
                        d.zone = 'metadata'
          ON MATCH  SET d.aliases = $synonyms, d.hierarchy_level = $hierarchy_level,
                        d.description = $description,
                        d.dimension_type = $dimension_type, d.data_type = $data_type,
                        d.dataset_name = $dataset_name,
                        d.is_display_dimension = $is_display_dimension,
                        d.is_contribution_dimension = $is_contribution_dimension,
                        d.status = $status, d.datasource_id = $datasource_id,
                        d.zone = 'metadata'
        WITH d
        MATCH (dom:Domain {key: $domain_key})
        MERGE (dom)-[:HAS_DIMENSION]->(d)
        """,
        dim_key=dim_key,
        domain=domain,
        domain_key=domain_key,
        name=name,
        dimension_type=dimension_type,
        data_type=data_type,
        synonyms=synonyms,
        hierarchy_level=hierarchy_level,
        description=description,
        dataset_name=dataset_name,
        is_display_dimension=is_display_dimension,
        is_contribution_dimension=is_contribution_dimension,
        status=status,
        datasource_id=datasource_id,
    )


def _write_dim_parent(tx, *, child_key: str, parent_key: str) -> None:
    tx.run(
        """
        MATCH (child:Dimension {key: $child_key})
        MATCH (parent:Dimension {key: $parent_key})
        MERGE (child)-[:HAS_PARENT]->(parent)
        """,
        child_key=child_key,
        parent_key=parent_key,
    )


def _write_dim_maps_to_prefer_semantic(tx, *, dim_key: str, edges: list[dict]) -> None:
    """优先连 DatasetColumn (MAPS_TO_DATASET_COLUMN)；不存在时降级连物理 Column (MAPS_TO_COLUMN)。"""
    result = tx.run(
        """
        MATCH (d:Dimension {key: $dim_key})
        UNWIND $edges AS e
        OPTIONAL MATCH (dc:DatasetColumn {key: e.dscol_key})
        OPTIONAL MATCH (col:Column {key: e.col_key})
        WITH d, e, dc, col,
             CASE WHEN dc IS NOT NULL THEN 'semantic'
                  WHEN col IS NOT NULL THEN 'physical'
                  ELSE 'missing' END AS resolved
        FOREACH (_ IN CASE WHEN dc IS NOT NULL THEN [1] ELSE [] END |
            MERGE (d)-[r:MAPS_TO_DATASET_COLUMN]->(dc)
              ON CREATE SET r.binding_type = e.binding_type, r.expr = e.expr, r.filter = e.filter
              ON MATCH  SET r.binding_type = e.binding_type, r.expr = e.expr, r.filter = e.filter
        )
        FOREACH (_ IN CASE WHEN dc IS NULL AND col IS NOT NULL THEN [1] ELSE [] END |
            MERGE (d)-[r:MAPS_TO_COLUMN]->(col)
              ON CREATE SET r.expr = e.expr, r.filter = e.filter
              ON MATCH  SET r.expr = e.expr, r.filter = e.filter
        )
        RETURN e.col_repr AS col_repr, resolved
        """,
        dim_key=dim_key,
        edges=edges,
    )
    for rec in result:
        resolved = rec["resolved"]
        if resolved == "physical":
            log.warning(
                "Dimension %s MAPS_TO fallback to physical Column for %s "
                "(DatasetColumn not found — check config)",
                dim_key, rec["col_repr"],
            )
        elif resolved == "missing":
            log.warning(
                "Dimension %s MAPS_TO: neither DatasetColumn nor Column found for %s",
                dim_key, rec["col_repr"],
            )


def _write_dim_values(tx, *, dim_key: str, rows: list[dict], datasource_id: str = "") -> None:
    tx.run(
        """
        MATCH (d:Dimension {key: $dim_key})
        UNWIND $rows AS r
        MERGE (dv:DimensionValue {key: r.dv_key})
          ON CREATE SET dv.dimension_key = $dim_key, dv.value = r.value,
                        dv.label = r.label, dv.occur_cnt = coalesce(r.occur_cnt, 0),
                        dv.datasource_id = $datasource_id,
                        dv.zone = 'metadata'
          ON MATCH  SET dv.value = r.value, dv.label = r.label,
                        dv.occur_cnt = coalesce(r.occur_cnt, dv.occur_cnt, 0),
                        dv.datasource_id = $datasource_id,
                        dv.zone = 'metadata'
        MERGE (d)-[:HAS_VALUE]->(dv)
        """,
        dim_key=dim_key,
        rows=rows,
        datasource_id=datasource_id,
    )


def _write_caliber(
    session,
    cal: dict,
    *,
    domain: str,
    domain_key_str: str,
    db_id: str,
    schema: str,
    pending: list[_PendingBridge],
    table_db_map: Optional[dict] = None,
    datasource_id: str = "",
) -> None:
    qcol = str(cal.get("column") or "").strip()
    value = str(cal.get("value") or "").strip()
    if not (qcol and value):
        return
    try:
        cdb, csch, ctbl, cname = split_qualified_column(
            qcol,
            default_db=_default_db_for_ref(table_db_map, domain, qcol, db_id, is_column=True),
        )
    except ValueError:
        log.warning("caliber column not parseable: %r", qcol)
        return
    col_short = f"{ctbl}.{cname}"
    cal_key_str = str(cal.get("key") or caliber_key(domain, col_short, value, datasource_id))
    col_k = column_key(cdb, csch, ctbl, cname, datasource_id)
    session.execute_write(
        _write_caliber_node,
        cal_key=cal_key_str,
        domain=domain,
        col_key=col_k,
        name=str(cal.get("name") or col_short),
        value=value,
        description=str(cal.get("description") or ""),
        predicate_template=str(cal.get("predicate_template") or f"{cname} = '{value}'"),
        aliases=_str_list(cal.get("aliases") or cal.get("synonyms") or []),
        datasource_id=datasource_id,
    )


def _write_caliber_node(
    tx,
    *,
    cal_key: str,
    domain: str,
    col_key: str,
    name: str,
    value: str,
    description: str,
    predicate_template: str,
    aliases: list[str],
    datasource_id: str = "",
) -> None:
    tx.run(
        """
        MERGE (cal:Caliber {key: $cal_key})
          ON CREATE SET cal.domain = $domain, cal.column_key = $col_key,
                        cal.name = $name, cal.value = $value, cal.description = $description,
                        cal.predicate_template = $predicate_template, cal.aliases = $aliases,
                        cal.datasource_id = $datasource_id,
                        cal.zone = 'metadata'
          ON MATCH  SET cal.name = $name, cal.value = $value, cal.description = $description,
                        cal.predicate_template = $predicate_template, cal.aliases = $aliases,
                        cal.datasource_id = $datasource_id,
                        cal.zone = 'metadata'
        WITH cal
        OPTIONAL MATCH (col:Column {key: $col_key})
        FOREACH (_ IN CASE WHEN col IS NULL THEN [] ELSE [col] END |
            MERGE (cal)-[:FILTER_ON]->(col)
        )
        """,
        cal_key=cal_key,
        domain=domain,
        col_key=col_key,
        name=name,
        value=value,
        description=description,
        predicate_template=predicate_template,
        aliases=aliases,
        datasource_id=datasource_id,
    )


def _write_metric(
    session,
    met: dict,
    *,
    domain: str,
    domain_key_str: str,
    db_id: str,
    schema: str,
    pending: list[_PendingBridge],
    profile: Optional[DatasetProfile] = None,
    table_db_map: Optional[dict] = None,
    datasource_id: str = "",
) -> None:
    name = str(met.get("name") or "").strip()
    if not name:
        return
    m_key = str(met.get("key") or metric_key(domain, name, datasource_id))
    status = str(met.get("status") or "stable")
    skip_bridge = _is_skipped_status(status)

    is_north = _bool_flag(met.get("is_north_star"))
    is_disp = _bool_flag(met.get("is_display"))
    is_disp_dist = _bool_flag(met.get("is_display_distribution"))
    role = metric_role_from_props(
        {
            "is_north_star": is_north,
            "is_display": is_disp,
            "is_display_distribution": is_disp_dist,
            "role": met.get("role"),
        }
    )
    session.execute_write(
        _write_metric_node,
        met_key=m_key,
        domain=domain,
        domain_key=domain_key_str,
        name=name,
        type=str(met.get("type") or "quantity"),
        synonyms=_str_list(met.get("synonyms")),
        is_north_star=is_north,
        is_display=is_disp,
        is_display_distribution=is_disp_dist,
        role=role,
        anomaly_rules_json=anomaly_rules_to_json(met.get("anomaly_rules")),
        description=str(met.get("description") or met.get("notes") or ""),
        tags=_str_list(met.get("tags")),
        unit=str(met.get("unit") or ""),
        status=status,
        valid_to=str(met.get("valid_to") or ""),
        datasource_id=datasource_id,
    )

    if skip_bridge:
        pending.append(_PendingBridge("Metric.bridges", f"{m_key} status={status}"))
        return

    # ---- formulas ----
    formulas = list(met.get("formulas") or [])
    for f in formulas:
        _write_formula(
            session,
            f,
            domain=domain,
            domain_key_str=domain_key_str,
            metric_name=name,
            metric_key_str=m_key,
            db_id=db_id,
            schema=schema,
            profile=profile,
            table_db_map=table_db_map,
            datasource_id=datasource_id,
        )

    # ---- analyzed_by (v4) / legacy YAML key can_drill_by ----
    drill = _str_list(met.get("analyzed_by")) or _str_list(met.get("can_drill_by"))
    if drill:
        session.execute_write(
            _write_metric_drill,
            met_key=m_key,
            dim_keys=[dim_key(domain, d, datasource_id) for d in drill],
        )


    # ---- optional caliber links (YAML ``caliber_keys``: list of Caliber ``key``) ----
    cal_keys = [str(x) for x in (met.get("caliber_keys") or met.get("calibers") or []) if x]
    if cal_keys:
        session.execute_write(_write_metric_calibers, met_key=m_key, cal_keys=cal_keys)

    corr_names = _str_list(met.get("correlated_with")) or _str_list(met.get("correlated_metrics"))
    if corr_names:
        peer_keys = [metric_key(domain, str(n).strip(), datasource_id) for n in corr_names if str(n).strip()]
        if peer_keys:
            session.execute_write(_write_metric_correlations, met_key=m_key, peer_keys=peer_keys)

def _write_metric_node(
    tx,
    *,
    met_key: str,
    domain: str,
    domain_key: str,
    name: str,
    type: str,
    synonyms: list[str],
    is_north_star: bool,
    is_display: bool,
    is_display_distribution: bool,
    role: str,
    anomaly_rules_json: str,
    description: str,
    tags: list[str],
    unit: str,
    status: str,
    valid_to: str,
    datasource_id: str = "",
) -> None:
    """v3 uses valid_to to mark retired metrics; empty means active."""
    tx.run(
        """
        MERGE (m:Metric {key: $met_key})
          ON CREATE SET m.domain = $domain, m.name = $name, m.type = $type,
                        m.aliases = $synonyms, m.is_north_star = $is_north_star,
                        m.is_display = $is_display, m.is_display_distribution = $is_display_distribution,
                        m.role = $role, m.anomaly_rules_json = $anomaly_rules_json,
                        m.description = $description, m.tags = $tags, m.unit = $unit,
                        m.status = $status, m.datasource_id = $datasource_id,
                        m.valid_to = CASE WHEN $valid_to <> '' THEN datetime($valid_to) ELSE null END,
                        m.zone = 'metadata'
          ON MATCH  SET m.aliases = $synonyms, m.is_north_star = $is_north_star,
                        m.is_display = $is_display, m.is_display_distribution = $is_display_distribution,
                        m.role = $role, m.anomaly_rules_json = $anomaly_rules_json,
                        m.description = $description, m.tags = $tags, m.unit = $unit,
                        m.type = $type, m.status = $status, m.datasource_id = $datasource_id,
                        m.valid_to = CASE WHEN $valid_to <> '' THEN datetime($valid_to) ELSE null END,
                        m.zone = 'metadata'
        WITH m
        MATCH (dom:Domain {key: $domain_key})
        MERGE (dom)-[:HAS_METRIC]->(m)
        """,
        met_key=met_key,
        domain=domain,
        domain_key=domain_key,
        name=name,
        type=type,
        synonyms=synonyms,
        is_north_star=is_north_star,
        is_display=is_display,
        is_display_distribution=is_display_distribution,
        role=role,
        anomaly_rules_json=anomaly_rules_json,
        description=description,
        tags=tags,
        unit=unit,
        status=status,
        valid_to=valid_to,
        datasource_id=datasource_id,
    )


def _write_formula(
    session,
    f: dict,
    *,
    domain: str,
    domain_key_str: str,
    metric_name: str,
    metric_key_str: str,
    db_id: str,
    schema: str,
    profile: Optional[DatasetProfile] = None,
    table_db_map: Optional[dict] = None,
    datasource_id: str = "",
) -> None:
    dataset = str(f.get("dataset") or "").strip()
    if not dataset:
        return
    physical_table = str(f.get("physical_table") or dataset).strip()
    short = dataset_short(physical_table, domain=domain, profile=profile)
    if "." in dataset:
        logical_name = logical_dataset_name(dataset, default_db=db_id)
    else:
        logical_name = dataset
    ds_logical_name = logical_name or short
    key_disambig = logical_name or short
    date_range_value = str(f.get("date_range") or "").strip()
    role_value = str(f.get("role") or "").strip().lower()
    if role_value not in ("numerator", "denominator"):
        role_value = ""
    base_key = formula_key(domain, metric_name, key_disambig, date_range_value, datasource_id)
    if role_value:
        base_key = f"{base_key}:{role_value}"
    f_key = str(f.get("key") or base_key)

    # 公式的所有列都属于它自己的 physical_table，按 (domain, 表名/数据集名) 解析 db，
    # 同名表跨域复用时也能选对数据源；解析出的 db 同时用于表与列。
    fml_default_db = _default_db_for_ref(table_db_map, domain, physical_table, db_id, is_column=False)
    if fml_default_db == db_id and dataset:
        fml_default_db = table_db_map.get((domain, dataset), db_id) if table_db_map else db_id
    try:
        tdb, tsch, tname = split_qualified_table(physical_table, default_db=fml_default_db)
    except ValueError:
        log.warning("formula physical_table not parseable: %r", physical_table)
        return
    tbl_k = table_key(tdb, tsch, tname, datasource_id)

    used = list(f.get("uses_columns") or [])

    # Formula writing doesn't create Dataset nodes; it matches existing ones by name+domain
    # (Dataset nodes are created by write_datasets with datasource_id in the key)

    # Formula 节点 + HAS_FORMULA + OF_VIEW→Dataset + CONTAINS_TABLE→Table
    session.execute_write(
        _write_formula_node_tx,
        fml_key=f_key,
        met_key=metric_key_str,
        domain=domain,
        domain_key_str=domain_key_str,
        dataset=dataset,
        ds_name=ds_logical_name,
        parents=tname,
        formula=str(f.get("formula") or ""),
        evidence=str(f.get("formula_evidence") or ""),
        date_range=str(f.get("date_range") or ""),
        role=role_value,
        tbl_key=tbl_k,
        datasource_id=datasource_id,
    )

    # USES_COLUMN：优先连 DatasetColumn（语义列），无语义列时降级连物理 Column 并告警
    dscol_rows: list[dict] = []
    for u in used:
        qcol = str(u.get("column") or "").strip()
        if not qcol:
            continue
        try:
            cdb, csch, ctbl, cname = split_qualified_column(qcol, default_db=fml_default_db)
        except ValueError:
            continue
        ds_name_for_col = ds_logical_name or logical_dataset_name(ctbl, default_db=db_id)
        dscol_rows.append(
            {
                "dscol_key": dataset_column_key(domain, ds_name_for_col, cname, datasource_id),
                "col_key": column_key(cdb, csch, ctbl, cname, datasource_id),
                "role": str(u.get("role") or "numerator"),
                "col_repr": qcol,
            }
        )
    if dscol_rows:
        session.execute_write(
            _write_formula_uses_column_prefer_semantic,
            fml_key=f_key,
            rows=dscol_rows,
        )


def _write_formula_node_tx(
    tx,
    *,
    fml_key: str,
    met_key: str,
    domain: str,
    domain_key_str: str,
    dataset: str,
    ds_name: str,
    parents: str,
    formula: str,
    evidence: str,
    date_range: str,
    role: str,
    tbl_key: str,
    datasource_id: str = "",
) -> None:
    tx.run(
        """
        MATCH (m:Metric {key: $met_key})
        MERGE (f:Formula {key: $fml_key})
          ON CREATE SET f.domain = $domain, f.metric_key = m.key, f.dataset = $dataset,
                        f.formula = $formula, f.formula_evidence = $evidence,
                        f.date_range = $date_range, f.role = $role,
                        f.datasource_id = $datasource_id, f.zone = 'metadata'
          ON MATCH  SET f.dataset = $dataset, f.formula = $formula, f.formula_evidence = $evidence,
                        f.date_range = $date_range, f.role = $role,
                        f.datasource_id = $datasource_id, f.zone = 'metadata'
        MERGE (m)-[:HAS_FORMULA]->(f)
        """,
        met_key=met_key,
        fml_key=fml_key,
        domain=domain,
        dataset=dataset,
        formula=formula,
        evidence=evidence,
        date_range=date_range,
        role=role,
        datasource_id=datasource_id,
    )
    tx.run(
        """
        MATCH (f:Formula {key: $fml_key})
        OPTIONAL MATCH (t:Table {key: $tbl_key})
        MATCH (ds:Dataset {name: $ds_name, domain: $domain})
        WITH f, ds, t, $domain_key_str AS domk
        OPTIONAL MATCH (dom:Domain {key: domk})
        FOREACH (_ IN CASE WHEN dom IS NULL THEN [] ELSE [dom] END |
            MERGE (dom)-[:HAS_DATASET]->(ds)
        )
        WITH f, ds, t
        FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [t] END |
            MERGE (ds)-[:CONTAINS_TABLE]->(t)
        )
        MERGE (f)-[:OF_VIEW]->(ds)
        """,
        fml_key=fml_key,
        tbl_key=tbl_key,
        ds_name=ds_name,
        domain=domain,
        parents=parents,
        refresh_freq=date_range or "daily",
        domain_key_str=domain_key_str,
    )


def _write_formula_uses_column_prefer_semantic(tx, *, fml_key: str, rows: list[dict]) -> None:
    """优先连 DatasetColumn；不存在时降级连物理 Column 并返回 fallback 列表供调用方告警。"""
    result = tx.run(
        """
        MATCH (f:Formula {key: $fml_key})
        UNWIND $rows AS row
        OPTIONAL MATCH (dc:DatasetColumn {key: row.dscol_key})
        OPTIONAL MATCH (col:Column {key: row.col_key})
        WITH f, row, dc, col,
             CASE WHEN dc IS NOT NULL THEN 'semantic'
                  WHEN col IS NOT NULL THEN 'physical'
                  ELSE 'missing' END AS resolved
        FOREACH (_ IN CASE WHEN dc IS NOT NULL THEN [1] ELSE [] END |
            MERGE (f)-[r:USES_COLUMN]->(dc)
              ON CREATE SET r.role = row.role
              ON MATCH  SET r.role = row.role
        )
        FOREACH (_ IN CASE WHEN dc IS NULL AND col IS NOT NULL THEN [1] ELSE [] END |
            MERGE (f)-[r:USES_COLUMN]->(col)
              ON CREATE SET r.role = row.role
              ON MATCH  SET r.role = row.role
        )
        RETURN row.col_repr AS col_repr, resolved
        """,
        fml_key=fml_key,
        rows=rows,
    )
    for rec in result:
        resolved = rec["resolved"]
        if resolved == "physical":
            log.warning(
                "Formula %s USES_COLUMN fallback to physical Column for %s "
                "(DatasetColumn not found — check config)",
                fml_key, rec["col_repr"],
            )
        elif resolved == "missing":
            log.warning(
                "Formula %s USES_COLUMN: neither DatasetColumn nor Column found for %s",
                fml_key, rec["col_repr"],
            )


def _write_metric_calibers(tx, *, met_key: str, cal_keys: list[str]) -> None:
    tx.run(
        """
        MATCH (m:Metric {key: $met_key})
        UNWIND $cal_keys AS ck
        OPTIONAL MATCH (cal:Caliber {key: ck})
        FOREACH (_ IN CASE WHEN cal IS NULL THEN [] ELSE [cal] END |
            MERGE (m)-[:HAS_CALIBER]->(cal)
        )
        """,
        met_key=met_key,
        cal_keys=cal_keys,
    )


def _write_metric_drill(tx, *, met_key: str, dim_keys: list[str]) -> None:
    tx.run(
        """
        MATCH (m:Metric {key: $met_key})
        UNWIND $dim_keys AS dk
        OPTIONAL MATCH (d:Dimension {key: dk})
        FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [d] END |
            MERGE (m)-[:ANALYZED_BY]->(d)
        )
        """,
        met_key=met_key,
        dim_keys=dim_keys,
    )


def _write_metric_correlations(tx, *, met_key: str, peer_keys: list[str]) -> None:
    """``Metric -[:CORRELATED_WITH]-> Metric``（YAML ``correlated_with`` / ``correlated_metrics``）。"""
    tx.run(
        """
        MATCH (m:Metric {key: $met_key})
        UNWIND $peer_keys AS pk
        OPTIONAL MATCH (p:Metric {key: pk})
        FOREACH (_ IN CASE WHEN p IS NULL OR id(m) = id(p) THEN [] ELSE [p] END |
            MERGE (m)-[:CORRELATED_WITH]->(p)
        )
        """,
        met_key=met_key,
        peer_keys=peer_keys,
    )


def _write_metric_derived_from(tx, *, src_key: str, edges: list[dict]) -> None:
    tx.run(
        """
        MATCH (src:Metric {key: $src_key})
        UNWIND $edges AS e
        OPTIONAL MATCH (tgt:Metric {key: e.tgt_key})
        FOREACH (_ IN CASE WHEN tgt IS NULL THEN [] ELSE [tgt] END |
            MERGE (src)-[r:DERIVED_FROM {relation_type: e.relation_type, role: e.role}]->(tgt)
        )
        """,
        src_key=src_key,
        edges=edges,
    )


# ---------------------------------------------------------------------- #
# 公共导出
# ---------------------------------------------------------------------- #
__all__ = [
    "TOPLINE_LITERALS_DEFAULT",
    "TOPLINE_LITERALS_RICH",
    "backfill_dimension_supplement",
    "backfill_metric_units",
    "derive_analyzed_by_from_topology",
    "derive_calibers_from_formulas",
    "derive_column_time_grain",
    "derive_granularity_partitions",
    "derive_partitions_from_comments",
    "derive_partitions_from_data",
    "derive_view_alias_synonyms",
    "ingest_semantic",
    "load_metrics_dict",
    "write_dataset_columns",
    "parse_topline_filters",
    "write_datasets",
    "write_domains",
]


# 让静态检查器看到 METADATA_ZONE 没有被使用导致的 unused 警告消除（语义上 zone 已硬写在 Cypher 里）
_ZONE_REF = METADATA_ZONE

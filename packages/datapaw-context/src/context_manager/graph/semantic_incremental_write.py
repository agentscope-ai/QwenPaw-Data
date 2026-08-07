"""增量 upsert / delete（按 sheet → 专用 Cypher）。

被 ``semantic_diff.apply_diff`` 调用。每个 sheet 一个 upsert_rows + 共用 delete_rows。
不 wipe，纯 MERGE upsert；删除 scoped + 引用计数已在 diff 阶段守卫。
"""
from __future__ import annotations

from typing import Any

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from .keys import (
    dataset_column_key,
    dataset_key,
    dim_key,
    dim_value_key,
    domain_key,
    formula_key,
    metric_key,
)

log = get_logger("graph.semantic_incremental_write")


def _ds_clause(alias: str = "n") -> str:
    return f"($ds = '' OR {alias}.datasource_id = $ds)"


# ---------------------------------------------------------------------- #
# upsert per sheet
# ---------------------------------------------------------------------- #

def upsert_rows(driver: Driver, sheet: str, rows: list[dict], *, datasource_id: str) -> int:
    fn = _UPSERT_DISPATCH.get(sheet)
    if not fn:
        log.warning("upsert not implemented for sheet=%s, skipping %d rows", sheet, len(rows))
        return 0
    n = 0
    with neo4j_session(driver) as s:
        for r in rows:
            try:
                fn(s, r, datasource_id=datasource_id)
                n += 1
            except Exception:
                log.exception("upsert %s row failed: %s", sheet, r.get("_row_key"))
    return n


def _upsert_domain(s, r: dict, *, datasource_id: str) -> None:
    domain = str(r.get("domain_name") or "").strip()
    if not domain:
        return
    s.run(
        f"""
        MERGE (d:Domain {{key: $key}})
          ON CREATE SET d.name = $name, d.datasource_id = $ds
          ON MATCH  SET d.display_name = $display, d.description = $desc,
                        d.aliases = $aliases, d.datasource_id = $ds
        WITH d
        MERGE (dsrc:DataSource {{key: 'dsrc:' + $ds}})
          ON CREATE SET dsrc.datasource_name = $ds
        MERGE (dsrc)-[:HAS_DOMAIN]->(d)
        """,
        key=domain_key(domain, datasource_id),
        name=domain, display=str(r.get("display_name") or domain),
        desc=str(r.get("description") or ""),
        aliases=[x for x in (r.get("aliases") or []) if x],
        ds=datasource_id,
    )


def _upsert_dataset(s, r: dict, *, datasource_id: str) -> None:
    domain = str(r.get("domain_name") or "").strip()
    name = str(r.get("dataset_name") or "").strip()
    if not (domain and name):
        return
    s.run(
        f"""
        MERGE (dom:Domain {{key: $dom_key}})
          ON CREATE SET dom.name = $domain, dom.datasource_id = $ds
        MERGE (ds:Dataset {{key: $ds_key}})
          ON CREATE SET ds.name = $name, ds.domain = $domain, ds.datasource_id = $ds
          ON MATCH  SET ds.description = $desc, ds.dataset_type = $type,
                        ds.sql = $sql, ds.parents = $parents
        MERGE (dom)-[:HAS_DATASET]->(ds)
        """,
        dom_key=domain_key(domain, datasource_id),
        ds_key=dataset_key(domain, name, datasource_id),
        domain=domain, name=name,
        desc=str(r.get("dataset_comment") or ""),
        type=str(r.get("dataset_type") or "OLAP"),
        sql=str(r.get("sql_content") or ""),
        parents=str(r.get("parents") or ""),
        ds=datasource_id,
    )


def _upsert_dataset_column(s, r: dict, *, datasource_id: str) -> None:
    domain = str(r.get("domain_name") or "").strip()
    ds_name = str(r.get("dataset_name") or "").strip()
    col = str(r.get("column_name") or "").strip()
    if not (domain and ds_name and col):
        return
    s.run(
        f"""
        MERGE (ds:Dataset {{key: $ds_key}})
          ON CREATE SET ds.name = $ds_name, ds.domain = $domain, ds.datasource_id = $ds
        MERGE (dc:DatasetColumn {{key: $dc_key}})
          ON CREATE SET dc.name = $col, dc.domain = $domain, dc.datasource_id = $ds
          ON MATCH  SET dc.display_name = $cn, dc.data_type = $dtype,
                        dc.description = $comment, dc.column_type = $ctype,
                        dc.sample_values = $samples, dc.dimension_type = $dimtype,
                        dc.is_primary = $pk, dc.is_nullable = $nullable
        MERGE (ds)-[:HAS_COLUMN]->(dc)
        """,
        ds_key=dataset_key(domain, ds_name, datasource_id),
        dc_key=dataset_column_key(domain, ds_name, col, datasource_id),
        domain=domain, ds_name=ds_name, col=col,
        cn=str(r.get("column_name_cn") or col),
        dtype=str(r.get("data_type") or "text"),
        comment=str(r.get("column_comment") or ""),
        ctype=str(r.get("column_type") or ""),
        samples=[x for x in (r.get("samples") or []) if x],
        dimtype=str(r.get("dimension_type") or ""),
        pk=bool(r.get("is_primary")),
        nullable=bool(r.get("is_nullable", True)),
        ds=datasource_id,
    )


def _upsert_dimension(s, r: dict, *, datasource_id: str) -> None:
    domain = str(r.get("domain_name") or "").strip()
    name = str(r.get("dimension_name") or "").strip()
    if not (domain and name):
        return
    s.run(
        f"""
        MERGE (dom:Domain {{key: $dom_key}})
          ON CREATE SET dom.name = $domain, dom.datasource_id = $ds
        MERGE (d:Dimension {{key: $dim_key}})
          ON CREATE SET d.name = $name, d.domain = $domain, d.datasource_id = $ds
          ON MATCH  SET d.description = $desc, d.aliases = $syns,
                        d.hierarchy_level = $depth, d.is_display_dimension = $vis,
                        d.is_contribution_dimension = $attr,
                        d.dimension_type = $dimtype, d.data_type = $dtype
        MERGE (dom)-[:HAS_DIMENSION]->(d)
        """,
        dom_key=domain_key(domain, datasource_id),
        dim_key=dim_key(domain, name, datasource_id),
        domain=domain, name=name,
        desc=str(r.get("description") or ""),
        syns=[x for x in (r.get("synonyms") or []) if x],
        depth=int(r.get("depth") or 0),
        vis=bool(r.get("is_visible", True)),
        attr=bool(r.get("is_attribution", True)),
        dimtype="OLAP维度", dtype="text",
        ds=datasource_id,
    )
    # parent
    parent = str(r.get("parent_name") or "").strip()
    if parent:
        s.run(
            f"""
            MERGE (d:Dimension {{key: $dim_key}})
            MERGE (p:Dimension {{key: $p_key}})
              ON CREATE SET p.name = $parent, p.domain = $domain, p.datasource_id = $ds
            MERGE (d)-[:HAS_PARENT]->(p)
            """,
            dim_key=dim_key(domain, name, datasource_id),
            p_key=dim_key(domain, parent, datasource_id),
            parent=parent, domain=domain, ds=datasource_id,
        )
    # dimension values (enums)
    enums = r.get("enums") or []
    for ev in enums:
        if isinstance(ev, dict):
            val = str(ev.get("value") or "").strip()
        else:
            val = str(ev).strip()
        if not val:
            continue
        s.run(
            f"""
            MERGE (d:Dimension {{key: $dim_key}})
            MERGE (dv:DimensionValue {{key: $dv_key}})
              ON CREATE SET dv.dimension_key = $dim_key, dv.value = $val
            MERGE (d)-[:HAS_VALUE]->(dv)
            """,
            dim_key=dim_key(domain, name, datasource_id),
            dv_key=dim_value_key(domain, name, val, datasource_id),
            val=val,
        )


def _upsert_dataset_dimension(s, r: dict, *, datasource_id: str) -> None:
    domain = str(r.get("domain_name") or "").strip()
    ds_name = str(r.get("dataset_name") or "").strip()
    dim_name = str(r.get("dimension_name") or "").strip()
    expr = str(r.get("calculate_expr") or "").strip()
    if not (domain and dim_name and expr):
        return
    dimtype = str(r.get("dimension_type") or "OLAP维度")
    dtype = str(r.get("data_type") or "text")
    # 若有 dataset，连 MAPS_TO_DATASET_COLUMN；否则 MAPS_TO_COLUMN（需物理列）
    if ds_name:
        s.run(
            f"""
            MERGE (d:Dimension {{key: $dim_key}})
              ON CREATE SET d.name = $dim_name, d.domain = $domain, d.datasource_id = $ds
              ON MATCH  SET d.dimension_type = $dimtype, d.data_type = $dtype
            MERGE (ds:Dataset {{key: $ds_key}})
              ON CREATE SET ds.name = $ds_name, ds.domain = $domain, ds.datasource_id = $ds
            MERGE (ds)-[:HAS_COLUMN]->(dc:DatasetColumn {{name: $col, domain: $domain}})
            MERGE (d)-[rel:MAPS_TO_DATASET_COLUMN]->(dc)
              ON CREATE SET rel.expr = $expr
              ON MATCH  SET rel.expr = $expr
            """,
            dim_key=dim_key(domain, dim_name, datasource_id),
            ds_key=dataset_key(domain, ds_name, datasource_id),
            domain=domain, dim_name=dim_name, ds_name=ds_name,
            col=_extract_col_from_expr(expr),
            expr=expr, dimtype=dimtype, dtype=dtype, ds=datasource_id,
        )
    else:
        s.run(
            f"""
            MERGE (d:Dimension {{key: $dim_key}})
              ON CREATE SET d.name = $dim_name, d.domain = $domain, d.datasource_id = $ds
              ON MATCH  SET d.dimension_type = $dimtype, d.data_type = $dtype
            """,
            dim_key=dim_key(domain, dim_name, datasource_id),
            domain=domain, dim_name=dim_name,
            dimtype=dimtype, dtype=dtype, ds=datasource_id,
        )


def _upsert_metric(s, r: dict, *, datasource_id: str) -> None:
    domain = str(r.get("domain_name") or "").strip()
    name = str(r.get("metric_name") or "").strip()
    if not (domain and name):
        return
    s.run(
        f"""
        MERGE (dom:Domain {{key: $dom_key}})
          ON CREATE SET dom.name = $domain, dom.datasource_id = $ds
        MERGE (m:Metric {{key: $met_key}})
          ON CREATE SET m.name = $name, m.domain = $domain, m.datasource_id = $ds
          ON MATCH  SET m.description = $desc, m.unit = $unit,
                        m.is_north_star = $north, m.is_display_distribution = $dist,
                        m.is_display = $vis, m.aliases = $syns, m.tags = $tags
        MERGE (dom)-[:HAS_METRIC]->(m)
        """,
        dom_key=domain_key(domain, datasource_id),
        met_key=metric_key(domain, name, datasource_id),
        domain=domain, name=name,
        desc=str(r.get("description") or ""),
        unit=str(r.get("unit") or ""),
        north=bool(r.get("is_polaris")),
        dist=bool(r.get("show_distribution")),
        vis=bool(r.get("is_visible", True)),
        syns=[x for x in (r.get("synonyms") or []) if x],
        tags=[x for x in (r.get("tags") or []) if x],
        ds=datasource_id,
    )


def _upsert_metric_formula(s, r: dict, *, datasource_id: str) -> None:
    domain = str(r.get("domain_name") or "").strip()
    met_name = str(r.get("metric_name") or "").strip()
    ds_name = str(r.get("dataset_name") or "").strip()
    if not (domain and met_name):
        return
    formula = str(r.get("formula") or "")
    evidence = str(r.get("formula_evidence") or "")
    date_range = str(r.get("date_range") or "")
    evidence_ext = str(r.get("evidence_ext") or "")
    # formula_key 需要一个 short 值；用 evidence 或 dataset 的 tail
    short = evidence or ds_name or formula
    f_key = formula_key(domain, met_name, short, date_range, datasource_id)
    s.run(
        f"""
        MERGE (m:Metric {{key: $met_key}})
          ON CREATE SET m.name = $met_name, m.domain = $domain, m.datasource_id = $ds
        MERGE (f:Formula {{key: $fml_key}})
          ON CREATE SET f.domain = $domain, f.metric_key = m.key, f.dataset = $ds_name,
                        f.datasource_id = $ds, f.zone = 'metadata'
          ON MATCH  SET f.formula = $formula, f.formula_evidence = $evidence,
                        f.date_range = $date_range, f.evidence_ext = $evidence_ext,
                        f.dataset = $ds_name
        MERGE (m)-[:HAS_FORMULA]->(f)
        """,
        met_key=metric_key(domain, met_name, datasource_id),
        fml_key=f_key,
        domain=domain, met_name=met_name, ds_name=ds_name,
        formula=formula, evidence=evidence,
        date_range=date_range, evidence_ext=evidence_ext,
        ds=datasource_id,
    )
    # OF_VIEW → Dataset（若存在）
    if ds_name:
        s.run(
            f"""
            MERGE (f:Formula {{key: $fml_key}})
            MERGE (ds:Dataset {{key: $ds_key}})
              ON CREATE SET ds.name = $ds_name, ds.domain = $domain, ds.datasource_id = $ds
            MERGE (f)-[:OF_VIEW]->(ds)
            """,
            fml_key=f_key,
            ds_key=dataset_key(domain, ds_name, datasource_id),
            domain=domain, ds_name=ds_name, ds=datasource_id,
        )
    # derived_from → peer metrics
    for peer in (r.get("derived_from") or []):
        peer_str = str(peer).strip()
        if not peer_str:
            continue
        role = ""
        if ":" in peer_str:
            peer_str, role = peer_str.split(":", 1)
            peer_str, role = peer_str.strip(), role.strip()
        if not peer_str or peer_str == met_name:
            continue
        s.run(
            f"""
            MERGE (m:Metric {{key: $met_key}})
            MERGE (peer:Metric {{key: $peer_key}})
              ON CREATE SET peer.name = $peer_name, peer.domain = $domain, peer.datasource_id = $ds
            MERGE (m)-[rel:DERIVED_FROM]->(peer)
              ON CREATE SET rel.role = $role
              ON MATCH  SET rel.role = $role
            """,
            met_key=metric_key(domain, met_name, datasource_id),
            peer_key=metric_key(domain, peer_str, datasource_id),
            peer_name=peer_str, domain=domain, role=role, ds=datasource_id,
        )


def _extract_col_from_expr(expr: str) -> str:
    import re
    m = re.search(r"select\s*\(\s*([\w一-鿿]+)", expr, re.IGNORECASE)
    return m.group(1) if m else ""


_UPSERT_DISPATCH = {
    "biz_domain": _upsert_domain,
    "dataset": _upsert_dataset,
    "dataset_column": _upsert_dataset_column,
    "dimension": _upsert_dimension,
    "dataset_dimension": _upsert_dataset_dimension,
    "metric": _upsert_metric,
    "metric_formula": _upsert_metric_formula,
    # datasource 不走行级 upsert（registry 单独管理）
}


# ---------------------------------------------------------------------- #
# delete per sheet
# ---------------------------------------------------------------------- #

def delete_rows(driver: Driver, sheet: str, keys: list[str], *, datasource_id: str) -> int:
    """scoped DETACH DELETE。引用计数已在 diff 阶段守卫，这里直接删。"""
    if not keys:
        return 0
    n = 0
    with neo4j_session(driver) as s:
        for k in keys:
            parts = k.split("$$$")
            try:
                if sheet == "biz_domain":
                    domain = parts[1] if len(parts) > 1 else ""
                    if domain:
                        s.run(
                            "MATCH (d:Domain {name: $name}) "
                            "WHERE ($ds = '' OR d.datasource_id = $ds) "
                            "DETACH DELETE d",
                            name=domain, ds=datasource_id,
                        )
                        n += 1
                elif sheet == "dataset":
                    name = parts[-1] if parts else ""
                    if name:
                        s.run(
                            "MATCH (ds:Dataset {name: $name}) "
                            "WHERE ($ds = '' OR ds.datasource_id = $ds) "
                            "DETACH DELETE ds",
                            name=name, ds=datasource_id,
                        )
                        n += 1
                elif sheet == "dataset_column":
                    if len(parts) >= 4:
                        col = parts[3]
                        s.run(
                            "MATCH (dc:DatasetColumn {name: $col}) "
                            "WHERE ($ds = '' OR dc.datasource_id = $ds) "
                            "DETACH DELETE dc",
                            col=col, ds=datasource_id,
                        )
                        n += 1
                elif sheet == "dimension":
                    name = parts[-1] if parts else ""
                    if name:
                        # 先删 DimensionValue，再删 Dimension
                        s.run(
                            "MATCH (d:Dimension {name: $name}) "
                            "WHERE ($ds = '' OR d.datasource_id = $ds) "
                            "OPTIONAL MATCH (d)-[:HAS_VALUE]->(dv) "
                            "DETACH DELETE dv",
                            name=name, ds=datasource_id,
                        )
                        s.run(
                            "MATCH (d:Dimension {name: $name}) "
                            "WHERE ($ds = '' OR d.datasource_id = $ds) "
                            "DETACH DELETE d",
                            name=name, ds=datasource_id,
                        )
                        n += 1
                elif sheet == "metric":
                    name = parts[-1] if parts else ""
                    if name:
                        # 级联 Formula/Caliber
                        s.run(
                            "MATCH (m:Metric {name: $name}) "
                            "WHERE ($ds = '' OR m.datasource_id = $ds) "
                            "OPTIONAL MATCH (m)-[:HAS_FORMULA]->(f) "
                            "OPTIONAL MATCH (m)-[:HAS_CALIBER]->(c) "
                            "DETACH DELETE f, c",
                            name=name, ds=datasource_id,
                        )
                        s.run(
                            "MATCH (m:Metric {name: $name}) "
                            "WHERE ($ds = '' OR m.datasource_id = $ds) "
                            "DETACH DELETE m",
                            name=name, ds=datasource_id,
                        )
                        n += 1
                elif sheet == "metric_formula":
                    # 整行 key，用 (metric, dataset, date_range, evidence) 定位
                    if len(parts) >= 4:
                        domain, met, _ds_name = parts[1], parts[2], parts[3]
                        s.run(
                            "MATCH (f:Formula {domain: $domain, metric_key: $met_key}) "
                            "WHERE ($ds = '' OR f.datasource_id = $ds) "
                            "DETACH DELETE f",
                            domain=domain,
                            met_key=metric_key(domain, met, datasource_id),
                            ds=datasource_id,
                        )
                        n += 1
                elif sheet == "dataset_dimension":
                    # 删 binding 边（不删 Dimension/Dataset 节点）
                    if len(parts) >= 4:
                        domain, _ds_name, dim = parts[1], parts[2], parts[3]
                        s.run(
                            "MATCH (d:Dimension {name: $dim})-[r:MAPS_TO_DATASET_COLUMN]->(dc) "
                            "WHERE ($ds = '' OR d.datasource_id = $ds) "
                            "  AND dc.domain = $domain "
                            "DELETE r",
                            domain=domain, dim=dim, ds=datasource_id,
                        )
                        n += 1
            except Exception:
                log.exception("delete %s key failed: %s", sheet, k)
    return n


__all__ = ["upsert_rows", "delete_rows"]

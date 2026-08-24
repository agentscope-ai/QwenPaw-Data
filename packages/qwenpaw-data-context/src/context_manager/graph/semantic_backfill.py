"""Backfill Neo4j semantic nodes with API-aligned properties from YAML and optional PG.

Usage::

    python scripts/setup/backfill_semantic_layer.py
    python scripts/setup/backfill_semantic_layer.py --yaml data/test/metrics_dict.yaml
    python scripts/setup/backfill_semantic_layer.py --pg-enums --limit 30
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from neo4j import Driver

from ..ingest import _pg_conn_kwargs
from ..utils import get_logger, neo4j_session
from .keys import (
    dataset_key,
    dataset_short,
    dim_key,
    dim_value_key,
    domain_key,
    logical_dataset_name,
    metric_key,
    split_qualified_column,
    split_qualified_table,
    table_key,
)
from .semantic import load_metrics_dict
from .semantic_fields import (
    anomaly_rules_to_json,
    dim_value_rows,
    metric_role_from_props,
    str_list,
)

log = get_logger("graph.semantic_backfill")


def _load_yaml_domains(path: Path) -> list[dict[str, Any]]:
    data = load_metrics_dict(path)
    if "domains" in data:
        return list(data.get("domains") or [])
    # sample_domain_import.yaml shape
    if data.get("domain"):
        dom = dict(data["domain"])
        dom.setdefault("metrics", data.get("metrics") or [])
        dom.setdefault("dimensions", data.get("dimensions") or [])
        dom.setdefault("dimension_values", data.get("dimension_values") or [])
        dom.setdefault("datasets", data.get("datasets") or [])
        return [dom]
    return []


def _bool_flag(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "y")


def backfill_from_yaml(driver: Driver, yaml_path: Path, *, db_id: str = "app_db") -> dict[str, int]:
    """Patch Domain / Metric / Dimension / Dataset / DimensionValue from YAML."""
    domains = _load_yaml_domains(yaml_path)
    stats = {
        "domains": 0,
        "metrics": 0,
        "dimensions": 0,
        "dim_values": 0,
        "datasets": 0,
        "formulas": 0,
    }

    with neo4j_session(driver) as session:
        for dom in domains:
            dom_name = str(dom.get("name") or "").strip()
            if not dom_name:
                continue
            dk = str(dom.get("key") or domain_key(dom_name))
            session.run(
                """
                MERGE (d:Domain {key: $key})
                SET d.name = $name,
                    d.display_name = $display_name,
                    d.description = $description,
                    d.aliases = $aliases,
                    d.zone = 'metadata'
                """,
                key=dk,
                name=dom_name,
                display_name=str(dom.get("display_name") or dom_name),
                description=str(dom.get("description") or ""),
                aliases=str_list(dom.get("aliases")),
            )
            stats["domains"] += 1

            for dim in dom.get("dimensions") or []:
                dname = str(dim.get("name") or dim.get("dimension_name") or "").strip()
                if not dname:
                    continue
                d_key = str(dim.get("key") or dim_key(dom_name, dname))
                parent = dim.get("parent") or dim.get("parent_dimension") or ""
                if parent in (None, "null"):
                    parent = ""
                parent = str(parent).strip() if parent else ""

                ds_name = str(dim.get("dataset_name") or "").strip()
                if not ds_name:
                    maps = dim.get("maps_to_columns") or []
                    if maps and isinstance(maps[0], dict):
                        ds_name = str(maps[0].get("dataset_name") or "").strip()
                        if not ds_name:
                            qcol = str(maps[0].get("column") or "")
                            if qcol:
                                try:
                                    _, _, tbl, _ = split_qualified_column(qcol, default_db=db_id)
                                    ds_name = logical_dataset_name(tbl) if tbl else ""
                                except ValueError:
                                    pass

                session.run(
                    """
                    MERGE (d:Dimension {key: $key})
                    SET d.domain = $domain,
                        d.name = $name,
                        d.dimension_type = $dimension_type,
                        d.data_type = $data_type,
                        d.aliases = $synonyms,
                        d.hierarchy_level = $hierarchy_level,
                        d.description = $description,
                        d.dataset_name = $dataset_name,
                        d.is_display_dimension = $is_display_dimension,
                        d.is_contribution_dimension = $is_contribution_dimension,
                        d.zone = 'metadata'
                    """,
                    key=d_key,
                    domain=dom_name,
                    name=dname,
                    dimension_type=str(dim.get("dimension_type") or "OLAP维度"),
                    data_type=str(dim.get("data_type") or "text"),
                    synonyms=str_list(dim.get("synonyms")),
                    hierarchy_level=int(dim.get("hierarchy_level") or 0),
                    description=str(dim.get("description") or ""),
                    dataset_name=ds_name,
                    is_display_dimension=_bool_flag(
                        dim.get("is_display_dimension"), default=True
                    ),
                    is_contribution_dimension=_bool_flag(
                        dim.get("is_contribution_dimension"), default=True
                    ),
                )
                stats["dimensions"] += 1

                if parent:
                    session.run(
                        """
                        MATCH (child:Dimension {key: $child_key})
                        MATCH (parent:Dimension {key: $parent_key})
                        MERGE (child)-[:HAS_PARENT]->(parent)
                        """,
                        child_key=d_key,
                        parent_key=dim_key(dom_name, parent),
                    )

                rows = dim_value_rows(dom_name, dname, dim.get("values"))
                if rows:
                    session.run(
                        """
                        MATCH (d:Dimension {key: $dim_key})
                        UNWIND $rows AS r
                        MERGE (dv:DimensionValue {key: r.dv_key})
                          SET dv.dimension_key = $dim_key,
                              dv.value = r.value,
                              dv.label = r.label,
                              dv.occur_cnt = r.occur_cnt,
                              dv.zone = 'metadata'
                        MERGE (d)-[:HAS_VALUE]->(dv)
                        """,
                        dim_key=d_key,
                        rows=rows,
                    )
                    stats["dim_values"] += len(rows)

            for met in dom.get("metrics") or []:
                mname = str(met.get("name") or met.get("metric_name") or "").strip()
                if not mname:
                    continue
                m_key = str(met.get("key") or metric_key(dom_name, mname))
                props = {
                    "is_north_star": _bool_flag(met.get("is_north_star")),
                    "is_display": _bool_flag(met.get("is_display")),
                    "is_display_distribution": _bool_flag(met.get("is_display_distribution")),
                    "role": str(met.get("role") or ""),
                }
                role = metric_role_from_props(props)
                session.run(
                    """
                    MERGE (m:Metric {key: $key})
                    SET m.domain = $domain,
                        m.name = $name,
                        m.aliases = $synonyms,
                        m.tags = $tags,
                        m.description = $description,
                        m.unit = $unit,
                        m.is_north_star = $is_north_star,
                        m.is_display = $is_display,
                        m.is_display_distribution = $is_display_distribution,
                        m.role = $role,
                        m.anomaly_rules_json = $anomaly_rules_json,
                        m.zone = 'metadata'
                    """,
                    key=m_key,
                    domain=dom_name,
                    name=mname,
                    synonyms=str_list(met.get("synonyms")),
                    tags=str_list(met.get("tags")),
                    description=str(met.get("description") or met.get("notes") or ""),
                    unit=str(met.get("unit") or ""),
                    is_north_star=props["is_north_star"],
                    is_display=props["is_display"],
                    is_display_distribution=props["is_display_distribution"],
                    role=role,
                    anomaly_rules_json=anomaly_rules_to_json(met.get("anomaly_rules")),
                )
                stats["metrics"] += 1

                for f in met.get("formulas") or []:
                    dataset = str(f.get("dataset") or "").strip()
                    if not dataset:
                        continue
                    session.run(
                        """
                        MATCH (m:Metric {key: $m_key})-[:HAS_FORMULA]->(f:Formula)
                        WHERE f.dataset = $dataset OR f.dataset ENDS WITH $dataset_tail
                        SET f.formula_evidence = coalesce($evidence, f.formula_evidence),
                            f.date_range = coalesce($date_range, f.date_range, '')
                        """,
                        m_key=m_key,
                        dataset=dataset,
                        dataset_tail=dataset.split(".")[-1] if "." in dataset else dataset,
                        evidence=str(f.get("formula_evidence") or ""),
                        date_range=str(f.get("date_range") or ""),
                    )
                    stats["formulas"] += 1

            for ds in dom.get("datasets") or []:
                ds_name = str(ds.get("dataset_name") or ds.get("name") or "").strip()
                if not ds_name:
                    continue
                ds_k = dataset_key(dom_name, ds_name)
                session.run(
                    """
                    MERGE (ds:Dataset {key: $key})
                    SET ds.name = $name,
                        ds.domain = $domain,
                        ds.description = $description,
                        ds.dataset_type = $dataset_type,
                        ds.sql = $sql,
                        ds.parents = $parents,
                        ds.zone = 'metadata'
                    WITH ds
                    MATCH (dom:Domain {key: $dom_key})
                    MERGE (dom)-[:HAS_DATASET]->(ds)
                    """,
                    key=ds_k,
                    name=ds_name,
                    domain=dom_name,
                    description=str(ds.get("description") or ""),
                    dataset_type=str(ds.get("dataset_type") or "OLAP"),
                    sql=str(ds.get("sql") or ""),
                    parents=str(ds.get("parents") or ds.get("table_name") or ds_name.removeprefix("view_")),
                    dom_key=dk,
                )
                stats["datasets"] += 1

        # dimension_values top-level block (sample_domain_import)
        for dom in domains:
            dom_name = str(dom.get("name") or "").strip()
            for dv in dom.get("dimension_values") or []:
                dname = str(dv.get("dimension_name") or "").strip()
                val = str(dv.get("value") or dv.get("dimension_value") or "").strip()
                if not dom_name or not dname or not val:
                    continue
                d_key = dim_key(dom_name, dname)
                session.run(
                    """
                    MATCH (d:Dimension {key: $dim_key})
                    MERGE (dv:DimensionValue {key: $dv_key})
                      SET dv.value = $value,
                          dv.label = $value,
                          dv.occur_cnt = $occur_cnt,
                          dv.dimension_key = $dim_key,
                          dv.zone = 'metadata'
                    MERGE (d)-[:HAS_VALUE]->(dv)
                    """,
                    dim_key=d_key,
                    dv_key=dim_value_key(dom_name, dname, val),
                    value=val,
                    occur_cnt=int(dv.get("occur_cnt") or dv.get("dimension_occur_cnt") or 0),
                )
                stats["dim_values"] += 1

    # Propagate parents onto Dataset from CONTAINS_TABLE
    with neo4j_session(driver) as session:
        session.run(
            """
            MATCH (ds:Dataset)-[:CONTAINS_TABLE]->(t:Table)
            WHERE coalesce(ds.parents, '') = ''
            SET ds.parents = t.name
            """
        )

    log.info("semantic backfill from %s: %s", yaml_path, stats)
    return stats


def backfill_datasets_from_formulas(
    driver: Driver,
    *,
    domains: Optional[list[str]] = None,
    db_id: str = "app_db",
) -> dict[str, int]:
    """Ensure ``Dataset`` nodes + ``HAS_DATASET`` + ``CONTAINS_TABLE`` from formulas.

    Derives API-facing ``dataset_<table>`` names (legacy semantic layer style) from
    each ``Formula.dataset`` qualified table string and links them to ``Domain``.
    """
    stats = {
        "formula_rows": 0,
        "datasets_merged": 0,
        "has_dataset_rels": 0,
        "contains_table_rels": 0,
    }
    domain_clause = ""
    params: dict[str, Any] = {}
    if domains:
        domain_clause = "AND m.domain IN $domains"
        params["domains"] = domains

    with neo4j_session(driver) as session:
        rows = session.run(
            f"""
            MATCH (m:Metric)-[:HAS_FORMULA]->(f:Formula)
            WHERE coalesce(m.domain, '') <> '' {domain_clause}
            RETURN DISTINCT m.domain AS domain, f.dataset AS dataset, f.key AS f_key
            ORDER BY domain, dataset
            """,
            **params,
        ).data()
        stats["formula_rows"] = len(rows)

        for row in rows:
            domain = str(row.get("domain") or "").strip()
            dataset_raw = str(row.get("dataset") or "").strip()
            f_key = str(row.get("f_key") or "")
            if not domain or not dataset_raw:
                continue

            tbl_k = ""
            table_name = ""
            qualified = dataset_raw
            if "." in dataset_raw:
                try:
                    tdb, tsch, tname = split_qualified_table(
                        dataset_raw, default_db=db_id
                    )
                    tbl_k = table_key(tdb, tsch, tname)
                    table_name = tname
                except ValueError:
                    table_name = dataset_raw.rsplit(".", 1)[-1]
                    qualified = f"{db_id}.public.{table_name}"
            else:
                rec = session.run(
                    """
                    MATCH (f:Formula {key: $f_key})-[:OF_VIEW]->(:Dataset)-[:CONTAINS_TABLE]->(t:Table)
                    RETURN t.key AS tkey, t.name AS tname, t.db AS db, t.schema AS schema
                    LIMIT 1
                    """,
                    f_key=f_key,
                ).single()
                if rec:
                    tbl_k = str(rec.get("tkey") or "")
                    table_name = str(rec.get("tname") or dataset_raw)
                    qualified = (
                        f"{rec.get('db') or db_id}.{rec.get('schema') or 'public'}.{table_name}"
                    )
                else:
                    table_name = dataset_raw.removeprefix("view_")
                    qualified = dataset_raw

            if "." not in dataset_raw:
                logical = dataset_raw
            else:
                logical = logical_dataset_name(qualified or table_name, default_db=db_id)
            if not logical:
                continue
            parents = table_name or logical.removeprefix("view_")
            short = dataset_short(qualified, domain=domain) if "." in qualified else dataset_raw
            dom_k = domain_key(domain)
            ds_k = dataset_key(domain, logical)

            session.run(
                """
                MERGE (ds:Dataset {key: $ds_key})
                SET ds.name = $logical,
                    ds.domain = $domain,
                    ds.dataset_type = coalesce(ds.dataset_type, 'OLAP'),
                    ds.parents = $parents,
                    ds.qualified_table = $qualified,
                    ds.dataset_short = coalesce(ds.dataset_short, $short),
                    ds.zone = 'metadata'
                WITH ds
                MATCH (dom:Domain)
                WHERE dom.name = $domain OR dom.key = $dom_key
                MERGE (dom)-[:HAS_DATASET]->(ds)
                WITH ds
                OPTIONAL MATCH (t:Table {key: $tbl_key})
                FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [t] END |
                    MERGE (ds)-[:CONTAINS_TABLE]->(t)
                )
                """,
                ds_key=ds_k,
                logical=logical,
                domain=domain,
                dom_key=dom_k,
                parents=parents,
                qualified=qualified,
                short=short,
                tbl_key=tbl_k,
            )
            stats["datasets_merged"] += 1
            stats["has_dataset_rels"] += 1
            if tbl_k:
                stats["contains_table_rels"] += 1

        # Drop redundant HAS_DATASET on legacy short-name datasets when logical twin exists
        session.run(
            """
            MATCH (dom:Domain)-[r:HAS_DATASET]->(ds_short:Dataset)
            WHERE NOT ds_short.name STARTS WITH 'view_'
              AND coalesce(ds_short.sql, '') <> '*'
            MATCH (ds_short)-[:CONTAINS_TABLE]->(t:Table)<-[:CONTAINS_TABLE]-(ds_long:Dataset)
            WHERE ds_long.name STARTS WITH 'view_'
            DELETE r
            """
        )

    log.info("HAS_DATASET backfill: %s", stats)
    return stats


def enrich_dimension_values_from_pg(
    driver: Driver,
    *,
    db_id: str = "app_db",
    schema: str = "public",
    max_values: int = 100,
    min_count: int = 1,
) -> int:
    """Sample enum-like columns and write DimensionValue + occur_cnt."""
    try:
        import psycopg
    except ImportError:
        log.warning("psycopg not installed; skip PG enum enrichment")
        return 0

    updated = 0
    with neo4j_session(driver) as s:
        dims = s.run(
            """
            MATCH (d:Dimension)-[r:MAPS_TO_COLUMN]->(c:Column)
            RETURN d.key AS dkey, d.domain AS domain, d.name AS dname,
                   c.key AS ckey, c.name AS cname, r.filter AS col_filter
            LIMIT 500
            """
        ).data()

    for row in dims:
        ckey = str(row.get("ckey") or "")
        if not ckey:
            continue
        if not ckey.startswith("col:"):
            continue
        rest = ckey[4:]
        try:
            cdb, csch, ctbl, cname = split_qualified_column(rest, default_db=db_id)
        except ValueError:
            parts = rest.split(".")
            if len(parts) < 4:
                continue
            cdb, csch, ctbl, cname = parts[0], parts[1], parts[2], ".".join(parts[3:])
        if db_id and cdb != db_id:
            continue
        tbl = ctbl
        sch = csch or schema
        col_filter = str(row.get("col_filter") or "").strip()
        where = f'"{cname}" IS NOT NULL'
        if col_filter:
            where = f"{col_filter} AND {where}"
        sql = (
            f'SELECT "{cname}"::text AS v, COUNT(*)::bigint AS cnt '
            f'FROM "{sch}"."{tbl}" WHERE {where} '
            f'GROUP BY 1 ORDER BY cnt DESC LIMIT {int(max_values)}'
        )
        try:
            import psycopg

            with psycopg.connect(**_pg_conn_kwargs(schema=schema), connect_timeout=8) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    pg_rows = cur.fetchall()
        except Exception as exc:
            log.debug("PG sample skip %s.%s: %s", tbl, cname, exc)
            continue

        if not pg_rows:
            continue

        domain = str(row.get("domain") or "")
        dname = str(row.get("dname") or "")
        dkey = str(row.get("dkey") or "")
        dv_rows = []
        for val, cnt in pg_rows:
            if val is None or not str(val).strip():
                continue
            if int(cnt) < min_count:
                continue
            v = str(val).strip()
            dv_rows.append(
                {
                    "dv_key": dim_value_key(domain, dname, v),
                    "value": v,
                    "label": v,
                    "occur_cnt": int(cnt),
                }
            )
        if not dv_rows:
            continue

        with neo4j_session(driver) as s:
            s.run(
                """
                MATCH (d:Dimension {key: $dim_key})
                UNWIND $rows AS r
                MERGE (dv:DimensionValue {key: r.dv_key})
                  SET dv.value = r.value,
                      dv.label = r.label,
                      dv.occur_cnt = r.occur_cnt,
                      dv.dimension_key = $dim_key,
                      dv.zone = 'metadata'
                MERGE (d)-[:HAS_VALUE]->(dv)
                """,
                dim_key=dkey,
                rows=dv_rows,
            )
            # mirror top values on Column.sample_values for sample_values API fallback
            s.run(
                """
                MATCH (c:Column {key: $ckey})
                SET c.sample_values = $vals
                """,
                ckey=ckey,
                vals=[r["value"] for r in dv_rows[:50]],
            )
        updated += len(dv_rows)

    log.info("PG enum enrichment: %d dimension values written", updated)
    return updated


def run_backfill(
    driver: Driver,
    yaml_path: Path,
    *,
    pg_enrich: bool = False,
    datasets_from_formulas: bool = True,
    dataset_domains: Optional[list[str]] = None,
    db_id: str = "app_db",
    schema: str = "public",
) -> dict[str, Any]:
    out: dict[str, Any] = {"yaml": backfill_from_yaml(driver, yaml_path, db_id=db_id)}
    if datasets_from_formulas:
        out["datasets"] = backfill_datasets_from_formulas(
            driver, domains=dataset_domains, db_id=db_id
        )
    if pg_enrich:
        out["pg_values"] = enrich_dimension_values_from_pg(
            driver, db_id=db_id, schema=schema
        )
    return out

#!/usr/bin/env python3
"""Neo4j per-workspace 图 → Holo AGE per-workspace 图迁移脚本。

1:1 迁移：每个 Neo4j workspace 图 → cm_graph_<workspace_id>。
幂等可重跑（整图 drop 重建）。embedding 直接搬，不重算。

用法：
    python scripts/migrate/neo4j_to_holo_workspaces.py \
        --workspaces ws1,ws2 \
        --neo4j-uri bolt://localhost:7687 \
        --neo4j-user neo4j --neo4j-password graphrag123 \
        [--skip-vectors] [--dry-run]

    # 或从控制面枚举全部 workspace（sqlite:/// 或 postgresql:// 均可）：
    python scripts/migrate/neo4j_to_holo_workspaces.py \
        --control-plane-dsn sqlite:///path/to/control.sqlite
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from qwenpaw_data_auth.database import get_db, query_all

# ---------------------------------------------------------------------------
# agtype 序列化
# ---------------------------------------------------------------------------


def to_agtype_value(v: Any) -> Optional[str]:
    """Python 值 → agtype 字面量字符串。None → None（调用方跳过该 key）。"""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool, list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return json.dumps(str(v), ensure_ascii=False)


def build_agtype_map(props: dict[str, Any]) -> tuple[str, list[str]]:
    """构建 agtype_build_map 的参数片段。跳过 None 值。

    Returns:
        (parts, values): parts 形如 "'key', %s::ag_catalog.agtype, ..."
    """
    parts: list[str] = []
    values: list[str] = []
    for k, v in props.items():
        av = to_agtype_value(v)
        if av is None:
            continue
        parts.append(f"'{k}', %s::ag_catalog.agtype")
        values.append(av)
    return (", ".join(parts), values)


# ---------------------------------------------------------------------------
# 标识符安全
# ---------------------------------------------------------------------------

_UNSAFE_RE = re.compile(r'["\';\\]')


def _check_identifier(name: str) -> None:
    if not name or _UNSAFE_RE.search(name):
        raise ValueError(f"unsafe graph name: {name!r}")


# ---------------------------------------------------------------------------
# 图名
# ---------------------------------------------------------------------------


def workspace_graph_name(workspace_id: str) -> str:
    """workspace_id → Holo AGE 图名（与 provisioning.graph_name_for_workspace 一致）。"""
    ws = (workspace_id or "").strip()
    if not ws or ws == "__offline__":
        return "cm_graph_offline"
    sanitized = re.sub(r"[^a-z0-9_]+", "_", ws.lower())
    return f"cm_graph_{sanitized}"


# ---------------------------------------------------------------------------
# SQL 生成
# ---------------------------------------------------------------------------


def build_node_insert_sql(
    graph: str, label: str, rows: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """批量节点 INSERT。rows 为 props 字典列表。"""
    _check_identifier(graph)
    _check_identifier(label)
    groups: list[str] = []
    all_values: list[str] = []
    for row in rows:
        parts, values = build_agtype_map(row)
        groups.append(f"(ag_catalog.agtype_build_map({parts}))")
        all_values.extend(values)
    sql = f'INSERT INTO "{graph}"."{label}"(properties) VALUES ' + ", ".join(groups)
    return sql, all_values


def build_edge_insert_sql(
    graph: str,
    rel_type: str,
    start_label: str,
    end_label: str,
    edges: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """批量边 INSERT（INSERT...SELECT 按端点 key 关联）。同组多边 UNION ALL。"""
    _check_identifier(graph)
    _check_identifier(rel_type)
    _check_identifier(start_label)
    _check_identifier(end_label)
    selects: list[str] = []
    all_values: list[str] = []
    for edge in edges:
        props = edge.get("props") or {}
        parts, values = build_agtype_map(props)
        build_map = (
            f"ag_catalog.agtype_build_map({parts})"
            if parts
            else "ag_catalog.agtype_build_map()"
        )
        selects.append(
            f"SELECT s.id, e.id, {build_map} "
            f'FROM "{graph}"."{start_label}" s, "{graph}"."{end_label}" e '
            f"WHERE ag_catalog.agtype_object_field(s.properties, 'key')::text = %s "
            f"AND ag_catalog.agtype_object_field(e.properties, 'key')::text = %s"
        )
        all_values.extend(values)
        all_values.append(edge["start_key"])
        all_values.append(edge["end_key"])
    sql = (
        f'INSERT INTO "{graph}"."{rel_type}"(start_id, end_id, properties) '
        + " UNION ALL ".join(selects)
    )
    return sql, all_values


def build_count_sql(graph: str, label: str) -> str:
    return f'SELECT count(*) FROM "{graph}"."{label}"'


def build_drop_graph_sql(graph: str) -> str:
    return f"CALL ag_catalog.hg_age_drop_graph('{graph}', true)"


# ---------------------------------------------------------------------------
# 向量
# ---------------------------------------------------------------------------

_VEC_TABLE_MAP: dict[str, tuple[str, list[str]]] = {
    "met_vec": (
        "vec_metric",
        ["valid_to", "datasource_id", "workspace_id", "owner_id"],
    ),
    "dim_vec": (
        "vec_dimension",
        ["valid_to", "datasource_id", "workspace_id", "owner_id"],
    ),
    "col_vec": (
        "vec_column",
        ["db", "tbl", "datasource_id", "workspace_id", "owner_id"],
    ),
    "dscol_vec": (
        "vec_dataset_column",
        ["db", "datasource_id", "workspace_id", "owner_id"],
    ),
    "ds_vec": ("vec_dataset", ["datasource_id", "workspace_id", "owner_id"]),
    "strategy_vec": (
        "vec_strategy",
        ["graph_db_id", "valid_to", "workspace_id", "owner_id"],
    ),
    "ev_vec": ("vec_event", ["valid_to", "workspace_id", "owner_id"]),
    "ent_vec": ("vec_entity", ["workspace_id", "owner_id"]),
    "claim_vec": ("vec_claim", ["predicate", "valid_to", "workspace_id", "owner_id"]),
}

_LABEL_VEC_CONFIG: dict[str, tuple[str, str, list[str]]] = {
    "Metric": ("met_vec", "embedding", ["valid_to", "datasource_id"]),
    "Dimension": ("dim_vec", "embedding", ["valid_to", "datasource_id"]),
    "Column": ("col_vec", "embedding", ["db", "tbl", "datasource_id"]),
    "DatasetColumn": ("dscol_vec", "embedding", ["db", "datasource_id"]),
    "Dataset": ("ds_vec", "embedding", ["datasource_id"]),
    "Event": ("ev_vec", "embedding", ["valid_to"]),
    "Entity": ("ent_vec", "embedding", []),
    "Strategy": ("strategy_vec", "signature_emb", ["graph_db_id", "valid_to"]),
    "Claim": ("claim_vec", "embedding", ["predicate", "valid_to"]),
}


def _vec_literal(vec: list[float]) -> str:
    """[0.1, 1.0] → '{0.1,1}'（PostgreSQL float4[] 字面量）。"""
    return "{" + ",".join(format(v, "g") for v in vec) + "}"


def build_vec_upsert_sql(
    graph: str, index_name: str, rows: list[dict[str, Any]]
) -> tuple[str, list[Any]]:
    """vec 表 UPSERT（ON CONFLICT 幂等）。"""
    _check_identifier(graph)
    table, extra_cols = _VEC_TABLE_MAP[index_name]
    cols = ["key", "embedding", "embedding_hash"] + extra_cols
    col_list = ", ".join(cols)
    placeholders: list[str] = []
    for c in cols:
        if c == "embedding":
            placeholders.append("%s::float4[]")
        else:
            placeholders.append("%s")
    ph = ", ".join(placeholders)

    update_cols = [c for c in cols if c not in ("key", "workspace_id", "owner_id")]
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    values_parts: list[str] = []
    all_values: list[Any] = []
    for row in rows:
        values_parts.append(f"({ph})")
        all_values.append(row.get("key"))
        all_values.append(_vec_literal(row.get("embedding") or []))
        all_values.append(row.get("embedding_hash") or "")
        for c in extra_cols:
            all_values.append(row.get(c))

    sql = (
        f'INSERT INTO "{graph}"."{table}" ({col_list}) VALUES '
        + ", ".join(values_parts)
        + f" ON CONFLICT (key, workspace_id, owner_id) DO UPDATE SET {update_set}"
    )
    return sql, all_values


def extract_vector_rows(
    nodes_by_label: dict[str, list[dict[str, Any]]], workspace_id: str
) -> dict[str, list[dict[str, Any]]]:
    """从节点 props 中提取向量行，按 index_name 分组。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for label, nodes in nodes_by_label.items():
        if label not in _LABEL_VEC_CONFIG:
            continue
        index_name, emb_field, extra_payload_cols = _LABEL_VEC_CONFIG[label]
        rows: list[dict[str, Any]] = []
        for node in nodes:
            emb = node.get(emb_field)
            if not emb:
                continue
            row: dict[str, Any] = {
                "key": node.get("key"),
                "embedding": emb,
                "embedding_hash": node.get("embedding_hash") or "",
                "workspace_id": node.get("workspace_id") or workspace_id,
                "owner_id": node.get("owner_id") or "__global__",
            }
            for col in extra_payload_cols:
                row[col] = node.get(col)
            rows.append(row)
        if rows:
            out[index_name] = rows
    return out


# ---------------------------------------------------------------------------
# 计数校验
# ---------------------------------------------------------------------------


def verify_counts(
    neo4j_nodes: dict[str, int],
    neo4j_edges: dict[str, int],
    holo_counts: dict[tuple[str, str], int],
) -> None:
    """Neo4j vs Holo 计数校验，不一致抛 RuntimeError。"""
    errors: list[str] = []
    for label, count in neo4j_nodes.items():
        hc = holo_counts.get(("node", label), -1)
        if hc != count:
            errors.append(f"node {label}: neo4j={count} holo={hc}")
    for rel_type, count in neo4j_edges.items():
        hc = holo_counts.get(("edge", rel_type), -1)
        if hc != count:
            errors.append(f"edge {rel_type}: neo4j={count} holo={hc}")
    if errors:
        raise RuntimeError("count verification failed: " + "; ".join(errors))


# ---------------------------------------------------------------------------
# 控制面枚举
# ---------------------------------------------------------------------------


def list_workspaces_from_control_plane(dsn: str) -> list[str]:
    """从控制面枚举活跃 workspace（需 tenant 也 active）。

    SQLite 与 Hologres/PostgreSQL 控制面均可，统一走 ``qwenpaw_data_auth.database``。
    """
    with get_db(dsn) as conn:
        rows = query_all(
            conn,
            "SELECT w.id FROM workspaces w "
            "JOIN tenants t ON w.tenant_id = t.id "
            "WHERE w.status = 'active' AND t.status = 'active'",
        )
    return sorted(str(r["id"]) for r in rows)


# ---------------------------------------------------------------------------
# Neo4j 导出
# ---------------------------------------------------------------------------


def _count_neo4j_nodes(session: Any) -> dict[str, int]:
    rows = session.run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c").data()
    return {r["label"]: int(r["c"]) for r in rows if r["label"]}


def _count_neo4j_edges(session: Any) -> dict[str, int]:
    rows = session.run("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS c").data()
    return {r["type"]: int(r["c"]) for r in rows if r["type"]}


def _export_nodes(session: Any, label: str, batch_size: int) -> list[dict[str, Any]]:
    """按 elementId 分页导出指定 label 的节点。"""
    out: list[dict[str, Any]] = []
    last = ""
    while True:
        rows = session.run(
            f"MATCH (n:`{label}`) WHERE elementId(n) > $last "
            "RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props "
            "ORDER BY elementId(n) LIMIT $lim",
            last=last,
            lim=batch_size,
        ).data()
        if not rows:
            break
        out.extend(rows)
        last = rows[-1]["eid"]
        if len(rows) < batch_size:
            break
    return out


def _export_edges(session: Any, batch_size: int) -> list[dict[str, Any]]:
    """按 elementId 分页导出全部边。"""
    out: list[dict[str, Any]] = []
    last = ""
    while True:
        rows = session.run(
            "MATCH (s)-[r]->(t) WHERE elementId(r) > $last "
            "RETURN elementId(r) AS eid, type(r) AS type, "
            "labels(s)[0] AS s_label, s.key AS s_key, "
            "labels(t)[0] AS t_label, t.key AS t_key, "
            "properties(r) AS props "
            "ORDER BY elementId(r) LIMIT $lim",
            last=last,
            lim=batch_size,
        ).data()
        if not rows:
            break
        out.extend(rows)
        last = rows[-1]["eid"]
        if len(rows) < batch_size:
            break
    return out


# ---------------------------------------------------------------------------
# Holo 执行
# ---------------------------------------------------------------------------


def _execute(conn: Any, sql: str, params: Any = None) -> None:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
    finally:
        cur.close()


def _holo_count(conn: Any, graph: str, label: str) -> int:
    cur = conn.cursor()
    try:
        cur.execute(build_count_sql(graph, label))
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        cur.close()


def _schema_exists(conn: Any, graph: str) -> bool:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (graph,),
        )
        return cur.fetchone() is not None
    finally:
        cur.close()


def _drop_graph(
    conn: Any, graph: str, *, settle_timeout: float = 120.0, poll_interval: float = 2.0
) -> None:
    """删图：优先 hg_age_drop_graph，失败退化为逐表 DELETE。

    删完校验 schema 是否真消失；若 ``hg_age_drop_graph`` 部分失败留下
    残留，用 DROP SCHEMA CASCADE 兜底，保证后续 ensure 从干净状态重建。

    ⚠️ 真实实例坑（2026-08-14）：Hologres 的 information_schema 可见性
    异步收敛——drop 返回后短时间内 schema 仍"可见"，但 ag_catalog 条目已删。
    此时立刻 ensure 会命中"图已存在"的对账分支，第一条 create_vlabel 就报
    "graph does not exist"。因此真正删除成功后要轮询等 schema 不可见。
    purge 退化路径（drop_graph 不可用时逐表 DELETE）语义上保留 schema 与
    label 定义，不做等待。
    """
    import time

    really_dropped = True
    try:
        _execute(conn, build_drop_graph_sql(graph))
    except Exception:
        really_dropped = False
        # 退化路径：枚举 schema 下的表逐个清空
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{graph}'"
            )
            tables = [r[0] for r in cur.fetchall()]
        finally:
            cur.close()
        for table in tables:
            _execute(conn, f'DELETE FROM "{graph}"."{table}"')
    # 校验 + 兜底：schema 仍在则强制删（hg_age_drop_graph 可能只清了表）
    if _schema_exists(conn, graph):
        try:
            _execute(conn, f'DROP SCHEMA "{graph}" CASCADE')
            really_dropped = True
        except Exception:
            pass
    if not really_dropped:
        # purge 模式：schema 按设计保留（label 定义复用），无需等不可见
        return
    # 等 information_schema 可见性收敛到"schema 不存在"
    deadline = time.time() + settle_timeout
    while _schema_exists(conn, graph):
        if time.time() >= deadline:
            raise RuntimeError(
                f"graph {graph} still visible {settle_timeout:.0f}s after drop — "
                "information_schema 可见性未收敛，拒绝继续（重建会撞对账分支）"
            )
        time.sleep(poll_interval)


def _reconcile_missing_labels(
    conn: Any,
    graph: str,
    node_labels: set[str],
    edge_types: set[str],
) -> None:
    """对账补建：Neo4j 数据中存在但图内缺失的 vlabel/elabel。

    生产 Neo4j 图可能含 v3 遗留 label/type（不在 v4 ``holo_ddl`` 清单），
    1:1 迁移必须补建，否则 INSERT 报表不存在、计数校验必挂。
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = '{graph}'"
        )
        existing = {r[0] for r in cur.fetchall()}
    finally:
        cur.close()
    for lab in sorted(node_labels):
        _check_identifier(lab)
        if lab not in existing:
            _execute(conn, f"CALL ag_catalog.hg_age_create_vlabel('{graph}', '{lab}')")
            print(f"  [补建 vlabel] {graph}.{lab}")
    for rel in sorted(edge_types):
        _check_identifier(rel)
        if rel not in existing:
            _execute(conn, f"CALL ag_catalog.hg_age_create_elabel('{graph}', '{rel}')")
            print(f"  [补建 elabel] {graph}.{rel}")


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ---------------------------------------------------------------------------
# 核心：单 workspace 迁移
# ---------------------------------------------------------------------------


def migrate_workspace(
    workspace_id: str,
    *,
    neo4j_session: Any,
    holo_conn: Any,
    ensure_workspace_graph: Callable[[str], str],
    batch_size: int = 1000,
    skip_vectors: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """迁移单个 workspace 的 Neo4j 图到 Holo AGE 图。

    Args:
        workspace_id: 源 workspace 标识
        neo4j_session: Neo4j session（已连接到对应 workspace 图）
        holo_conn: psycopg connection（autocommit）
        ensure_workspace_graph: provisioning 建图函数
        batch_size: 分批大小
        skip_vectors: 跳过向量迁移
        dry_run: 只计数不写入

    Returns:
        stats dict
    """
    graph = ensure_workspace_graph(workspace_id)
    _check_identifier(graph)

    # 计数
    node_counts = _count_neo4j_nodes(neo4j_session)
    edge_counts = _count_neo4j_edges(neo4j_session)

    if dry_run:
        return {
            "dry_run": True,
            "graph": graph,
            "nodes": node_counts,
            "edges": edge_counts,
            "node_total": sum(node_counts.values()),
            "edge_total": sum(edge_counts.values()),
        }

    # 删旧图 + 重建
    _drop_graph(holo_conn, graph)
    # drop 后必须失效 provisioning 的进程内缓存，否则第二次 ensure 命中缓存
    # 直接返回图名而不重建（图已被整图删除）。
    try:
        from context_manager.graph.backends import provisioning as _prov

        with _prov._lock:  # noqa: SLF001
            _prov._ensured_graphs.discard(graph)  # noqa: SLF001
    except Exception:
        pass
    graph = ensure_workspace_graph(workspace_id)

    # 动态补建 Neo4j 数据中存在但 v4 清单缺失的 label（v3 遗留等）
    _reconcile_missing_labels(
        holo_conn,
        graph,
        node_labels=set(node_counts.keys()),
        edge_types=set(edge_counts.keys()),
    )

    # 导出并写入节点
    all_nodes_by_label: dict[str, list[dict[str, Any]]] = {}
    node_total = 0
    for label in node_counts:
        nodes = _export_nodes(neo4j_session, label, batch_size)
        all_nodes_by_label[label] = nodes
        for batch in _chunks(nodes, batch_size):
            props_list = [n["props"] for n in batch]
            sql, values = build_node_insert_sql(graph, label, props_list)
            _execute(holo_conn, sql, values)
        node_total += len(nodes)

    # 导出并写入边
    edges = _export_edges(neo4j_session, batch_size)
    edge_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for e in edges:
        if not e.get("s_key") or not e.get("t_key"):
            continue
        key = (e["type"], e["s_label"], e["t_label"])
        edge_groups.setdefault(key, []).append(e)

    for (rel_type, s_label, t_label), group in edge_groups.items():
        for batch in _chunks(group, batch_size):
            shaped = [
                {
                    "start_key": e["s_key"],
                    "end_key": e["t_key"],
                    "props": e.get("props") or {},
                }
                for e in batch
            ]
            sql, values = build_edge_insert_sql(
                graph, rel_type, s_label, t_label, shaped
            )
            _execute(holo_conn, sql, values)
    edge_total = len(edges)

    # 向量（extract_vector_rows 期望纯 props 字典，从导出行里取 n["props"]）
    vectors_stats: dict[str, int] = {}
    if not skip_vectors:
        props_by_label = {
            label: [n["props"] for n in nodes]
            for label, nodes in all_nodes_by_label.items()
        }
        vec_rows = extract_vector_rows(props_by_label, workspace_id)
        for index_name, rows in vec_rows.items():
            for batch in _chunks(rows, batch_size):
                sql, values = build_vec_upsert_sql(graph, index_name, batch)
                _execute(holo_conn, sql, values)
            vectors_stats[index_name] = len(rows)

    # 计数校验
    holo_counts: dict[tuple[str, str], int] = {}
    for label in node_counts:
        holo_counts[("node", label)] = _holo_count(holo_conn, graph, label)
    for rel_type in edge_counts:
        holo_counts[("edge", rel_type)] = _holo_count(holo_conn, graph, rel_type)
    verify_counts(node_counts, edge_counts, holo_counts)

    return {
        "graph": graph,
        "nodes": node_counts,
        "edges_by_type": edge_counts,
        "node_total": node_total,
        "edge_total": edge_total,
        "vectors": vectors_stats,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Neo4j per-workspace → Holo AGE per-workspace 迁移"
    )
    parser.add_argument("--workspaces", help="逗号分隔的 workspace 列表")
    parser.add_argument(
        "--control-plane-dsn",
        default=os.getenv("QWENPAW_DATA_DB_URL", ""),
        help="控制面 DSN（sqlite:/// 或 postgresql://... for Hologres）",
    )
    parser.add_argument(
        "--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687")
    )
    parser.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument(
        "--neo4j-password", default=os.getenv("NEO4J_PASSWORD", "graphrag123")
    )
    parser.add_argument("--skip-vectors", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    # 确定 workspace 列表
    if args.workspaces:
        workspaces = [w.strip() for w in args.workspaces.split(",") if w.strip()]
    elif args.control_plane_dsn:
        workspaces = list_workspaces_from_control_plane(args.control_plane_dsn)
    else:
        parser.error("需要 --workspaces 或 --control-plane-dsn")
        return 1

    if not workspaces:
        print("无 workspace 可迁移")
        return 0

    print(
        f"迁移 {len(workspaces)} 个 workspace: {workspaces[:5]}{'...' if len(workspaces) > 5 else ''}"
    )

    # 连接
    from neo4j import GraphDatabase
    import psycopg

    sys.path.insert(
        0,
        str(
            Path(__file__).resolve().parents[2] / "packages" / "qwenpaw-data-context" / "src"
        ),
    )
    from context_manager.graph.backends.provisioning import ensure_workspace_graph

    holo_password = os.getenv("HOLO_AGE_PASSWORD", "")
    if not holo_password:
        print("错误：HOLO_AGE_PASSWORD 环境变量未设置")
        return 1

    holo_conn = psycopg.connect(
        host=os.getenv(
            "HOLO_AGE_HOST",
            "public-hologres.example.com",
        ),
        port=int(os.getenv("HOLO_AGE_PORT", "8099")),
        user=os.getenv("HOLO_AGE_USER", "BASIC$qwenpaw_data_agent_user"),
        password=holo_password,
        dbname=os.getenv("HOLO_AGE_DATABASE", "example_busi"),
        autocommit=True,
    )

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )

    failed: list[str] = []
    for ws in workspaces:
        print(f"\n{'=' * 60}\n迁移 workspace: {ws}\n{'=' * 60}")
        try:
            with driver.session(database=ws) as session:
                stats = migrate_workspace(
                    ws,
                    neo4j_session=session,
                    holo_conn=holo_conn,
                    ensure_workspace_graph=ensure_workspace_graph,
                    batch_size=args.batch_size,
                    skip_vectors=args.skip_vectors,
                    dry_run=args.dry_run,
                )
            if args.dry_run:
                print(f"  [dry-run] nodes={stats['nodes']} edges={stats['edges']}")
            else:
                print(
                    f"  ✓ {stats['graph']}: {stats['node_total']} nodes, "
                    f"{stats['edge_total']} edges, vectors={stats['vectors']}"
                )
        except Exception as exc:
            print(f"  ✗ 失败: {exc}")
            failed.append(ws)

    holo_conn.close()
    driver.close()

    if failed:
        print(f"\n{len(failed)} 个 workspace 迁移失败: {failed}")
        return 1
    print(f"\n全部 {len(workspaces)} 个 workspace 迁移完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())

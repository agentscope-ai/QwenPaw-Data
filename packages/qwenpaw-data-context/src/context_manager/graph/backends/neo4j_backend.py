"""Neo4j 后端实现。

封装 Neo4j Python driver，提供 GraphBackend/GraphSession 接口。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional, Sequence

from .base import GraphBackend, GraphSession

if TYPE_CHECKING:
    from neo4j import Driver, Session

log = logging.getLogger(__name__)


class Neo4jSession(GraphSession):
    """Neo4j 会话实现。

    封装 neo4j.Session，提供统一的 GraphSession 接口。
    """

    def __init__(self, session: "Session"):
        self._session = session

    def run(self, cypher: str, **params: Any) -> Any:
        """执行 Cypher 查询。"""
        return self._session.run(cypher, **params)

    def execute_write(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        """执行写事务。"""
        return self._session.execute_write(fn, **kwargs)

    # ========================================================================
    # 节点操作
    # ========================================================================

    def upsert_node(
        self,
        label: str,
        key: str,
        props: dict[str, Any],
        update_props: Optional[dict[str, Any]] = None,
        match: Optional[dict[str, Any]] = None,
    ) -> None:
        """MERGE 节点并设置属性（ON CREATE / ON MATCH SET）。

        ``match`` 携带复合匹配键，
        会同时进入 MERGE 模式与属性集。
        """
        match = match or {}
        match_clause = "".join(f", {k}: ${k}" for k in match.keys())
        create_parts = [f"n.{k} = ${k}" for k in props.keys() if k not in match]
        create_parts += [f"n.{k} = ${k}" for k in match.keys()]
        create_clause = ", ".join(create_parts)

        if update_props is not None:
            update_parts = [f"n.{k} = $upd_{k}" for k in update_props.keys()]
            update_clause = ", ".join(update_parts)
            params = {
                "key": key,
                **match,
                **{k: v for k, v in props.items() if k not in match},
                **{f"upd_{k}": v for k, v in update_props.items()},
            }
        else:
            update_clause = create_clause
            params = {"key": key, **match, **{k: v for k, v in props.items() if k not in match}}

        cypher = f"""
        MERGE (n:{label} {{key: $key{match_clause}}})
        ON CREATE SET {create_clause}
        ON MATCH SET {update_clause}
        """
        self._session.run(cypher, **params)

    def batch_upsert_nodes(
        self,
        label: str,
        nodes: list[dict[str, Any]],
        key_field: str = "key",
        match_fields: Optional[Sequence[str]] = None,
    ) -> None:
        """UNWIND + MERGE 批量插入/更新节点。"""
        if not nodes:
            return

        match_fields = list(match_fields or [])
        # 从第一个节点推断属性列表
        sample = nodes[0]
        props_keys = [k for k in sample.keys() if k != key_field and k not in match_fields]

        match_clause = "".join(f", {f}: row.{f}" for f in match_fields)
        create_parts = [f"n.{k} = row.{k}" for k in props_keys + match_fields]
        create_clause = ", ".join(create_parts)

        cypher = f"""
        UNWIND $nodes AS row
        MERGE (n:{label} {{key: row.{key_field}{match_clause}}})
        ON CREATE SET {create_clause}
        ON MATCH SET {create_clause}
        """
        self._session.run(cypher, nodes=nodes)

    def update_node(
        self,
        label: str,
        key: str,
        updates: dict[str, Any],
        match: Optional[dict[str, Any]] = None,
    ) -> None:
        """更新现有节点属性（MATCH + SET）。"""
        match = match or {}
        match_clause = "".join(f", {k}: ${k}" for k in match.keys())
        set_clauses = [f"n.{k} = ${k}" for k in updates.keys()]
        set_clause = ", ".join(set_clauses)

        cypher = f"""
        MATCH (n:{label} {{key: $key{match_clause}}})
        SET {set_clause}
        """
        self._session.run(cypher, key=key, **{**match, **updates})

    def update_node_merge_props(
        self,
        label: str,
        key: str,
        props: dict[str, Any],
        match: Optional[dict[str, Any]] = None,
    ) -> None:
        """合并属性到现有节点（SET n += $props）。"""
        match = match or {}
        match_clause = "".join(f", {k}: ${k}" for k in match.keys())
        cypher = f"""
        MATCH (n:{label} {{key: $key{match_clause}}})
        SET n += $props
        """
        self._session.run(cypher, key=key, props=props, **match)

    def update_node_conditional(
        self,
        label: str,
        key: str,
        updates: dict[str, Any],
        condition_field: str,
        condition_value: Any,
        match: Optional[dict[str, Any]] = None,
    ) -> None:
        """条件更新（embedding-aware SET）。

        仅当 condition_field 的值与 condition_value 不同时才更新。
        """
        match = match or {}
        match_clause = "".join(f", {k}: ${k}" for k in match.keys())
        set_clauses = []
        for k, v in updates.items():
            if k == condition_field:
                set_clauses.append(f"n.{k} = ${k}")
            else:
                # 条件更新：只有 hash 变化时才更新
                set_clauses.append(
                    f"n.{k} = CASE WHEN n.{condition_field} <> ${condition_field} THEN ${k} ELSE n.{k} END"
                )
        set_clause = ", ".join(set_clauses)

        cypher = f"""
        MATCH (n:{label} {{key: $key{match_clause}}})
        SET {set_clause}
        """
        params = {"key": key, condition_field: condition_value, **match, **updates}
        self._session.run(cypher, **params)

    def delete_node(
        self,
        label: str,
        key: str,
        match: Optional[dict[str, Any]] = None,
    ) -> None:
        """删除节点及其关联边（DETACH DELETE）。"""
        match = match or {}
        match_clause = "".join(f", {k}: ${k}" for k in match.keys())
        cypher = f"""
        MATCH (n:{label} {{key: $key{match_clause}}})
        DETACH DELETE n
        """
        self._session.run(cypher, key=key, **match)

    # ========================================================================
    # 边操作
    # ========================================================================

    def upsert_edge(
        self,
        rel_type: str,
        start_label: str,
        start_key: str,
        end_label: str,
        end_key: str,
        props: Optional[dict[str, Any]] = None,
        start_match: Optional[dict[str, Any]] = None,
        end_match: Optional[dict[str, Any]] = None,
    ) -> None:
        """MERGE 边并设置属性。"""
        props = props or {}
        start_match = start_match or {}
        end_match = end_match or {}
        sm = "".join(f", {k}: $sm_{k}" for k in start_match.keys())
        em = "".join(f", {k}: $em_{k}" for k in end_match.keys())
        set_clauses = [f"r.{k} = ${k}" for k in props.keys()]
        set_clause = ", ".join(set_clauses) if set_clauses else ""

        cypher = f"""
        MATCH (a:{start_label} {{key: $start_key{sm}}})
        MATCH (b:{end_label} {{key: $end_key{em}}})
        MERGE (a)-[r:{rel_type}]->(b)
        """
        if set_clause:
            cypher += f"SET {set_clause}"

        params = {
            "start_key": start_key,
            "end_key": end_key,
            **{f"sm_{k}": v for k, v in start_match.items()},
            **{f"em_{k}": v for k, v in end_match.items()},
            **props,
        }
        self._session.run(cypher, **params)

    def upsert_edge_guarded(
        self,
        rel_type: str,
        start_label: str,
        start_key: str,
        end_label: str,
        end_key: str,
        props: Optional[dict[str, Any]] = None,
        start_match: Optional[dict[str, Any]] = None,
        end_match: Optional[dict[str, Any]] = None,
    ) -> None:
        """FOREACH guard 语义：仅当两端点都存在时才创建边。"""
        props = props or {}
        start_match = start_match or {}
        end_match = end_match or {}
        sm = "".join(f", {k}: $sm_{k}" for k in start_match.keys())
        em = "".join(f", {k}: $em_{k}" for k in end_match.keys())
        set_clauses = [f"r.{k} = ${k}" for k in props.keys()]
        set_clause = ", ".join(set_clauses) if set_clauses else ""

        cypher = f"""
        MATCH (a:{start_label} {{key: $start_key{sm}}})
        OPTIONAL MATCH (b:{end_label} {{key: $end_key{em}}})
        FOREACH (_ IN CASE WHEN b IS NULL THEN [] ELSE [b] END |
            MERGE (a)-[r:{rel_type}]->(b)
        """
        if set_clause:
            cypher += f"SET {set_clause}"
        cypher += ")"

        params = {
            "start_key": start_key,
            "end_key": end_key,
            **{f"sm_{k}": v for k, v in start_match.items()},
            **{f"em_{k}": v for k, v in end_match.items()},
            **props,
        }
        self._session.run(cypher, **params)

    def batch_upsert_edges(
        self,
        rel_type: str,
        edges: list[dict[str, Any]],
        start_label: str,
        end_label: str,
        start_key_field: str = "start_key",
        end_key_field: str = "end_key",
        start_match_fields: Optional[Sequence[str]] = None,
        end_match_fields: Optional[Sequence[str]] = None,
    ) -> None:
        """UNWIND + MATCH + MERGE 批量插入边。"""
        if not edges:
            return

        start_match_fields = list(start_match_fields or [])
        end_match_fields = list(end_match_fields or [])
        sample = edges[0]
        reserved = {start_key_field, end_key_field} | set(start_match_fields) | set(end_match_fields)
        props_keys = [k for k in sample.keys() if k not in reserved]

        sm = "".join(f", {f}: row.{f}" for f in start_match_fields)
        em = "".join(f", {f}: row.{f}" for f in end_match_fields)
        set_clauses = [f"r.{k} = row.{k}" for k in props_keys]
        set_clause = ", ".join(set_clauses) if set_clauses else ""

        cypher = f"""
        UNWIND $edges AS row
        MATCH (a:{start_label} {{key: row.{start_key_field}{sm}}})
        MATCH (b:{end_label} {{key: row.{end_key_field}{em}}})
        MERGE (a)-[r:{rel_type}]->(b)
        """
        if set_clause:
            cypher += f"SET {set_clause}"

        self._session.run(cypher, edges=edges)

    def batch_upsert_edges_guarded(
        self,
        rel_type: str,
        edges: list[dict[str, Any]],
        start_label: str,
        end_label: str,
        start_key_field: str = "start_key",
        end_key_field: str = "end_key",
        start_match_fields: Optional[Sequence[str]] = None,
        end_match_fields: Optional[Sequence[str]] = None,
    ) -> None:
        """UNWIND + OPTIONAL MATCH + FOREACH guard 批量插入边。"""
        if not edges:
            return

        start_match_fields = list(start_match_fields or [])
        end_match_fields = list(end_match_fields or [])
        sample = edges[0]
        reserved = {start_key_field, end_key_field} | set(start_match_fields) | set(end_match_fields)
        props_keys = [k for k in sample.keys() if k not in reserved]

        sm = "".join(f", {f}: row.{f}" for f in start_match_fields)
        em = "".join(f", {f}: row.{f}" for f in end_match_fields)
        set_clauses = [f"r.{k} = row.{k}" for k in props_keys]
        set_clause = ", ".join(set_clauses) if set_clauses else ""

        cypher = f"""
        UNWIND $edges AS row
        MATCH (a:{start_label} {{key: row.{start_key_field}{sm}}})
        OPTIONAL MATCH (b:{end_label} {{key: row.{end_key_field}{em}}})
        FOREACH (_ IN CASE WHEN b IS NULL THEN [] ELSE [b] END |
            MERGE (a)-[r:{rel_type}]->(b)
        """
        if set_clause:
            cypher += f"SET {set_clause}"
        cypher += ")"

        self._session.run(cypher, edges=edges)

    def delete_edges(
        self,
        rel_type: str,
        from_label: Optional[str] = None,
        from_key: Optional[str] = None,
        to_label: Optional[str] = None,
        to_key: Optional[str] = None,
        from_match: Optional[dict[str, Any]] = None,
        to_match: Optional[dict[str, Any]] = None,
    ) -> None:
        """删除边（可选按端点过滤）。"""
        from_match = from_match or {}
        to_match = to_match or {}
        fm = "".join(f", {k}: $fm_{k}" for k in from_match.keys())
        tm = "".join(f", {k}: $tm_{k}" for k in to_match.keys())
        from_match_cypher = (
            f"(a:{from_label} {{key: $from_key{fm}}})" if from_label and from_key else "(a)"
        )
        to_match_cypher = f"(b:{to_label} {{key: $to_key{tm}}})" if to_label and to_key else "(b)"

        cypher = f"""
        MATCH {from_match_cypher}-[r:{rel_type}]->{to_match_cypher}
        DELETE r
        """

        params = {}
        if from_key:
            params["from_key"] = from_key
            params.update({f"fm_{k}": v for k, v in from_match.items()})
        if to_key:
            params["to_key"] = to_key
            params.update({f"tm_{k}": v for k, v in to_match.items()})

        self._session.run(cypher, **params)

    def batch_upsert_edges_ordered(
        self,
        rel_type: str,
        nodes: list[dict[str, Any]],
        label: str,
        key_field: str = "key",
        order_field: Optional[str] = None,
        match_fields: Optional[Sequence[str]] = None,
    ) -> None:
        """按顺序创建链式边（先删除再重建）。"""
        if len(nodes) < 2:
            return

        match_fields = list(match_fields or [])
        # 先删除现有链式边
        self.delete_edges(rel_type, from_label=label)

        # 按 order_field 排序（如果提供）
        if order_field:
            nodes = sorted(nodes, key=lambda n: n.get(order_field, 0))

        # 创建链式边
        edges = []
        for i in range(len(nodes) - 1):
            edge = {
                "start_key": nodes[i][key_field],
                "end_key": nodes[i + 1][key_field],
            }
            for f in match_fields:
                edge[f] = nodes[i].get(f)
            edges.append(edge)

        self.batch_upsert_edges(
            rel_type,
            edges,
            label,
            label,
            start_match_fields=match_fields,
            end_match_fields=match_fields,
        )

    def node_exists(self, label: str, key: str, match: Optional[dict[str, Any]] = None) -> bool:
        """检查节点是否存在（供写事务内的前置探测）。"""
        match = match or {}
        match_clause = "".join(f", {k}: ${k}" for k in match.keys())
        rec = self._session.run(
            f"MATCH (n:{label} {{key: $key{match_clause}}}) RETURN 1 AS x LIMIT 1",
            key=key, **match,
        ).single()
        return rec is not None

    def edge_exists(
        self,
        rel_type: str,
        start_label: str,
        start_key: str,
        end_label: str,
        end_key: str,
        start_match: Optional[dict[str, Any]] = None,
        end_match: Optional[dict[str, Any]] = None,
    ) -> bool:
        """检查两端点间是否存在 rel_type 边（MERGE 边幂等守卫用）。"""
        start_match = start_match or {}
        end_match = end_match or {}
        sm = "".join(f", {k}: $sm_{k}" for k in start_match.keys())
        em = "".join(f", {k}: $em_{k}" for k in end_match.keys())
        rec = self._session.run(
            f"MATCH (a:{start_label} {{key: $sk{sm}}})-[r:{rel_type}]->"
            f"(b:{end_label} {{key: $ek{em}}}) RETURN 1 AS x LIMIT 1",
            sk=start_key, ek=end_key,
            **{
                **{f"sm_{k}": v for k, v in start_match.items()},
                **{f"em_{k}": v for k, v in end_match.items()},
            },
        ).single()
        return rec is not None

    def find_node_label(
        self,
        key: str,
        labels: Optional[Sequence[str]] = None,
        match: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """按 key 跨 label 查找节点所在 label。"""
        match = match or {}
        match_clause = "".join(f", {k}: ${k}" for k in match.keys())
        candidates = list(labels) if labels else []
        if candidates:
            label_or = " OR ".join(f"n:{lb}" for lb in candidates)
            cypher = f"MATCH (n) WHERE n.key = $key{match_clause} AND ({label_or}) RETURN labels(n)[0] AS lb LIMIT 1"
        else:
            cypher = f"MATCH (n) WHERE n.key = $key{match_clause} RETURN labels(n)[0] AS lb LIMIT 1"
        rec = self._session.run(cypher, key=key, **match).single()
        return rec["lb"] if rec else None

    def close(self) -> None:
        """关闭会话。"""
        self._session.close()


class Neo4jBackend(GraphBackend):
    """Neo4j 后端实现。

    使用官方 neo4j Python driver 连接 Neo4j 数据库。
    """

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        default_database: Optional[str] = None,
        driver: Any = None,
    ):
        """初始化 Neo4j 后端。

        Args:
            uri: Neo4j Bolt URI（如 bolt://localhost:7687）
            user: 用户名
            password: 密码
            default_database: 默认逻辑数据库（可选）
            driver: 外部注入的 driver（可选）。传入时本后端不拥有其生命周期，
                    ``close()`` 不关闭它（避免与注入方双重关闭，如 server
                    lifespan 复用 fallback_driver）。
        """
        if driver is not None:
            self._driver: Driver = driver
            self._owns_driver = False
        else:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                uri,
                auth=(user, password),
                notifications_min_severity="OFF",
            )
            self._owns_driver = True
        self._default_database = default_database
        log.info("Neo4j backend initialized: %s (owns_driver=%s)", uri, self._owns_driver)

    @contextmanager
    def session(self, *, database: Optional[str] = None) -> Iterator[Neo4jSession]:
        """创建 Neo4j 会话。

        Args:
            database: 逻辑数据库名，优先使用此参数；
                     若为 None，则使用 default_database

        Yields:
            Neo4jSession 实例
        """
        db = database or self._default_database
        kwargs = {"database": db} if db else {}

        session = self._driver.session(**kwargs)
        try:
            yield Neo4jSession(session)
        finally:
            session.close()

    def close(self) -> None:
        """关闭 Neo4j driver（仅当本后端拥有它时）。"""
        if self._owns_driver:
            self._driver.close()
            log.info("Neo4j backend closed")
        else:
            log.debug("Neo4j backend closed (driver externally owned; not closing)")

"""图数据库后端抽象层。

为不同图数据库提供统一的会话与写原语接口，通过配置选择后端。
社区版内置 Neo4j 实现；其他 openCypher 兼容后端可实现同一接口并通过
``registry.get_manager().register()`` 在运行时接入。

覆盖 12 种写模式：
- P1: MERGE_NODE → upsert_node()
- P2: BATCH_MERGE → batch_upsert_nodes()
- P3: NODE+CHAIN_EDGE → upsert_node() + upsert_edge()
- P4: BATCH+CHAIN → batch_upsert_nodes() + batch_upsert_edges()
- P5: FOREACH_GUARD_EDGE → upsert_edge_guarded()
- P6: UNWIND_GUARD_EDGE → batch_upsert_edges_guarded()
- P7: MATCH_EDGE → upsert_edge()
- P8: SIMPLE_SET → update_node()
- P9: SET_PLUS_EQUALS → update_node_merge_props()
- P10: CHAIN_REBUILD → delete_edges() + batch_upsert_edges_ordered()
- P11: EMBEDDING_AWARE → update_node_conditional()
- P12: COALESCE_PRESERVE → update_node() with conditional logic in caller
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, Sequence

log = logging.getLogger(__name__)


class GraphSession(ABC):
    """统一的图会话接口。

    所有图后端都实现此接口，提供一致的读写操作。
    """

    @abstractmethod
    def run(self, cypher: str, **params: Any) -> Any:
        """执行 Cypher 读查询。

        Args:
            cypher: Cypher 查询语句
            **params: 查询参数

        Returns:
            查询结果（后端特定类型）
        """

    @abstractmethod
    def execute_write(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        """执行写事务。

        用于需要在单个事务中执行多个写操作的场景。

        Args:
            fn: 写操作函数，接收一个事务/会话对象和 kwargs
            **kwargs: 传递给 fn 的参数

        Returns:
            fn 的返回值
        """

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
        """插入或更新节点（MERGE + ON CREATE/ON MATCH SET 语义）。

        如果节点不存在则创建并设置 props；如果已存在则更新 update_props（或 props）。

        Args:
            label: 节点标签（如 'Person', 'Table'）
            key: 节点唯一标识（对应 .key 属性）
            props: 创建时设置的属性字典（ON CREATE SET）
            update_props: 更新时设置的属性字典（ON MATCH SET）；
                         若为 None 则使用 props
            match: 额外匹配属性；MERGE 的复合匹配键。节点按 ``key`` + match
                   全部字段定位；写入时 match 字段也会落到属性上。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 upsert_node")

    def upsert_node_identity(
        self,
        label: str,
        identity: dict[str, Any],
        create_props: dict[str, Any],
        update_props: Optional[dict[str, Any]] = None,
    ) -> None:
        """按任意身份字段 MERGE 节点（非 key 唯一标识场景）。

        与 :meth:`upsert_node` 相同语义，但匹配键由 ``identity`` 全量指定
        （如 UserMemory 用 ``{id}``），不假定存在 ``key`` 属性。
        identity 字段也写入节点属性。

        Args:
            label: 节点标签
            identity: MERGE 匹配的完整身份属性字典
            create_props: ON CREATE SET 的属性
            update_props: ON MATCH SET 的属性；None 则不额外更新（仅 identity）
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 upsert_node_identity")

    def read_node(
        self,
        label: str,
        identity: dict[str, Any],
        fields: Sequence[str],
    ) -> Optional[dict[str, Any]]:
        """按身份字段读单节点的指定字段；不存在返回 None。"""
        raise NotImplementedError(f"{type(self).__name__} 不支持 read_node")

    def batch_upsert_nodes(
        self,
        label: str,
        nodes: list[dict[str, Any]],
        key_field: str = "key",
        match_fields: Optional[Sequence[str]] = None,
    ) -> None:
        """批量插入或更新节点（UNWIND + MERGE 语义）。

        Args:
            label: 节点标签
            nodes: 节点列表，每个元素是属性字典
            key_field: 用作唯一标识的字段名（默认 'key'）
            match_fields: 额外参与 MERGE 匹配的字段名，从每个节点字典取值。
        """
        # 默认实现：逐个调用
        for node in nodes:
            key = node.get(key_field)
            if key is None:
                raise ValueError(f"Node missing '{key_field}': {node}")
            m = (
                {f: node.get(f) for f in match_fields if node.get(f) is not None}
                if match_fields
                else None
            )
            self.upsert_node(label, key, node, match=m or None)

    def update_node(
        self,
        label: str,
        key: str,
        updates: dict[str, Any],
        match: Optional[dict[str, Any]] = None,
    ) -> None:
        """更新现有节点属性（MATCH + SET 语义）。

        Args:
            label: 节点标签
            key: 节点 key
            updates: 要更新的属性字典
            match: 额外匹配属性（复合键）
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 update_node")

    def update_node_merge_props(
        self,
        label: str,
        key: str,
        props: dict[str, Any],
        match: Optional[dict[str, Any]] = None,
    ) -> None:
        """合并属性到现有节点（MATCH + SET n += $props 语义）。

        Args:
            label: 节点标签
            key: 节点 key
            props: 要合并的属性字典（现有属性保留，新属性覆盖或添加）
            match: 额外匹配属性（复合键）
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 update_node_merge_props")

    def update_node_conditional(
        self,
        label: str,
        key: str,
        updates: dict[str, Any],
        condition_field: str,
        condition_value: Any,
        match: Optional[dict[str, Any]] = None,
    ) -> None:
        """条件更新节点属性（embedding-aware SET 语义）。

        仅当 condition_field 的值与 condition_value 不同时才更新。
        用于 idempotent embedding 更新：
        SET emb = CASE WHEN hash <> existing THEN $vec ELSE existing END

        Args:
            label: 节点标签
            key: 节点 key
            updates: 要更新的属性字典
            condition_field: 条件字段名（如 'embedding_hash'）
            condition_value: 条件值（如新的 hash）
            match: 额外匹配属性（复合键）
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 update_node_conditional")

    def delete_node(
        self,
        label: str,
        key: str,
        match: Optional[dict[str, Any]] = None,
    ) -> None:
        """删除节点及其关联边（DETACH DELETE 语义）。

        Args:
            label: 节点标签
            key: 节点 key
            match: 额外匹配属性（复合键）
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 delete_node")

    def delete_nodes_batch(
        self,
        label: str,
        node_ids: Sequence[Any],
        edge_labels: Optional[Sequence[str]] = None,
    ) -> None:
        """按内部 id 批量删除节点及其关联边（DETACH DELETE 语义的批量版）。

        相比逐个 :meth:`delete_node`，实现后端应尽量用少数几条语句完成，
        避免逐节点逐表删边的 round-trip 风暴。

        Args:
            label: 节点标签
            node_ids: 节点内部 id 列表
            edge_labels: 关联边 label 白名单（可选）。传入时只清理这些边，
                调用方已知删除涉及面时应收窄；None = 全部边。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 delete_nodes_batch")

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
        """插入或更新边（MATCH + MERGE 语义）。

        要求起点和终点都存在，否则抛出异常或跳过。

        Args:
            rel_type: 关系类型（如 'KNOWS', 'HAS_COLUMN'）
            start_label: 起点标签
            start_key: 起点 key
            end_label: 终点标签
            end_key: 终点 key
            props: 边属性字典（可选）
            start_match: 起点额外匹配属性（复合键）
            end_match: 终点额外匹配属性（复合键）
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 upsert_edge")

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
        """插入或更新边，仅当两端点都存在时（FOREACH guard 语义）。

        如果任一端点不存在则静默跳过（不报错）。
        用于跨层/可选依赖的边创建。

        Args:
            rel_type: 关系类型
            start_label: 起点标签
            start_key: 起点 key
            end_label: 终点标签
            end_key: 终点 key
            props: 边属性字典（可选）
            start_match: 起点额外匹配属性（复合键）
            end_match: 终点额外匹配属性（复合键）
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 upsert_edge_guarded")

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
        """批量插入或更新边（UNWIND + MATCH + MERGE 语义）。

        Args:
            rel_type: 关系类型
            edges: 边列表，每个元素包含 start_key, end_key 和可选属性
            start_label: 起点标签
            end_label: 终点标签
            start_key_field: 起点 key 字段名
            end_key_field: 终点 key 字段名
            start_match_fields: 边字典中参与起点复合匹配的字段名
            end_match_fields: 边字典中参与终点复合匹配的字段名
        """
        # 默认实现：逐个调用
        for edge in edges:
            sk = edge.get(start_key_field)
            ek = edge.get(end_key_field)
            if sk is None or ek is None:
                raise ValueError(f"Edge missing keys: {edge}")
            props = {k: v for k, v in edge.items() if k not in (start_key_field, end_key_field)}
            self.upsert_edge(rel_type, start_label, sk, end_label, ek, props)

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
        """批量插入边，仅当两端点都存在（UNWIND + OPTIONAL MATCH + FOREACH guard 语义）。

        Args:
            rel_type: 关系类型
            edges: 边列表
            start_label: 起点标签
            end_label: 终点标签
            start_key_field: 起点 key 字段名
            end_key_field: 终点 key 字段名
            start_match_fields: 边字典中参与起点复合匹配的字段名
            end_match_fields: 边字典中参与终点复合匹配的字段名
        """
        # 默认实现：逐个调用 guarded 版本
        for edge in edges:
            sk = edge.get(start_key_field)
            ek = edge.get(end_key_field)
            if sk is None or ek is None:
                continue
            props = {k: v for k, v in edge.items() if k not in (start_key_field, end_key_field)}
            self.upsert_edge_guarded(rel_type, start_label, sk, end_label, ek, props)

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
        """删除边（可选按端点过滤）。

        Args:
            rel_type: 关系类型
            from_label: 起点标签（可选）
            from_key: 起点 key（可选）
            to_label: 终点标签（可选）
            to_key: 终点 key（可选）
            from_match: 起点额外匹配属性（复合键）
            to_match: 终点额外匹配属性（复合键）
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 delete_edges")

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
        """检查两端点间是否存在 ``rel_type`` 边（MERGE 边幂等守卫用）。"""
        raise NotImplementedError(f"{type(self).__name__} 不支持 edge_exists")

    def find_node_label(
        self,
        key: str,
        labels: Optional[Sequence[str]] = None,
        match: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """按 key（+复合匹配）跨 label 查找节点所在 label；找不到返回 None。

        供"只知道 key 不知道 label"的边写入场景（如 RELATED_TO 邻居）。
        ``labels`` 限定候选 label 列表，默认搜全部已知 label。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 find_node_label")

    def batch_upsert_edges_ordered(
        self,
        rel_type: str,
        nodes: list[dict[str, Any]],
        label: str,
        key_field: str = "key",
        order_field: Optional[str] = None,
        match_fields: Optional[Sequence[str]] = None,
    ) -> None:
        """按顺序创建链式边（CHAIN_REBUILD 语义）。

        先删除现有边，再按 order_field 排序后创建链式边。
        用于 submit_trace 中的 NEXT 链重建。

        Args:
            rel_type: 关系类型（如 'NEXT'）
            nodes: 节点列表
            label: 节点标签
            key_field: key 字段名
            order_field: 排序字段名（如 'ts', 'step_idx'）；None 则按列表顺序
            match_fields: 节点字典中参与复合匹配的字段名
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 batch_upsert_edges_ordered")

    @abstractmethod
    def close(self) -> None:
        """关闭会话。"""


class GraphBackend(ABC):
    """图数据库后端抽象基类。

    管理连接池和会话创建。
    """

    @abstractmethod
    @contextmanager
    def session(self, *, database: Optional[str] = None) -> Iterator[GraphSession]:
        """创建并返回一个会话上下文管理器。

        Args:
            database: 逻辑数据库名（可选）

        Yields:
            GraphSession 实例
        """

    @abstractmethod
    def close(self) -> None:
        """关闭后端连接，释放资源。"""

"""图数据库后端工厂。

根据配置选择后端。社区版内置 Neo4j；其他 openCypher 兼容后端可实现
``GraphBackend`` 接口后通过 ``registry.get_manager().register()`` 接入。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .base import GraphBackend, GraphSession

if TYPE_CHECKING:
    from ...config import Config

log = logging.getLogger(__name__)

__all__ = [
    "GraphBackend",
    "GraphSession",
    "get_backend",
]


def get_backend(cfg: "Config", *, neo4j_driver: Any = None) -> GraphBackend:
    """根据配置创建图数据库后端。

    Args:
        cfg: Config 实例
        neo4j_driver: 外部注入的 Neo4j driver（可选，见 Neo4jBackend）

    Returns:
        GraphBackend 实例

    Raises:
        ValueError: 未知的 graph_backend 类型
    """
    backend_type = cfg.graph_backend.lower()

    if backend_type == "neo4j":
        from .neo4j_backend import Neo4jBackend

        return Neo4jBackend(
            uri=cfg.neo4j_uri,
            user=cfg.neo4j_user,
            password=cfg.neo4j_password,
            default_database=cfg.neo4j_database,
            driver=neo4j_driver,
        )

    raise ValueError(
        f"Unknown graph_backend: {backend_type!r}. Built-in: 'neo4j'. "
        "自定义后端请实现 GraphBackend 并通过 "
        "graph.backends.registry.get_manager().register() 注册。"
    )

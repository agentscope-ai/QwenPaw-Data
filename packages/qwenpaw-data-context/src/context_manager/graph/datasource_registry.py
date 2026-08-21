"""数据源注册表：``datasource_id → DatasourceInfo`` 的唯一映射源。

每个数据源对应 1 个 JDBC 连接（1 个数据库），可包含多个物理 ``db_id``。
注册表同时服务：

- **SQL 执行路由**（Task 3）——``resolve_backend()`` 返回执行后端标识
- **图谱隔离**（Task 4）——``resolve().db_ids`` 给出该数据源下的物理库集合

配置来源（优先级从高到低）：

1. 环境变量 ``DATASOURCES_CONFIG`` 指向的 JSON 文件
2. ``semantic-layer/config/datasources.json``
3. 内置 ``_BUILTIN_DATASOURCES`` 兜底

新增数据源只需在 JSON 里加一条，不必改代码。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------- #
# 内置兜底（config 文件不存在时使用）
# ---------------------------------------------------------------------- #
_BUILTIN_DATASOURCES: list[dict[str, Any]] = [
    {
        "datasource_id": "example",
        "display_name": "Example Datasource",
        "db_type": "postgres",
        "default_sql_backend": "direct",
        "db_ids": ["example_db"],
        "neo4j_database": "neo4j",
    },
]


# ---------------------------------------------------------------------- #
# DatasourceInfo
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class DatasourceInfo:
    """一个数据源的完整配置。"""

    datasource_id: str
    display_name: str = ""
    db_type: str = ""
    default_sql_backend: str = ""
    db_ids: frozenset[str] = field(default_factory=frozenset)
    neo4j_database: str = ""

    @property
    def primary_db_id(self) -> str:
        """返回第一个 db_id（用于未显式指定 db_id 时的默认值）。"""
        return next(iter(self.db_ids)) if self.db_ids else ""


# ---------------------------------------------------------------------- #
# Registry 单例
# ---------------------------------------------------------------------- #
_REGISTRY: dict[str, DatasourceInfo] = {}
_LOADED: bool = False


def _config_path() -> Path:
    """datasources.json 的路径：``DATASOURCES_CONFIG`` 环境变量 > 默认位置。"""
    raw = (os.getenv("DATASOURCES_CONFIG") or "").strip()
    if raw:
        return Path(raw)
    # 包根 = src/context_manager/graph/ 上溯四级（graph -> context_manager -> src -> 包根）。
    return Path(__file__).resolve().parent.parent.parent.parent / "config" / "datasources.json"


def _parse_entry(raw: dict[str, Any]) -> DatasourceInfo:
    """把 JSON 里的一条记录解析为 DatasourceInfo。"""
    db_ids = raw.get("db_ids") or []
    return DatasourceInfo(
        datasource_id=raw["datasource_id"],
        display_name=raw.get("display_name", ""),
        db_type=raw.get("db_type", ""),
        default_sql_backend=raw.get("default_sql_backend", ""),
        db_ids=frozenset(db_ids),
        neo4j_database=raw.get("neo4j_database", ""),
    )


def load_registry() -> None:
    """从 JSON 加载注册表（幂等，进程内只加载一次）。

    文件不存在或解析失败时静默使用内置兜底。
    """
    global _REGISTRY, _LOADED
    if _LOADED:
        return

    entries: list[dict[str, Any]] = _BUILTIN_DATASOURCES
    path = _config_path()
    if path.is_file():
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "datasources" in data:
                entries = data["datasources"]
                log.info("datasource registry loaded from %s (%d entries)", path, len(entries))
            else:
                log.warning("datasource registry: %s has unexpected format, using builtin", path)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("datasource registry: failed to load %s: %s; using builtin", path, exc)
    else:
        log.debug("datasource registry: %s not found, using builtin", path)

    _REGISTRY.clear()
    for raw in entries:
        ds_id = raw.get("datasource_id", "").strip()
        if not ds_id:
            continue
        _REGISTRY[ds_id] = _parse_entry(raw)

    _LOADED = True


def _ensure_loaded() -> None:
    if not _LOADED:
        load_registry()


# ---------------------------------------------------------------------- #
# Public API
# ---------------------------------------------------------------------- #
def resolve(datasource_id: str) -> DatasourceInfo:
    """查找数据源配置。找不到时抛 ``ValueError``。"""
    _ensure_loaded()
    ds_id = (datasource_id or "").strip()
    if ds_id in _REGISTRY:
        return _REGISTRY[ds_id]
    raise ValueError(
        f"unknown datasource_id '{datasource_id}'; "
        f"known: {sorted(_REGISTRY.keys())}"
    )


def try_resolve(datasource_id: str) -> Optional[DatasourceInfo]:
    """查找数据源配置。找不到时返回 ``None``。"""
    _ensure_loaded()
    return _REGISTRY.get((datasource_id or "").strip())


def resolve_backend(datasource_id: str) -> str:
    """返回该数据源的默认 SQL 执行后端标识。"""
    ds = resolve(datasource_id)
    return ds.default_sql_backend


def list_datasources() -> list[DatasourceInfo]:
    """返回所有已注册的数据源。"""
    _ensure_loaded()
    return list(_REGISTRY.values())


def db_id_to_datasource(db_id: str) -> Optional[DatasourceInfo]:
    """反向查找：给定物理 db_id，返回所属数据源。"""
    _ensure_loaded()
    for ds in _REGISTRY.values():
        if db_id in ds.db_ids:
            return ds
    return None

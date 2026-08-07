"""当前生效数据源的切换与查询。

数据源连接信息统一由 ``semantic_config.db`` 的 ``datasource.config`` 字段维护。本模块只负责：
- 记录用户通过 ``PUT /api/datasources/active`` 切换的当前生效 ``datasource_id``；
- 提供给 executor 连库时读取该数据源 config 的桥接函数。
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..utils import get_logger

log = get_logger("api.datasource_active")

router = APIRouter(tags=["datasource-active"])

# 当前生效的 datasource_id（用户切换；空串表示未切换，executor 将拒绝连库）
_active_datasource_id: str = ""


def get_active_datasource_id() -> str:
    """返回当前生效的 datasource_id，未切换则返回空串。"""
    return _active_datasource_id


# 别名，保持与旧代码兼容（executor/cm_api/cm_resolve 仍在用这个名字）
def get_synced_default_datasource_id() -> str:
    return _active_datasource_id


def set_active_datasource(datasource_id: str) -> str:
    """显式设置当前生效的数据源（用户切换）。

    校验该 datasource_id 在 semantic_config.db 的 datasource 表中存在后，
    更新 ``_active_datasource_id``。executor 连库时会从 config 字段读连接凭证。

    返回设置后的 datasource_id；校验失败抛 ValueError。
    """
    code = (datasource_id or "").strip()
    if not code:
        raise ValueError("datasource_id 不能为空")
    if not _datasource_id_exists(code):
        raise ValueError(
            f"datasource_id {code!r} 在 semantic_config.db 中未找到或已删除"
        )
    global _active_datasource_id
    _active_datasource_id = code
    log.info("active datasource switched to: %s", code)
    return code


def _semantic_config_db_path() -> str:
    """返回 semantic_config.db 路径。

    优先用环境变量 SEMANTIC_CONFIG_DB_PATH；否则回退到
    ``$DATAPAW_HOME/data-bridge/state/semantic_config.db``
   （与 semantic_config.config.Settings.db_path 默认值一致）。
    """
    raw = (os.getenv("SEMANTIC_CONFIG_DB_PATH") or "").strip()
    if raw:
        return raw
    from datapaw.context.paths import semantic_config_db_path as _default
    return str(_default())


def _datasource_id_exists(code: str) -> bool:
    """检查标识符在 semantic_config.db 是否存在（未软删）。"""
    db_path = _semantic_config_db_path()
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            row = con.execute(
                "SELECT 1 FROM datasource "
                "WHERE (datasource_id = ? OR CAST(id AS TEXT) = ?) "
                "AND is_deleted = 0",
                (code, code),
            ).fetchone()
        return row is not None
    except Exception as exc:
        log.warning("semantic_config.db existence check failed for code=%s: %s", code, exc)
        return False


def load_datasource_config(datasource_id: str) -> dict[str, Any] | None:
    """从 semantic_config.db 按 datasource_id 或数字 id 读 config 字段。

    ``datasource_id`` 参数既可能是 host 注入的数字主键 ``id``，也可能是
    ``datasource_id``，两者任一命中即返回该行 config。
    config 在表里存 TEXT（JSON 字符串），这里解成 dict 返回。
    找不到 code / 无 config → 返回 None。附带 ``_datasource_type`` 方便调用方按类型解参数。
    """
    code = (datasource_id or "").strip()
    if not code:
        return None
    db_path = _semantic_config_db_path()
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT datasource_type, config FROM datasource "
                "WHERE (datasource_id = ? OR CAST(id AS TEXT) = ?) "
                "AND is_deleted = 0",
                (code, code),
            ).fetchone()
    except Exception as exc:
        log.warning("semantic_config.db read failed for code=%s: %s", code, exc)
        return None
    if row is None:
        return None
    raw = row["config"]
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("config JSON parse failed for code=%s: %s", code, exc)
        return None
    if not isinstance(cfg, dict):
        return None
    cfg["_datasource_type"] = (row["datasource_type"] or "").strip().lower()
    return cfg


# 图谱 canonical ID → semantic_config.db 中对应的数据源类型集合。
# 仅用于「按 canonical ID 反查真实连库 code」的兜底（如 SQL 图谱路由推断出的 ODPS）。
_CANONICAL_TO_TYPES: dict[str, tuple[str, ...]] = {
    "appdata": ("hologres", "postgresql", "postgres"),
    "analytics_dw": ("odps",),
}


def resolve_datasource_id_by_canonical(canonical: str) -> str | None:
    """把图谱 canonical ID 映射到 semantic_config.db 中对应类型的真实 datasource_id。

    连库凭证按真实 datasource_id/数字 id 查，而图谱路由只能给出 canonical ID
    （``appdata`` / ``analytics_dw``）。此函数按类型在库里找那条真实数据源。
    同类型有多条时取 ``id`` 最小的一条；无匹配 / 非 canonical → 返回 None。
    """
    types = _CANONICAL_TO_TYPES.get((canonical or "").strip())
    if not types:
        return None
    db_path = _semantic_config_db_path()
    placeholders = ",".join("?" * len(types))
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            row = con.execute(
                "SELECT datasource_id FROM datasource "
                f"WHERE lower(datasource_type) IN ({placeholders}) "
                "AND is_deleted = 0 ORDER BY id LIMIT 1",
                types,
            ).fetchone()
        return row[0] if row and row[0] else None
    except Exception as exc:
        log.warning("resolve canonical->id failed for %s: %s", canonical, exc)
        return None


def resolve_datasource_id(identifier: str) -> str:
    """把数字主键 ``id`` / ``datasource_id`` / 图谱 canonical ID 统一解析成
    semantic_config.db 中的真实 ``datasource_id``（即图谱节点打标签用的值）。
    """
    code = (identifier or "").strip()
    if not code:
        return ""
    db_path = _semantic_config_db_path()
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            row = con.execute(
                "SELECT datasource_id FROM datasource "
                "WHERE (datasource_id = ? OR CAST(id AS TEXT) = ?) AND is_deleted = 0",
                (code, code),
            ).fetchone()
        if row and row[0]:
            return row[0]
    except Exception as exc:
        log.warning("resolve_datasource_id failed for %s: %s", code, exc)
    canon = resolve_datasource_id_by_canonical(code)
    if canon:
        return canon
    return code


def resolve_pg_connect_kwargs(
    *,
    datasource_id: str | None = None,
    schema: str | None = None,
    search_path: bool = True,
) -> dict[str, Any]:
    """从 semantic_config.db 读取 PG 协议连接参数（``psycopg.connect`` kwargs）。

    优先级：显式 ``datasource_id`` → 当前 active datasource（用户 sync + 切换）。
    """
    code = (datasource_id or "").strip() or get_synced_default_datasource_id()
    if not code:
        raise RuntimeError(
            "未选择数据源：请先通过 PUT /api/datasources/active 切换数据源"
        )
    cfg = load_datasource_config(code)
    if cfg is None:
        raise RuntimeError(
            f"datasource_id={code!r} 在 semantic_config.db 中无 config，"
            f"请确认数据源已 sync 且连接信息完整"
        )
    ds_type = cfg.get("_datasource_type", "")
    if ds_type not in ("postgresql", "postgres", "hologres"):
        raise RuntimeError(
            f"datasource_id={code!r} 类型 {ds_type!r} 不支持 PG 协议直连"
        )
    kwargs: dict[str, Any] = {
        "host": cfg["host"],
        "port": int(cfg.get("port") or 5432),
        "user": cfg["user"],
        "password": cfg["password"],
        "dbname": cfg["dbname"],
    }
    if search_path:
        sch = (schema or "public").strip() or "public"
        kwargs["options"] = f"-c search_path={sch}"
    return kwargs


def resolve_mysql_connect_kwargs(
    *,
    datasource_id: str | None = None,
) -> dict[str, Any]:
    """从 semantic_config.db 读取 MySQL 连接参数（``pymysql.connect`` kwargs）。

    优先级：显式 ``datasource_id`` → 当前 active datasource。
    """
    code = (datasource_id or "").strip() or get_synced_default_datasource_id()
    if not code:
        raise RuntimeError(
            "未选择数据源：请先通过 PUT /api/datasources/active 切换数据源"
        )
    cfg = load_datasource_config(code)
    if cfg is None:
        raise RuntimeError(
            f"datasource_id={code!r} 在 semantic_config.db 中无 config，"
            f"请确认数据源已 sync 且连接信息完整"
        )
    ds_type = cfg.get("_datasource_type", "")
    if ds_type != "mysql":
        raise RuntimeError(
            f"datasource_id={code!r} 类型 {ds_type!r} 不是 mysql"
        )
    return {
        "host": cfg["host"],
        "port": int(cfg.get("port") or 3306),
        "user": cfg["user"],
        "password": cfg["password"],
        "database": cfg["database"],
    }


def resolve_odps_connect_config(
    *,
    datasource_id: str | None = None,
) -> dict[str, Any]:
    """从 semantic_config.db 读取 ODPS 连接参数（PyODPS ``ODPS`` 构造参数）。

    优先级：显式 ``datasource_id`` → 当前 active datasource。
    返回 dict 含 access_key_id / access_key_secret / project / endpoint / sts_token。
    """
    code = (datasource_id or "").strip() or get_synced_default_datasource_id()
    if not code:
        raise RuntimeError(
            "未选择数据源：请先通过 PUT /api/datasources/active 切换数据源"
        )
    cfg = load_datasource_config(code)
    if cfg is None:
        raise RuntimeError(
            f"datasource_id={code!r} 在 semantic_config.db 中无 config，"
            f"请确认数据源已 sync 且连接信息完整"
        )
    ds_type = cfg.get("_datasource_type", "")
    if ds_type != "odps":
        raise RuntimeError(
            f"datasource_id={code!r} 类型 {ds_type!r} 不是 odps"
        )
    return {
        "access_key_id": cfg["access_key_id"],
        "access_key_secret": cfg["access_key_secret"],
        "project": cfg["project"],
        "endpoint": cfg["endpoint"],
        "sts_token": cfg.get("sts_token"),
    }


class _SetActiveRequest(BaseModel):
    datasource_id: str = ""


class _SetActiveResponse(BaseModel):
    success: bool = True
    datasource_id: str = ""
    message: str = ""


@router.put("/api/datasources/active", response_model=_SetActiveResponse)
@router.put("/api/v1/cm/datasources/active", response_model=_SetActiveResponse)
def set_active_datasource_endpoint(req: _SetActiveRequest):
    """切换当前生效的数据源。

    切换后 executor 连库时会从 semantic_config.db 的 config 字段读该
    datasource_id 的连接凭证。
    """
    try:
        code = set_active_datasource(req.datasource_id)
        return _SetActiveResponse(success=True, datasource_id=code, message="switched")
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"success": False, "code": "INVALID_DATASOURCE", "message": str(exc)},
        )

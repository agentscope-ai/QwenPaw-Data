"""ODPS akless client — 连接凭证统一来自 ``semantic_config.db``。

本模块是 ``executor._execute_sql_via_odps_akless`` 和 ``ingest._reflect_odps``
的共享入口：通过 ``datasource_active_api.resolve_odps_connect_config`` 从
SQLite 数据源 config 字段读 access_key_id / access_key_secret / project /
endpoint / sts_token，构造 PyODPS ``ODPS`` client。

不再从环境变量（``CFG.odps_*``）读任何凭证。
"""
from __future__ import annotations

from typing import Any

from ..utils import get_logger

log = get_logger("graph.odps_akless")


def get_odps_client(
    *,
    datasource_id: str | None = None,
) -> Any:
    """构造一个 PyODPS ``ODPS`` client，凭证来自 semantic_config.db。

    Args:
        datasource_id: 显式指定数据源 id；为空则用当前 active datasource。

    Returns:
        ``odps.OODPS`` 实例。

    Raises:
        RuntimeError: 未选择数据源、config 缺失、类型不是 odps。
        ImportError: pyodps 未安装。
    """
    from ..api.datasource_active_api import resolve_odps_connect_config

    cfg = resolve_odps_connect_config(datasource_id=datasource_id)
    return _build_odps(cfg)


def get_odps_client_for(datasource_id: str) -> Any:
    """显式按 datasource_id 构造 ODPS client（不接受空值回退）。"""
    if not (datasource_id or "").strip():
        raise RuntimeError("datasource_id 不能为空")
    return get_odps_client(datasource_id=datasource_id)


def _build_odps(cfg: dict[str, Any]) -> Any:
    """按 config dict 构造 PyODPS client（STS 优先）。"""
    from odps import ODPS

    sts_token = cfg.get("sts_token")
    if sts_token:
        from odps.accounts import StsAccount

        account = StsAccount(
            cfg["access_key_id"],
            cfg["access_key_secret"],
            sts_token,
        )
        return ODPS(
            account=account,
            project=cfg["project"],
            endpoint=cfg["endpoint"],
        )
    return ODPS(
        cfg["access_key_id"],
        cfg["access_key_secret"],
        project=cfg["project"],
        endpoint=cfg["endpoint"],
    )


__all__ = ["get_odps_client", "get_odps_client_for"]

"""数据源连接测试

三类协议各自用原生驱动连一次库并做一次轻量探测（数 information_schema 表 / list_tables），
成功返回连接信息与表数，失败把异常信息回传。阻塞的驱动调用放到线程池执行，
并用 ``asyncio.wait_for`` 兜底整体超时。
"""
from __future__ import annotations

import asyncio
import time

from semantic_config.models.datasource import ConnectionTestResponse
from semantic_config.models.datasource_config import DatasourceType, resolve_type, validate_config

CONNECT_TIMEOUT = 10  # 秒；驱动级连接超时
_OVERALL_TIMEOUT = CONNECT_TIMEOUT + 5  # 秒；wait_for 兜底（覆盖 ODPS 等无 connect_timeout 的场景）
_CREDENTIAL_FIELDS = frozenset(
    {"password", "access_key_id", "access_key_secret", "sts_token"}
)


def _safe_error_message(exc: Exception, config: dict) -> str:
    """Redact known credential values even if a driver echoes them verbatim."""
    from context_manager.secrets.redact import _redact_str

    message = _redact_str(str(exc))
    for key in _CREDENTIAL_FIELDS:
        value = config.get(key)
        if value not in (None, ""):
            message = message.replace(str(value), "***")
    return message[:500]


def _test_postgres(cfg: dict) -> tuple[str, int | None]:
    import psycopg

    with psycopg.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
        connect_timeout=CONNECT_TIMEOUT,
    ) as conn:
        # 连接成功即已完成鉴权 + 可达性验证；SELECT 1 仅确认会话可用，不依赖任何表权限。
        conn.autocommit = True  # 避免尽力查表失败时污染事务
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            tables = _try_count(
                cur,
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'",
            )
    return f"connected to {cfg['host']}:{cfg['port']}/{cfg['dbname']}", tables


def _test_mysql(cfg: dict) -> tuple[str, int | None]:
    import pymysql

    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        database=cfg["database"],
        user=cfg["user"],
        password=cfg["password"],
        charset="utf8mb4",
        connect_timeout=CONNECT_TIMEOUT,
    )
    try:
        # connect(database=...) 成功即已鉴权且拿到库访问权；SELECT 1 不依赖任何表权限。
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            tables = _try_count(
                cur,
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                (cfg["database"],),
            )
    finally:
        conn.close()
    return f"connected to {cfg['host']}:{cfg['port']}/{cfg['database']}", tables


def _test_odps(cfg: dict) -> tuple[str, int | None]:
    from odps import ODPS

    sts_token = cfg.get("sts_token")
    if sts_token:
        from odps.accounts import StsAccount

        account = StsAccount(cfg["access_key_id"], cfg["access_key_secret"], sts_token)
        o = ODPS(account=account, project=cfg["project"], endpoint=cfg["endpoint"])
    else:
        o = ODPS(
            cfg["access_key_id"],
            cfg["access_key_secret"],
            project=cfg["project"],
            endpoint=cfg["endpoint"],
        )
    # ODPS 构造不发起网络请求；exist_project 是最轻的一次鉴权 + 项目可达性探测。
    if not o.exist_project(cfg["project"]):
        raise RuntimeError(f"project '{cfg['project']}' 不存在或无访问权限")
    tables: int | None
    try:  # 表数量尽力而为：账号无 list 权限时不影响连接判定
        tables = len(list(o.list_tables(max_items=10)))
    except Exception:  # noqa: BLE001
        tables = None
    return f"connected to ODPS project {cfg['project']}", tables


def _try_count(cur, sql: str, params: tuple = ()) -> int | None:
    """尽力执行一次计数查询，失败（如无 information_schema 权限）返回 None，不影响连接判定。"""
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001
        return None


def _test_duckdb(cfg: dict) -> tuple[str, int | None]:
    from pathlib import Path

    import duckdb

    path = Path(cfg["path"]).expanduser()
    if not path.is_file():
        raise RuntimeError(f"DuckDB 数据库文件不存在: {path}")
    conn = duckdb.connect(str(path), read_only=True)
    try:
        row = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchone()
        tables = int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()
    return f"connected to {path}", tables


def _test_sqlite(cfg: dict) -> tuple[str, int | None]:
    import sqlite3
    from pathlib import Path

    path = Path(cfg["path"]).expanduser()
    if not path.is_file():
        raise RuntimeError(f"SQLite 数据库文件不存在: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=CONNECT_TIMEOUT)
    try:
        cur = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'")
        row = cur.fetchone()
        tables = int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()
    return f"connected to {path}", tables


_TESTER_BY_TYPE = {
    DatasourceType.POSTGRESQL: _test_postgres,
    DatasourceType.MYSQL: _test_mysql,
    DatasourceType.ODPS: _test_odps,
    # MySQL 线协议兼容引擎：探测通道与 MySQL 相同。
    DatasourceType.STARROCKS: _test_mysql,
    DatasourceType.DORIS: _test_mysql,
    DatasourceType.TIDB: _test_mysql,
    DatasourceType.DUCKDB: _test_duckdb,
    DatasourceType.SQLITE: _test_sqlite,
}


async def test_connection(datasource_type: str | None, config: dict | None) -> ConnectionTestResponse:
    """校验并测试连接。config 结构非法 → BadRequestError(400)；连接失败 → success=False。"""
    normalized = validate_config(datasource_type, config)
    dt = resolve_type(datasource_type)
    fn = _TESTER_BY_TYPE[dt]

    t0 = time.monotonic()
    try:
        message, tables = await asyncio.wait_for(
            asyncio.to_thread(fn, normalized), timeout=_OVERALL_TIMEOUT
        )
        success, tables_found = True, tables
    except asyncio.TimeoutError:
        message, tables_found, success = f"连接超时（>{_OVERALL_TIMEOUT}s）", None, False
    except Exception as exc:  # noqa: BLE001 — 驱动异常统一回传为失败信息
        message, tables_found, success = _safe_error_message(exc, normalized), None, False

    return ConnectionTestResponse(
        success=success,
        message=message,
        tables_found=tables_found,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

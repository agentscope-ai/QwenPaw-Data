"""PostgreSQL 的查询日志来源。

日志真源是 ``hologres.hg_query_log`` —— 生产的 PG 兼容端点就是 Hologres。
但那是**表位置**知识，不是独立方言：``holo`` / ``hologres`` / ``psycopg2``
这些键已全部删除，PG 系一律 ``postgresql`` / ``pg``。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import AlgoConfig
from ..snapshots.query_log import RawQueryLogSnapshot
from .query_log_base import QueryLogInput

logger = logging.getLogger(__name__)

#: PG 系 connection 里库名的候选键。**方言规则**，不进 AlgoConfig ——
#: ODPS 的 project 不在此列，那是 ODPS 自己该知道的事。
PG_DBNAME_KEYS: tuple[str, ...] = ("dbname", "database")
#: 查询日期过滤的缓存语义。谓词语义变化时必须升级，防止复用旧 raw 快照。
QUERY_LOG_WINDOW_SEMANTICS = "half_open_v1"


class PGQueryLogInput(QueryLogInput):
    """Hologres 系统日志表。

    不可用（无库名 / 无执行器）时 ``fetch`` 返回空快照，组合根的分派
    对未认领类型降级 Null —— 日志永远是增强项。
    """

    dialect_keys = ("postgresql", "pg")

    @classmethod
    def fingerprint(cls, config: AlgoConfig) -> dict[str, Any]:
        """PG 原始日志指纹；窗口谓词变更必须触发一次性缓存失效。"""
        fingerprint = super().fingerprint(config)
        fingerprint["query_log_window_semantics"] = QUERY_LOG_WINDOW_SEMANTICS
        return fingerprint

    def dbname(self) -> str | None:
        """显式 ``input_query_log_dbname`` 优先，否则按方言键从 connection 取。

        「哪个键装着库名」是方言知识，因此解析在这里而不在 AlgoConfig。
        """
        if self.config.input_query_log_dbname:
            return self.config.input_query_log_dbname
        for key in PG_DBNAME_KEYS:
            value = self.config.datasource_connection.get(key)
            if value:
                return str(value)
        return None

    def available(self) -> bool:
        # 方言匹配已由注册分派保证（原「type in HOLO|MIRROR」检查随集合
        # 一起消失），只剩真正的可用性判据
        return bool(self.dbname()) and self._execute_sql is not None

    async def fetch(self) -> RawQueryLogSnapshot:
        """普通 PostgreSQL 没有 Hologres 系统日志表时按无日志运行。"""
        if not self.available() or self._execute_sql is None:
            return RawQueryLogSnapshot.build()

        relation = await self._execute_sql(
            "SELECT to_regclass('hologres.hg_query_log')::text AS query_log_relation"
        )
        relation_column = (
            None if relation is None else relation.get("query_log_relation")
        )
        if relation_column is None or relation_column.dropna().empty:
            logger.info(
                "hologres.hg_query_log 不存在，普通 PostgreSQL 按无查询日志运行"
            )
            return RawQueryLogSnapshot.build()

        return await super().fetch()

    def build_sql(self) -> str:
        opt = self.config
        # datname 里的连字符在 PG 侧是下划线；库名可来自 connection
        datname = str(self.dbname() or "").replace("-", "_")
        start, _, end_exclusive = opt.resolved_query_log_window()

        return f"""
            SELECT query_id, query_date, datname, usename, query
            FROM hologres.hg_query_log
            WHERE datname = '{datname}'
              AND query_date >= '{start}' AND query_date < '{end_exclusive}'
              AND command_tag = 'SELECT'
              AND result_rows > 0 AND read_rows > 0
              AND query != 'SELECT 1'
            LIMIT {opt.input_query_log_max_count}
        """

    # -- 调试备选：本地镜像表（原 psycopg2 方言，现已并入 postgresql）------
    # 日志被同步到一张普通 PG 表时，把 build_sql 换成这份（FROM 与时间列
    # 不同，其余处理完全一样）。镜像表名取自 QWENPAW_DATA_QUERY_LOG_MIRROR_TABLE
    # （默认 public.query_log_mirror，见
    # profiling.algorithm.query_context.query_log_mirror_table）：
    #
    #     SELECT instance, datname, command_tag, result_rows, read_rows,
    #            query_date, ds, query
    #     FROM {query_log_mirror_table()}
    #     WHERE datname = '{datname}'
    #       AND command_tag = 'SELECT'
    #       AND ds >= '{start}' AND ds < '{end_exclusive}'
    #       AND table_read IS NOT NULL
    #     LIMIT {opt.input_query_log_max_count}


__all__ = [
    "PG_DBNAME_KEYS",
    "QUERY_LOG_WINDOW_SEMANTICS",
    "PGQueryLogInput",
]

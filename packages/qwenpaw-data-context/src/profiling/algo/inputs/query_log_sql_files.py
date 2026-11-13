"""文件查询日志来源：spider2-lite 的 gold SQL（索引 jsonl + sql 目录）。

离线基准（spider2 的 sqlite / bigquery_json）没有查询日志表，但评测集
自带每个实例的 ground-truth SQL —— 每库 1~3 条，正是「历史上真的有人
这样查过」的最小日志。本来源把它们当作日志喂给 parser：JOIN 边点亮
DOMAIN 的聚类，GROUP BY / 聚合列点亮 CALIBER 的用法证据。

------------------------------------------------------------------------
布局：spider2 索引（唯一支持的布局）
------------------------------------------------------------------------
::

    {index}   spider2-lite.jsonl   每行 {"instance_id": "bq010", "db": "ga360", "question": ...}
    {dir}/{instance_id}.sql        gold SQL，一文件一条

按 ``db == 日志层库名`` 过滤；索引里有、文件不存在的实例（spider 只对
一部分实例发布了 gold）静默跳过。定位信息三处来源，前者优先：

- config：``input_query_log_path`` / ``input_query_log_dir`` / ``input_query_log_dbname``
- connection（方言规则，同 ``PG_DBNAME_KEYS``）：
  ``query_log_index`` / ``query_log_dir`` / ``query_log_db``

后者是生产路径的入口 —— ``AlgoProvider`` 只透传数据源 connection，
注册数据源时将上述三键写入 connection 即可。

------------------------------------------------------------------------
与基类模板的差异
------------------------------------------------------------------------
- 不建执行器、不执行 SQL（``executes_sql = False``，同 Null 的构造保证）；
- ``fetch`` 覆写为读文件，``build_sql`` 不使用；
- 指纹是文件清单（实例 id + mtime + size），不是时间窗 / max_count。

原始快照除必需的 ``query`` 外带 ``instance_id`` / ``question`` 两个额外列：
排查时能一眼看出哪条 SQL 来自哪个问题（契约允许额外列）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from ..config import AlgoConfig
from ..snapshots.query_log import RawQueryLogSnapshot
from .query_log_base import QueryLogInput

logger = logging.getLogger(__name__)

#: connection 里的候选键（方言规则，不进 AlgoConfig 解析）
SQL_FILES_INDEX_KEYS: tuple[str, ...] = ("query_log_index",)
SQL_FILES_DIR_KEYS: tuple[str, ...] = ("query_log_dir",)
SQL_FILES_DB_KEYS: tuple[str, ...] = ("query_log_db",)


def _first(connection: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = connection.get(key)
        if value:
            return str(value)
    return None


class SqlFileQueryLogInput(QueryLogInput):
    """spider2 gold SQL 作为查询日志。零 SQL 执行。"""

    dialect_keys: ClassVar[tuple[str, ...]] = ("sql_files",)
    executes_sql: ClassVar[bool] = False

    def _build_execute_sql(self) -> None:
        return None

    # -- 定位（config 优先，connection 兜底）-------------------------------

    @staticmethod
    def index_path(config: AlgoConfig) -> Path | None:
        value = config.input_query_log_path or _first(
            config.datasource_connection, SQL_FILES_INDEX_KEYS
        )
        return Path(value).expanduser() if value else None

    @staticmethod
    def sql_dir(config: AlgoConfig) -> Path | None:
        value = config.input_query_log_dir or _first(
            config.datasource_connection, SQL_FILES_DIR_KEYS
        )
        return Path(value).expanduser() if value else None

    @staticmethod
    def dbname(config: AlgoConfig) -> str | None:
        return config.input_query_log_dbname or _first(
            config.datasource_connection, SQL_FILES_DB_KEYS
        )

    @classmethod
    def entries(cls, config: AlgoConfig) -> list[tuple[str, str, Path]]:
        """本库的 ``(instance_id, question, sql 文件路径)``，按 instance_id 排序。

        纯函数（只看 config）—— ``fingerprint`` 与 ``fetch`` 共用同一份清单，
        两者不会各算各的。缺路径 / 缺库名 / 索引不存在一律空清单。
        """
        index, directory, db = (
            cls.index_path(config),
            cls.sql_dir(config),
            cls.dbname(config),
        )
        if index is None or directory is None or not db or not index.is_file():
            return []
        out: list[tuple[str, str, Path]] = []
        with index.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if str(record.get("db") or "") != db:
                    continue
                instance_id = str(record.get("instance_id") or "").strip()
                if not instance_id:
                    continue
                path = directory / f"{instance_id}.sql"
                if path.is_file():
                    out.append((instance_id, str(record.get("question") or ""), path))
        return sorted(out, key=lambda item: item[0])

    # -- QueryLogInput 实现 ---------------------------------------------

    def available(self) -> bool:
        return bool(self.entries(self.config))

    def build_sql(self) -> str:  # pragma: no cover - fetch 覆写，不走 SQL 模板
        raise NotImplementedError("SqlFileQueryLogInput 不执行 SQL")

    async def fetch(self) -> RawQueryLogSnapshot:
        entries = self.entries(self.config)
        if not entries:
            logger.info(
                "sql_files: 库 %r 没有 gold SQL（索引 %s），日志为空（正常路径）",
                self.dbname(self.config),
                self.index_path(self.config),
            )
            return RawQueryLogSnapshot.build()
        rows = [
            {
                "query": path.read_text(encoding="utf-8").strip(),
                "instance_id": instance_id,
                "question": question,
            }
            for instance_id, question, path in entries
        ]
        rows = [row for row in rows if row["query"]]
        logger.info(
            "sql_files: 库 %r 读入 %d 条 gold SQL", self.dbname(self.config), len(rows)
        )
        return RawQueryLogSnapshot.build(pd.DataFrame(rows))

    @classmethod
    def fingerprint(cls, config: AlgoConfig) -> dict[str, Any]:
        """文件清单即判据：实例 id + mtime + size。时间窗 / max_count 不适用。"""
        return {
            "datasource_type": config.resolved_query_log_type(),
            "input_query_log_dbname": cls.dbname(config),
            "input_query_log_path": str(cls.index_path(config) or ""),
            "input_query_log_dir": str(cls.sql_dir(config) or ""),
            "files": [
                [instance_id, path.stat().st_mtime_ns, path.stat().st_size]
                for instance_id, _, path in cls.entries(config)
            ],
        }


__all__ = [
    "SQL_FILES_DB_KEYS",
    "SQL_FILES_DIR_KEYS",
    "SQL_FILES_INDEX_KEYS",
    "SqlFileQueryLogInput",
]

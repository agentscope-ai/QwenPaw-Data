"""可执行 SQL 的表层来源：共享机制，方言留白。

并发拉列、并发探枚举、预算控制、分区列识别对所有关系型数据库都一样；
不一样的只有 **SQL 文本与标识符引用规则**。因此这一层实现前者，把后者
留给方言子类的四个抽象方法。

新增一种数据库 = 实现四个方法 + 在 :mod:`.__init__` 注册一行。
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import Sequence

import pandas as pd

from ..snapshots.base import as_str_list
from ..snapshots.table import empty_column_meta
from ..config import AlgoConfig
from .table_input_base import TableInput

logger = logging.getLogger(__name__)

#: 分区列候选：只探最新分区，避免全表扫描。
PARTITION_COLUMNS: tuple[str, ...] = ("dt", "ds", "date_id")


def quote_literal(text: str) -> str:
    """字符串字面量。SQL 标准写法，多数方言通用。"""
    return "'" + str(text).replace("'", "''") + "'"


def quote_literal_list(values: "tuple[str, ...] | list[str]") -> str:
    """拼成 ``'a', 'b'``，供 ``IN (...)`` 用。空集合返回 ``''``（永不命中）。"""
    return ", ".join(quote_literal(v) for v in values) or "''"


class SqlTableInput(TableInput):
    """通过 SQL 执行读取的表层来源。

    执行器在**构造时**从 ``config.load_table_execute_sql()`` 取 —— 装配只在
    :class:`~profiling.algo.config.AlgoConfig` 发生；测试桩经
    ``config.execute_sql_func`` 注入。构造完即绑定，调用面因此只剩
    ``(sql, max_rows)``。
    """

    def __init__(self, config: AlgoConfig) -> None:
        """**只收 config。** 执行器经 ``load_table_execute_sql`` 装配。

        与日志层刻意不对称：**这里构造失败就抛** —— 表层是所有任务的硬
        依赖，静默降级只会让问题在更下游炸开。
        """
        super().__init__(config)
        self._execute_sql = self.config.load_table_execute_sql()

    # -- 方言留白 --------------------------------------------------------

    @abstractmethod
    def sql_tables(self) -> str:
        """表元数据 SQL。至少产出 ``table_name``、``table_comment``。"""

    @abstractmethod
    def sql_columns(self, table_name: str) -> str:
        """单表列元数据 SQL。列名与取值须直接符合契约（``AS is_primary``）。"""

    @abstractmethod
    def sql_distinct(
        self,
        table_name: str,
        column_name: str,
        partition: str | None,
    ) -> str:
        """枚举探查 SQL。首列为取值，行数受 ``enum_threshold`` 限制。"""

    @abstractmethod
    def quote_ident(self, name: str) -> str:
        """标识符引用（PG 用双引号，MySQL 用反引号…）。"""

    def merge_partitioned(self, frame: pd.DataFrame) -> pd.DataFrame:
        """把 ``xxx_20260101`` 这类分区表合并成一张逻辑表。

        默认按 ``_YYYYMMDD`` 后缀归并；不支持分区裁剪的方言可覆写为原样返回。
        """
        if frame is None or len(frame) == 0:
            return frame
        out = frame.copy()
        out["table_name"] = (
            out["table_name"].astype(str).str.replace(r"_\d{6,8}$", "", regex=True)
        )
        return out.drop_duplicates(subset=["table_name"], keep="last").reset_index(
            drop=True
        )

    # -- 共享机制 --------------------------------------------------------

    async def fetch_table_meta(self) -> pd.DataFrame:
        frame = await self.sql(self.sql_tables())
        if self.config.input_table_merge_partitions_enabled:
            frame = self.merge_partitioned(frame)
        return frame

    async def fetch_column_meta(
        self,
        table_names: Sequence[str],
    ) -> dict[str, pd.DataFrame]:
        """并发拉取。单表失败只影响该表，不中断整体。

        **SQL 返回什么就是什么** —— 方言子类的 ``sql_columns`` 负责让列名
        与取值直接符合契约（``AS is_primary`` 而不是拉完再改名）。
        这里只补一个 ``table_name``，因为单表 SQL 里没有它。
        """
        sem = asyncio.Semaphore(max(1, self.config.input_table_enums_workers))

        async def one(name: str) -> tuple[str, pd.DataFrame]:
            async with sem:
                try:
                    raw = await self.sql(self.sql_columns(name))
                except Exception as exc:
                    logger.warning("列结构拉取失败 %s: %s", name, exc)
                    return name, empty_column_meta()
                if raw is None or len(raw) == 0:
                    return name, empty_column_meta()
                out = raw.copy()
                out["table_name"] = str(name)
                return name, out

        pairs = await asyncio.gather(*(one(n) for n in table_names))
        return {name: frame for name, frame in pairs if len(frame)}

    async def probe_enums(
        self,
        column_meta: dict[str, pd.DataFrame],
    ) -> dict[str, pd.DataFrame]:
        """并发探查每列的枚举值。预算用尽即带着已探到的部分返回。

        并发不能省：每列一条 ``SELECT DISTINCT``，宽库上百表 × 几十列是
        分钟级。这正是旧版把 ``add_enums`` 做成后台任务的原因——现在并发
        收进本方法内部，对外仍是「fetch 返回时已完整」。

        也正因为是分钟级，**进度必须可见**：静默几分钟时无从区分「还在跑」
        与「已挂死」。因此按表记完成进度（表间并发，完成顺序不等于声明
        顺序，所以计数器而不是序号），首尾各记一条汇总。
        """
        workers = max(1, self.config.input_table_enums_workers)
        sem = asyncio.Semaphore(workers)
        budget = self.config.input_table_enums_budget_seconds
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + budget if budget else None

        total_tables = len(column_meta)
        total_columns = sum(len(frame) for frame in column_meta.values())
        logger.info(
            "枚举探查开始：%s 表 / %s 列，并发 %s，预算 %s",
            total_tables,
            total_columns,
            workers,
            f"{budget:.0f}s" if budget else "不限",
        )

        done_tables = 0
        overrun_logged = False

        async def one(table: str, frame: pd.DataFrame) -> tuple[str, pd.DataFrame]:
            nonlocal done_tables, overrun_logged
            partition = self._partition_column(frame)
            enums: list[list[str]] = []
            counts: list[int | None] = []
            skipped = 0
            for column_name in frame["column_name"].astype(str):
                if deadline is not None and loop.time() > deadline:
                    # 预算耗尽只报一次：逐列报会在宽库上刷出上千条同样的行
                    if not overrun_logged:
                        overrun_logged = True
                        logger.warning(
                            "枚举探查预算 %.0fs 已用尽，剩余列留空返回", budget
                        )
                    enums.append([])
                    counts.append(None)
                    skipped += 1
                    continue
                async with sem:
                    values, count = await self._probe_one(table, column_name, partition)
                enums.append(values)
                counts.append(count)
            out = frame.copy()
            out["column_enums"] = enums
            out["column_distinct_num"] = counts
            done_tables += 1
            logger.info(
                "枚举探查 %s/%s %s：%s 列已探，%s 列跳过，累计 %.0fs",
                done_tables,
                total_tables,
                table,
                len(enums) - skipped,
                skipped,
                loop.time() - started,
            )
            return table, out

        results = await asyncio.gather(
            *(one(t, f) for t, f in column_meta.items()), return_exceptions=True
        )
        merged = dict(column_meta)
        failed = 0
        for item in results:
            if isinstance(item, BaseException):
                logger.warning("枚举探查异常: %s", item)
                failed += 1
                continue
            table, frame = item
            merged[table] = frame
        logger.info(
            "枚举探查结束：%s/%s 表成功，耗时 %.0fs",
            total_tables - failed,
            total_tables,
            loop.time() - started,
        )
        return merged

    async def _probe_one(
        self,
        table: str,
        column: str,
        partition: str | None,
    ) -> tuple[list[str], int | None]:
        try:
            frame = await self.sql(self.sql_distinct(table, column, partition))
        except Exception as exc:
            logger.debug("枚举探查失败 %s.%s: %s", table, column, exc)
            return [], None
        if frame is None or len(frame) == 0:
            return [], 0
        # 走规范化函数：去重、丢 nan、截断都只有这一处定义
        values = as_str_list(frame.iloc[:, 0].tolist())
        # 触到 LIMIT 说明还有更多，distinct 数不可信 → None
        overflow = len(frame) >= self.config.input_table_enums_max_distinct
        return values, (None if overflow else len(values))

    @staticmethod
    def _partition_column(frame: pd.DataFrame) -> str | None:
        lowered = {str(c).lower() for c in frame["column_name"]}
        return next((c for c in PARTITION_COLUMNS if c in lowered), None)

    async def sql(self, statement: str) -> pd.DataFrame:
        # table_max_rows is None → 不限制，仍把 max_rows=None 传给执行器。
        # 执行器构造时已绑定数据源，这里不再需要任何分派信息。
        return await self._execute_sql(
            statement, max_rows=self.config.input_table_max_count
        )


__all__ = [
    "PARTITION_COLUMNS",
    "SqlTableInput",
    "quote_literal",
    "quote_literal_list",
]

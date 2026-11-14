"""解析管线：原始日志快照 → 四类证据快照。**parser 包的顶层入口。**

它是 ``QueryLogContext`` 的 fetch 的主体，但 context 按设计不认识它
（context 只拿「一个能产出 snapshot 的协程」，见 ``contexts/base.py``）
—— 类壳 :class:`ParsedQueryLogInput` 与表层/日志层的来源类同构
（``create`` / ``fingerprint``），组合根只注入上游与进度钩子。

本文件是包内唯一坐在「全部 parser 实现之上」的模块 —— ``base`` /
``sql_analysis`` 被全部实现反向依赖，做不到这一点；而包内没有模块
反向 import 本文件，因此不成环。

新增一种证据 = 写 parser + 在 ``default_parsers`` 里加一行。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Sequence

import pandas as pd
import sqlglot

from ..config import AlgoConfig
from ..inputs import QueryLogInput
from ..snapshots.query_log import (
    REQUIRED_FAILED,
    QueryLogSnapshot,
    RawQueryLogSnapshot,
)
from .base import PARSER_VERSION, EvidenceParser
from .dimension_usage import DimensionUsageParser
from .join import JoinParser
from .metric_expr import MetricExprParser
from .sql_analysis import SqlAnalysis
from .sql_template import DATASET_MIN_SELECT_COLUMNS, SqlTemplateParser

logger = logging.getLogger(__name__)


#: sqlglot 报错带 ANSI 高亮与控制符，直写 xlsx 会触发 IllegalCharacterError。
#: 失败清单是给人看的文本，出口即清洗。
_CONTROL_CHARS = re.compile(r"\x1b\[[0-9;]*m|[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _error_text(exc: Exception) -> str:
    return _CONTROL_CHARS.sub("", f"{type(exc).__name__}: {exc}")


def default_parsers(
    *,
    template_min_count: int = 10,
    template_min_columns: int = DATASET_MIN_SELECT_COLUMNS,
) -> tuple[EvidenceParser, ...]:
    """四个 parser 各一个。顺序无关——它们互不感知。"""
    return (
        JoinParser(),
        SqlTemplateParser(
            min_count=template_min_count, min_columns=template_min_columns
        ),
        DimensionUsageParser(),
        MetricExprParser(),
    )


async def parse_evidence(
    raw: RawQueryLogSnapshot,
    *,
    parsers: Sequence[EvidenceParser] | None = None,
    dialect: str = "postgres",
    template_min_count: int = 10,
    template_min_columns: int = DATASET_MIN_SELECT_COLUMNS,
    on_progress=None,
) -> QueryLogSnapshot:
    """解析 + 单趟遍历 → 产出四类证据。**AST 不出本函数。**

    CPU 密集，整体下线程——不写「``async def`` 里不 await」的假异步。
    """
    statements = raw.statements()
    if not statements:
        logger.info("原始日志为空，证据为空（正常路径，日志是增强项）")
        return QueryLogSnapshot.build()

    active = tuple(
        parsers
        or default_parsers(
            template_min_count=template_min_count,
            template_min_columns=template_min_columns,
        )
    )
    if not active:
        raise ValueError("未注册任何日志 parser —— 这是装配错误，不是运行时降级")

    return await asyncio.to_thread(
        _parse_sync, statements, active, dialect, on_progress
    )


def _parse_sync(
    statements: Sequence[str],
    parsers: Sequence[EvidenceParser],
    dialect: str,
    on_progress,
) -> QueryLogSnapshot:
    failed: list[tuple[str, str]] = []
    parsed = 0

    total = len(statements)
    step = max(1, total // 20)
    for index, sql in enumerate(statements, start=1):
        try:
            analysis = SqlAnalysis.create_from_sql(sql, dialect=dialect)
        except Exception as exc:
            failed.append((sql, _error_text(exc)))
        else:
            parsed += 1
            for parser in parsers:
                try:
                    parser.visit(analysis)
                except Exception as exc:
                    # 单条失败只影响自己，不拖垮整体
                    logger.debug("%s 抽取失败: %s", parser.kind, exc)
        if on_progress is not None and index % step == 0:
            on_progress(int(index / total * 100), f"解析 {index}/{total}")

    logger.info(
        "解析完成 %s/%s，失败 %s（失败清单落 failed_statements）",
        parsed,
        total,
        len(failed),
    )

    evidence = {parser.kind: parser.result() for parser in parsers}
    failed_frame = (
        pd.DataFrame(failed, columns=list(REQUIRED_FAILED))
        if failed
        else pd.DataFrame({name: pd.Series(dtype="object") for name in REQUIRED_FAILED})
    )
    return QueryLogSnapshot.build(evidence, failed_frame)


class ParsedQueryLogInput:
    """解析后证据的来源：读上游原始日志快照 → :func:`parse_evidence`。

    **派生来源** —— 输入不是外部世界而是上游 context，因此 ``create``
    除 config 外还要注入 ``load_raw``（上游的 ``load``）与进度钩子。
    与表层/日志层 ``create(config)`` 的签名差异是「派生 vs 外部」这个
    架构差异的诚实表达。

    也因此**没有 offline 闸**：解析是纯本地计算（上游读的是本地缓存），
    offline 只承诺「不发远程查询」，禁掉解析反而毁了指纹漂移时的本地
    重解析自愈（离线回放靠它）。
    """

    def __init__(
        self,
        config: AlgoConfig,
        *,
        load_raw: Callable[[], Awaitable[RawQueryLogSnapshot]],
        on_progress: Callable[[int, str], Any] | None = None,
    ) -> None:
        self.config = config
        self._load_raw = load_raw
        self._on_progress = on_progress

    @classmethod
    def create(
        cls,
        config: AlgoConfig,
        *,
        load_raw: Callable[[], Awaitable[RawQueryLogSnapshot]],
        on_progress: Callable[[int, str], Any] | None = None,
    ) -> "ParsedQueryLogInput":
        """构造。**无分派** —— 解析只有一条管线，没有方言注册表；
        保留 ``create`` 名纯为组合根三个来源块同构。
        """
        return cls(config, load_raw=load_raw, on_progress=on_progress)

    @classmethod
    def fingerprint(cls, config: AlgoConfig) -> dict[str, Any]:
        """本地缓存的**完整**失效判据：上游原始日志的指纹 + 解析这一步的。

        上游部分直接调 ``QueryLogInput.fingerprint(config)`` —— 它是
        config 的纯函数，而「本证据的上游是原始日志」是本包声明的架构
        事实（见包文档），绑定收在类里，组合根不再拼装。

        **必须含 sqlglot 版本与 PARSER_VERSION** —— 旧版漏了，升级
        sqlglot 或改完解析代码后仍会命中陈旧缓存。改解析行为时，这里与
        ``PARSER_VERSION`` 一起动。config 派生的键与 config 字段名一致。
        """
        return {
            "raw": QueryLogInput.fingerprint(config),
            "sqlglot": getattr(sqlglot, "__version__", "unknown"),
            "parser": PARSER_VERSION,
            "parser_dialect": config.parser_dialect,
            "parser_template_min_count": config.parser_template_min_count,
            "parser_template_min_columns": config.parser_template_min_columns,
        }

    async def fetch(self) -> QueryLogSnapshot:
        raw = await self._load_raw()
        return await parse_evidence(
            raw,
            dialect=self.config.parser_dialect,
            template_min_count=self.config.parser_template_min_count,
            template_min_columns=self.config.parser_template_min_columns,
            on_progress=self._on_progress,
        )


__all__ = [
    "ParsedQueryLogInput",
    "default_parsers",
    "parse_evidence",
]

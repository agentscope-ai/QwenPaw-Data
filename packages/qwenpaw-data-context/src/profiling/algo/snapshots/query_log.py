"""查询日志的两种形态：原始文本与解析后的证据。

------------------------------------------------------------------------
为什么是两个快照而不是一个
------------------------------------------------------------------------
两者的**失效条件完全不同**::

    RawQueryLogSnapshot   dbname / 时间窗 / max_rows
    QueryLogSnapshot      raw 指纹 + sqlglot 版本 + PARSER_VERSION

合成一个的话，采集逻辑一改（采集器是会经常改的东西）就要重新拉 10 万条
日志——而原始日志根本没变。拆开之后各自失效，改采集器只重跑 parse。

解析发生在 ``QueryLogContext`` 的 fetch 里（读上游 context + 转换），
**不是一个 task** —— 它没有独立证据需求、没有 LLM、没有用户审核阶段，
塞进 TASK_ORDER 会在编排层的 stage 映射里多出一个没有归宿的成员。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import pandas as pd

from .base import empty_frame, ensure_required

# ===========================================================================
# 原始日志
# ===========================================================================

RAW_LOGS_KEY = "raw_logs"

#: 原始日志只需要一列。query_id / query_date / usename 等额外列保留，排查用。
REQUIRED_RAW_LOGS: tuple[str, ...] = ("query",)


@dataclass(frozen=True)
class RawQueryLogSnapshot:
    """从远程库拉回的 SQL 文本，未解析。

    单列 DataFrame 而非 ``Sequence[str]``：额外列在排查采集问题时有用，
    且 store 只认 DataFrame。
    """

    logs: pd.DataFrame = field(default_factory=lambda: empty_frame(REQUIRED_RAW_LOGS))

    def to_frames(self) -> Mapping[str, pd.DataFrame]:
        return {RAW_LOGS_KEY: self.logs}

    @classmethod
    def build(cls, logs: pd.DataFrame | None = None) -> RawQueryLogSnapshot:
        """只补齐必需列并 warning；额外列保留。核验只在 snapshot 内发生。"""
        return cls(
            logs=ensure_required(logs, REQUIRED_RAW_LOGS, label="原始日志"),
        )

    @classmethod
    def from_frames(cls, frames: Mapping[str, pd.DataFrame]) -> RawQueryLogSnapshot:
        return cls.build(frames.get(RAW_LOGS_KEY))

    @classmethod
    def from_statements(cls, statements) -> RawQueryLogSnapshot:
        cleaned = [str(s).strip() for s in (statements or ()) if str(s).strip()]
        if not cleaned:
            return cls.build()
        return cls.build(pd.DataFrame({"query": cleaned}))

    def is_empty(self) -> bool:
        return self.logs is None or len(self.logs) == 0

    def statements(self) -> tuple[str, ...]:
        """非空 SQL 文本。采集器的输入。"""
        if self.is_empty() or "query" not in self.logs.columns:
            return ()
        series = self.logs["query"].dropna().astype(str)
        return tuple(text.strip() for text in series if text.strip())


# ===========================================================================
# 解析后的证据
# ===========================================================================

#: 四类证据。与 task 无关，只与「从 SQL 里能看出什么」有关。
EVIDENCE_KINDS: tuple[str, ...] = (
    "join",  # → DOMAIN
    "sql_template",  # → DATASET
    "dimension_usage",  # → CALIBER / DICT
    "metric_expr",  # → CALIBER / DICT
)

REQUIRED_EVIDENCE: Mapping[str, tuple[str, ...]] = {
    "join": ("left_table", "right_table", "join_type", "on_columns", "count"),
    "sql_template": ("parents", "repeated_sql", "count"),
    "dimension_usage": ("table_name", "column_name", "count", "sources"),
    "metric_expr": (
        "table_name",
        "metric_name",
        "formula",
        "count",
        "metric_type",
        "sources",
        "agg_function",
        "agg_column",
    ),
}

EVIDENCE_PREFIX = "evidence__"

#: 解析失败的 SQL。通常只有几十条，是排查采集问题时真正要看的东西。
FAILED_KEY = "failed_statements"
REQUIRED_FAILED: tuple[str, ...] = ("query", "error")


@dataclass(frozen=True)
class QueryLogSnapshot:
    """四类原始证据。**AST 不在这里** —— 缓存成果，不缓存中间态。

    旧版把 sqlglot AST pickle 落盘：又贵又重，还会因解析器升级静默语义漂移，
    而指纹里根本没算解析器版本。
    """

    evidence: Mapping[str, pd.DataFrame] = field(default_factory=dict)
    failed: pd.DataFrame = field(default_factory=lambda: empty_frame(REQUIRED_FAILED))

    def to_frames(self) -> Mapping[str, pd.DataFrame]:
        frames = {f"{EVIDENCE_PREFIX}{kind}": self.of(kind) for kind in EVIDENCE_KINDS}
        frames[FAILED_KEY] = self.failed
        return frames

    @classmethod
    def from_frames(cls, frames: Mapping[str, pd.DataFrame]) -> QueryLogSnapshot:
        evidence = {
            kind: ensure_required(
                frames.get(f"{EVIDENCE_PREFIX}{kind}"),
                REQUIRED_EVIDENCE[kind],
                label=f"日志证据[{kind}]",
            )
            for kind in EVIDENCE_KINDS
        }
        return cls(
            evidence=evidence,
            failed=ensure_required(
                frames.get(FAILED_KEY), REQUIRED_FAILED, label="解析失败"
            ),
        )

    @classmethod
    def build(
        cls,
        evidence: Mapping[str, pd.DataFrame] | None = None,
        failed: pd.DataFrame | None = None,
    ) -> QueryLogSnapshot:
        return cls(
            evidence={
                kind: ensure_required(
                    (evidence or {}).get(kind),
                    REQUIRED_EVIDENCE[kind],
                    label=f"日志证据[{kind}]",
                )
                for kind in EVIDENCE_KINDS
            },
            failed=ensure_required(failed, REQUIRED_FAILED, label="解析失败"),
        )

    def is_empty(self) -> bool:
        """四类全空即为空。**空是正常路径** —— 日志永远是增强项。"""
        return all(len(self.of(kind)) == 0 for kind in EVIDENCE_KINDS)

    def of(self, kind: str) -> pd.DataFrame:
        """取一类证据；没有则返回带列空表，下游不必判空。"""
        frame = self.evidence.get(kind)
        if frame is not None:
            return frame
        return empty_frame(REQUIRED_EVIDENCE.get(kind, ()))


__all__ = [
    "EVIDENCE_KINDS",
    "EVIDENCE_PREFIX",
    "FAILED_KEY",
    "RAW_LOGS_KEY",
    "REQUIRED_EVIDENCE",
    "REQUIRED_FAILED",
    "REQUIRED_RAW_LOGS",
    "QueryLogSnapshot",
    "RawQueryLogSnapshot",
]

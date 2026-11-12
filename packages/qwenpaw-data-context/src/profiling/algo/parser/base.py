"""parser 协议与共用的形状胶水。

------------------------------------------------------------------------
解析一次，单趟多 parser
------------------------------------------------------------------------
AST 与作用域索引是最贵也最笨重的中间态。主循环逐条构建
:class:`~.sql_analysis.SqlAnalysis`，四个 parser 各 ``visit`` 一次后 AST
立刻可回收——不缓存中间态，只缓存证据 DataFrame。

**parser 之间互不感知。** 某个 parser **内部**可以有顺序（复合表达式先认领
agg 节点、标量聚合再跳过被认领的），但 parser **之间**不得有依赖——
否则单趟遍历会退化成隐式流水线。

------------------------------------------------------------------------
表标识统一（方案 B）
------------------------------------------------------------------------
原版四类证据的表名口径不一致（join 剥 schema 不剥分区、parents 带
schema、dim/metric 是带 schema 的逗号多表引用），靠旧 handler 在下游兜底。
本架构没有兜底层，snapshot 的表标识必须与 ``source_table.names()``
（短名、分区已合并）同口径才可对齐——因此各 parser 在**出口的表标识字段**
统一走 :func:`short_table_ref`：剥 schema、剥分区后缀 ``_\\d{6,8}``；
多表逗号引用逐段归一后排序去重；``"_"``（无法归属）语义保留。
算法判定本身逐行忠实原版，SQL 文本字段（formula / repeated_sql）不动。

归一是 **dialect 感知**的：PG / sqlite 的分区表约定是「剥后缀回基表名」
（``orders_20260101 → orders``，与 PG 表来源的分区归并同口径）；BigQuery
的分片族约定是「保留通配名」（``ga_sessions_20170801`` /
``ga_sessions_201707*`` → ``ga_sessions_*``，与 ``merge_shard_families``
同口径）。两种约定在同一函数里按 dialect 分流，各 parser 从
``SqlAnalysis.dialect_name`` 取 dialect 传入 —— 表来源与日志证据的表
标识因此在两种方言下都能对齐。
"""

from __future__ import annotations

import re
from typing import Any, Protocol, Sequence, runtime_checkable

import pandas as pd

from .sql_analysis import SqlAnalysis

#: 解析/抽取逻辑版本。**改任一抽取规则就 +1** —— 它参与日志证据的缓存指纹，
#: 不加就会在改完代码后仍然命中陈旧缓存。
PARSER_VERSION = 2  # 2：表标识归一 dialect 感知（BigQuery 分片族折成通配名）

#: 分区后缀：``orders_20260101`` 与 ``orders`` 视为同一张逻辑表。
_PARTITION_SUFFIX = re.compile(r"_\d{6,8}$")

#: BigQuery 分片形态：显式分片 ``_20170801``、SQL 侧通配前缀 ``_201707*``、
#: 已是族名的 ``_*`` —— 三者都折成 ``_*``。无下划线的裸通配（``1998*``）
#: 与 JSON 侧归并规则对不上，原样保留。
_BQ_SHARD_SUFFIX = re.compile(r"_(?:\d{6,8}|\d{0,8}\*)$")

#: 保留通配族名的方言（其余方言剥后缀回基表名）
_WILDCARD_DIALECTS: frozenset[str] = frozenset({"bigquery"})


@runtime_checkable
class EvidenceParser(Protocol):
    """单趟遍历中的一个访问者。"""

    @property
    def kind(self) -> str:
        """产出哪一类证据，见 ``snapshots.query_log.EVIDENCE_KINDS``。"""

    def visit(self, analysis: SqlAnalysis) -> None:
        """吸收一条已解析的 SQL，内部累积（含 per-SQL 去重）。"""

    def result(self) -> pd.DataFrame:
        """遍历结束后收敛：表标识归一、分组、计数等全局收尾。"""


# ===========================================================================
# 表标识统一（方案 B 的唯一实现点）
# ===========================================================================


def short_table_name(name: str, dialect: str = "postgres") -> str:
    """单个表名 → 短标识：去 schema（保最后一段），再按方言收敛分区/分片。

    - 默认（PG / sqlite）：剥分区后缀回基表名；
    - ``bigquery``：分片形态折成通配族名 ``_*``（见 ``_BQ_SHARD_SUFFIX``）。
    """
    short = str(name or "").strip().rsplit(".", 1)[-1]
    if str(dialect or "").strip().lower() in _WILDCARD_DIALECTS:
        # 数据集级通配 ``project.dataset.*``（整库扫）指不到任何一张表 ——
        # 归为「无法归属」（空串；多表引用侧折成 "_"），不冒充表名
        if short == "*":
            return ""
        return _BQ_SHARD_SUFFIX.sub("_*", short)
    return _PARTITION_SUFFIX.sub("", short)


def short_table_ref(ref: str, dialect: str = "postgres") -> str:
    """profile 表引用 → 短标识。

    兼容三种形态：单表 ``"dwd.orders"``、多表 ``"a,b"``（逐段归一后排序
    去重）、无法归属 ``"_"``（原样保留，由各 parser 自行决定丢弃与否）。
    """
    text = str(ref or "").strip()
    if not text or text == "_":
        return text or "_"
    parts = [short_table_name(part, dialect) for part in text.split(",")]
    parts = [part for part in parts if part]
    return ",".join(sorted(set(parts))) or "_"


# ===========================================================================
# 形状胶水
# ===========================================================================


def rows_to_frame(rows: list[dict[str, Any]], columns: Sequence[str]) -> pd.DataFrame:
    """统一的空表约定：**带列的空表**，否则下游 merge 会炸。"""
    if not rows:
        return pd.DataFrame({name: pd.Series(dtype="object") for name in columns})
    frame = pd.DataFrame(rows)
    for name in columns:
        if name not in frame.columns:
            frame[name] = None
    return frame[list(columns)]


__all__ = [
    "PARSER_VERSION",
    "EvidenceParser",
    "rows_to_frame",
    "short_table_name",
    "short_table_ref",
]

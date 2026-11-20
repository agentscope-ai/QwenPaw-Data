"""probe 事实来源：协议 + 由**原始行**构造的实现。

**协议在算法层，取数在编排层。** 编排层查库（它才知道租户/归属/表名），
把结果按 :data:`FACT_KEYS` 打成一个 ``{键: 行列表}`` 交过来；本模块把行
整形成四类快照的两态。算法层因此不查库，也碰不到 ``tenant_id`` /
``workspace_id`` —— ``profiling/provider.py`` 声明的边界得以保持。

------------------------------------------------------------------------
交进来的是原始行，不是快照
------------------------------------------------------------------------
编排层只负责「查出来」，不负责「整成算法要的形状」：行是普通的
``Mapping[str, 简单类型]``（SQLAlchemy 的 RowMapping 直接可用）。整形规则
属于算法侧的产出契约，随 ``snapshots/`` 一起演进，不该散到编排层去。

------------------------------------------------------------------------
四类产出，各带两态
------------------------------------------------------------------------
``domain`` / ``dataset`` / ``columns_caliber`` / ``columns_dict`` ——
``facts_of`` 的键与 context 的 ``name`` 前缀逐字一致，装配因此是机械的
（一个键控方法，装配层不做 getattr 反射）。

每个键对应一个 :class:`FactPair`：``staged`` 来自 staging 表（算法产出 +
人在编辑），``confirmed`` 来自正式表（已 promote 的 truth）。

------------------------------------------------------------------------
caliber 的列映射：写出去那条路的逆（v2.1）
------------------------------------------------------------------------
``CaliberSnapshot`` 要 ``column_name``，而两张口径表都没有这一列。
不需要发明映射 —— 照 ``algo_provider.map_*`` 写出去的那条路反着读::

    写：column_name → expr        (expr or column_name，两侧同规则)
    读：expr → column_name        维度侧与指标侧同

当 ``expr`` 是复合表达式（``SUM(x)``）时，它同时是口径也是 column_name 的
最好近似 —— 那正是写入时丢失的信息，读回来补不出更多。

------------------------------------------------------------------------
staging 的行属性随快照带走
------------------------------------------------------------------------
``probe_status``（PENDING / CONFIRMED / SKIPPED）与 ``source_type``
（ALGO / HUMAN）作为**额外列**挂在 staged 快照上，不进 ``REQUIRED_*``——
契约是「必须有」不是「只能有」（CLAUDE.md 规则 3），额外列一律保留。
写成必需列会逼四个 handler 也产出它们，每次运行刷一屏 warning。

有了这两列，handler 才分得清「这行人写的别覆盖」「这行被否决过别再提」。
"""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import (
    Any,
    Protocol,
    runtime_checkable,
)

import pandas as pd

from ..snapshots import CaliberSnapshot, DictSnapshot, DomainSnapshot, TableSnapshot
from ..snapshots.caliber import REQUIRED_CALIBERS
from ..snapshots.dictionary import REQUIRED_DIMENSIONS, REQUIRED_METRICS
from ..snapshots.domain import REQUIRED_DOMAINS, REQUIRED_MEMBERS
from ..snapshots.table import REQUIRED_COLUMN_META, REQUIRED_TABLE_META

logger = logging.getLogger(__name__)

Rows = Sequence[Mapping[str, Any]]

#: 编排层要交进来的键。**这就是两层之间的全部约定** —— 少给的键按空处理，
#: 因此离线跑批传空 dict 即可（四类事实全空，context 退化成纯本地）。
FACT_KEYS: tuple[str, ...] = (
    # confirmed 侧：正式表
    "domain",
    "dataset",
    "dataset_fields",
    "dimension_caliber",
    "metric_caliber",
    "dimension",
    "metric",
    # staged 侧：staging 表
    "staged_domain",
    "staged_dataset",
    "staged_dataset_fields",
    "staged_dimension_caliber",
    "staged_metric_caliber",
    "staged_dimension",
    "staged_metric",
)

#: staging 行属性 —— 作为额外列随 staged 快照带走
_ROW_ATTRS = ("probe_status", "source_type")


@dataclass(frozen=True)
class FactPair:
    """一种产出的两态。两边都可能为空快照 —— 该层还没有对应事实。

    行级状态（PENDING / CONFIRMED / SKIPPED）与来源（ALGO / HUMAN）是
    ``staged`` 快照**里的列**，不在这里再分一层：它们同住一张 staging 表，
    拆开只会让同一个逻辑实体的行散落在多份快照里。
    """

    #: staging 表内容。可被下一轮覆盖，改动经 EmitStore 回传核验
    staged: Any = None
    #: 正式表内容。算法只读消费
    confirmed: Any = None


@runtime_checkable
class ProbeFactsInput(Protocol):
    """已确认事实与草稿的读取器。

    用 Protocol 而非 ABC：实现方可能在另一个包里，不应被迫继承算法层的类。
    接口收敛为两个方法 —— 曾经的四个命名 getter（``domain()`` 等）合成
    键控的 ``facts_of``：实现方少写三个方法，装配层不再做 getattr 反射。
    """

    async def fetch(self) -> None:
        """备好四类事实。**幂等**，重复调用直接返回。"""

    def facts_of(self, key: str, lifecycle: str) -> Callable[[], Awaitable[Any]] | None:
        """预绑定「哪个产出、哪一侧」的读取器；键不认识返回 None。"""
        ...


# ===========================================================================
# 取值工具
# ===========================================================================


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_present(*values: Any) -> Any | None:
    """取首个非空标量；与 ``or`` 不同，NaN 不会挡住后续 fallback。"""
    for value in values:
        if value is None:
            continue
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _split(value: Any) -> list[str]:
    """多值串 → list[str]。快照里的多值字段一律是 list。

    v2.1 存储面多值统一 ``$$$``（W1 契约，见 weave_assembler）；
    不含 ``$$$`` 的历史串退回逗号拆分。"""
    if isinstance(value, (list, tuple, set)):
        return [t for t in (_text(v) for v in value) if t]
    text = _text(value)
    if not text:
        return []
    sep = "$$$" if "$$$" in text else ","
    return [t for t in (p.strip() for p in text.split(sep)) if t]


def _split_synonyms(value: Any) -> list[str]:
    """列表串先解析，避免进入 snapshot 前被拆成带括号的错误别名。

    非法列表忽略别名；普通字符串仍兼容 ``$$$`` 和历史逗号分隔。
    已是列表的别名原样交给 _split，不删除名称本身的括号。
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, (list, tuple, set)):
                return _split(parsed)
            logger.warning("Invalid synonyms list; aliases ignored")
            return []
    return _split(value)


def _frame(rows: Rows, columns: Sequence[str]) -> pd.DataFrame:
    """**带列的空表**，否则下游 merge 会炸（与 parser.rows_to_frame 同约定）。"""
    if isinstance(rows, pd.DataFrame):
        rows = rows.to_dict(orient="records")
    if len(rows) == 0:
        return pd.DataFrame({name: pd.Series(dtype="object") for name in columns})
    frame = pd.DataFrame([dict(r) for r in rows])
    for name in columns:
        if name not in frame.columns:
            frame[name] = None
    ordered = list(columns) + [c for c in frame.columns if c not in columns]
    return frame[ordered]


def _attrs(row: Mapping[str, Any], keep: bool) -> dict[str, Any]:
    return {key: row.get(key) for key in _ROW_ATTRS} if keep else {}


def _member_tables(raw: Any) -> list[str]:
    """``staging_biz_domain.member_tables`` 是 CandidateMemberTable 的 JSON 串。"""
    text = _text(raw)
    if not text:
        return []
    try:
        items = json.loads(text)
    except (TypeError, ValueError):
        return _split(text)
    names: list[str] = []
    for item in items if isinstance(items, list) else []:
        name = item.get("source_table") if isinstance(item, dict) else item
        if _text(name):
            names.append(str(name).strip())
    return names


# ===========================================================================
# 行 → 快照
# ===========================================================================


def _domain_snapshot(domains: Rows, members: Rows, *, staged: bool) -> DomainSnapshot:
    return DomainSnapshot.build(
        _frame(
            [
                {
                    "domain_id": r.get("id"),
                    "domain_name": r.get("domain_name"),
                    "display_name": r.get("display_name"),
                    "description": r.get("description"),
                    **_attrs(r, staged),
                }
                for r in domains
            ],
            REQUIRED_DOMAINS,
        ),
        _frame(members, REQUIRED_MEMBERS),
    )


def _dataset_snapshot(datasets: Rows, fields: Rows, *, staged: bool) -> TableSnapshot:
    """v2.1：列即 L3 dataset_field。业务描述充当 column_comment（给 LLM 的
    语义入口同位）；样例值 ``sample_values`` 由编排层从 P3 join 出（表式来源），
    映射回快照的 ``column_enums`` 额外列。``source`` 原样携带 —— 表式来源时
    它就是物理表两段全名，与写出去的 parents 语义对齐。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in fields:
        table_name = _text(row.get("dataset_name"))
        if not table_name:
            continue
        grouped.setdefault(table_name, []).append(
            {
                "table_name": table_name,
                "column_name": row.get("field_name"),
                "data_type": row.get("data_type"),
                "is_primary": row.get("is_primary"),
                "is_nullable": row.get("is_nullable"),
                "column_comment": row.get("description"),
                "column_enums": _split(row.get("sample_values")),
                "column_distinct_num": None,
            }
        )

    def _source_fields(r: Mapping[str, Any]) -> dict[str, Any]:
        source = _text(r.get("source"))
        is_sql = bool(source) and source.lstrip().upper().startswith(("SELECT", "WITH"))
        return {
            "source": source,
            # 算法内部血缘机械仍认 parents / sql_content —— 从 source 反推
            "sql_content": source if is_sql else None,
            "parents": None if is_sql else source,
        }

    return TableSnapshot.build(
        _frame(
            [
                {
                    "table_name": r.get("dataset_name"),
                    "table_comment": r.get("description"),
                    "domain_name": r.get("domain_name"),
                    **_source_fields(r),
                    **_attrs(r, staged),
                }
                for r in datasets
            ],
            REQUIRED_TABLE_META,
        ),
        {k: _frame(v, REQUIRED_COLUMN_META) for k, v in grouped.items()},
    )


def _caliber_snapshot(
    dim_rows: Rows, metric_rows: Rows, *, staged: bool
) -> CaliberSnapshot:
    """维度侧 + 指标侧合成一张 calibers，靠 ``column_role`` 区分。

    v2.1：两侧的回读锚点统一是 ``expr``（见模块 docstring）。"""
    rows: list[dict[str, Any]] = [
        {
            "table_name": r.get("dataset_name"),
            # 物理列身份优先取血缘列；老行没有时退回 expr（写出去那条路的逆）
            "column_name": r.get("column_name") or r.get("expr"),
            "domain_name": r.get("domain_name"),
            # 内部血缘列还原：date 身份丢了 DICT 会把时间维度全判 categorical
            "column_role": r.get("column_role") or "dimension",
            "dimension_name": r.get("dimension_name"),
            "value_example": _split(r.get("value_example")),
            "metric_name": None,
            "time_window": None,
            "expr": r.get("expr"),
            "additivity": None,
            "role_reason": None,
            "role_confidence": None,
            **_attrs(r, staged),
        }
        for r in dim_rows
    ]
    rows += [
        {
            "table_name": r.get("dataset_name"),
            # 物理列身份优先取血缘列 —— 退回 expr 会让 DICT 把 SUM(...) 当别名
            "column_name": r.get("column_name") or r.get("expr"),
            "domain_name": r.get("domain_name"),
            "column_role": "metric",
            "dimension_name": None,
            "value_example": None,
            "metric_name": r.get("metric_name"),
            "time_window": r.get("time_window"),
            "expr": r.get("expr"),
            "additivity": r.get("additivity"),
            "role_reason": None,
            "role_confidence": None,
            **_attrs(r, staged),
        }
        for r in metric_rows
    ]
    return CaliberSnapshot.build(_frame(rows, REQUIRED_CALIBERS))


def _dict_snapshot(
    dim_rows: Rows,
    metric_rows: Rows,
    dim_calibers: Rows,
    metric_calibers: Rows,
    *,
    staged: bool,
) -> DictSnapshot:
    """维度库 + 指标库。

    ``tables`` / ``formulas`` / ``time_windows`` 不在维度表/指标表上 ——
    它们属于口径层，这里按名字从同一批口径行聚合，不额外取数。
    """

    def _collect(rows: Rows, name_key: str, value_key: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in rows:
            name, value = _text(r.get(name_key)), _text(r.get(value_key))
            if name and value and value not in out.setdefault(name, []):
                out[name].append(value)
        return out

    dim_tables = _collect(dim_calibers, "dimension_name", "dataset_name")
    metric_tables = _collect(metric_calibers, "metric_name", "dataset_name")
    metric_formulas = _collect(metric_calibers, "metric_name", "expr")
    metric_windows = _collect(metric_calibers, "metric_name", "time_window")

    return DictSnapshot.build(
        _frame(
            [
                {
                    "domain_name": r.get("domain_name"),
                    "dimension_name": r.get("dimension_name"),
                    "description": r.get("description"),
                    "synonyms": _split_synonyms(r.get("synonyms")),
                    # staging/正式字典统一使用 dimension_type；算法快照保留
                    # dimension_role。旧事实输入只作兼容回退，不优先于新字段。
                    "dimension_role": _first_present(
                        r.get("dimension_type"), r.get("dimension_role")
                    ),
                    "tables": dim_tables.get(_text(r.get("dimension_name")) or "", []),
                    **_attrs(r, staged),
                }
                for r in dim_rows
            ],
            REQUIRED_DIMENSIONS,
        ),
        _frame(
            [
                {
                    "domain_name": r.get("domain_name"),
                    "metric_name": r.get("metric_name"),
                    "description": r.get("description"),
                    "synonyms": _split_synonyms(r.get("synonyms")),
                    "formulas": metric_formulas.get(
                        _text(r.get("metric_name")) or "", []
                    ),
                    "time_windows": metric_windows.get(
                        _text(r.get("metric_name")) or "", []
                    ),
                    "tables": metric_tables.get(_text(r.get("metric_name")) or "", []),
                    **_attrs(r, staged),
                }
                for r in metric_rows
            ],
            REQUIRED_METRICS,
        ),
    )


# ===========================================================================
# 实现
# ===========================================================================


class StaticProbeFactsInput:
    """由**已取好的原始行**构造四类事实。

    生产路径（编排层查库后交行）与离线路径（不传，四类全空）用的是同一个
    类 —— 差别只在有没有行，没有第二套代码。
    """

    def __init__(self, facts: Mapping[str, Rows] | None = None) -> None:
        f: Mapping[str, Rows] = facts or {}
        unknown = [k for k in f if k not in FACT_KEYS]
        if unknown:
            raise KeyError(
                f"未知事实键 {sorted(unknown)}；可选: {', '.join(FACT_KEYS)}"
            )

        def rows(key: str) -> Rows:
            value = f.get(key)
            if isinstance(value, pd.DataFrame):
                return value.to_dict(orient="records")
            return value or ()

        # 一次整形，之后只是取用 —— 访问器因此不需要是异步计算
        self._pairs = {
            "domain": FactPair(
                confirmed=_domain_snapshot(
                    rows("domain"),
                    [
                        {
                            "table_name": r.get("dataset_name"),
                            "domain_id": r.get("domain_id"),
                            "domain_name": r.get("domain_name"),
                        }
                        for r in rows("dataset")
                        if _text(r.get("dataset_name")) and _text(r.get("domain_name"))
                    ],
                    staged=False,
                ),
                staged=_domain_snapshot(
                    rows("staged_domain"),
                    [
                        {
                            "table_name": name,
                            "domain_id": r.get("id"),
                            "domain_name": r.get("domain_name"),
                            **_attrs(r, True),
                        }
                        for r in rows("staged_domain")
                        for name in _member_tables(r.get("member_tables"))
                    ],
                    staged=True,
                ),
            ),
            "dataset": FactPair(
                confirmed=_dataset_snapshot(
                    rows("dataset"), rows("dataset_fields"), staged=False
                ),
                staged=_dataset_snapshot(
                    rows("staged_dataset"),
                    rows("staged_dataset_fields"),
                    staged=True,
                ),
            ),
            "columns_caliber": FactPair(
                confirmed=_caliber_snapshot(
                    rows("dimension_caliber"), rows("metric_caliber"), staged=False
                ),
                staged=_caliber_snapshot(
                    rows("staged_dimension_caliber"),
                    rows("staged_metric_caliber"),
                    staged=True,
                ),
            ),
            "columns_dict": FactPair(
                confirmed=_dict_snapshot(
                    rows("dimension"),
                    rows("metric"),
                    rows("dimension_caliber"),
                    rows("metric_caliber"),
                    staged=False,
                ),
                staged=_dict_snapshot(
                    rows("staged_dimension"),
                    rows("staged_metric"),
                    rows("staged_dimension_caliber"),
                    rows("staged_metric_caliber"),
                    staged=True,
                ),
            ),
        }

    async def fetch(self) -> None:
        """行在构造时就给全了，无需拉取。保留方法以满足协议。"""

    def facts_of(self, key: str, lifecycle: str):
        """预绑定「哪个产出、哪一侧」的读取器。

        ``lifecycle`` 收 str（``'staged'`` / ``'confirmed'``，即
        :class:`FactPair` 的字段名）—— 收 ``Lifecycle`` 枚举会让 inputs
        层反向依赖 contexts 包，装配层传 ``lifecycle.value`` 即可。
        键不认识时返回 None —— context 拿到 None 即退化纯本地。
        """
        pair = self._pairs.get(key)
        if pair is None:
            return None

        async def read():
            return getattr(pair, lifecycle, None)

        return read


__all__ = ["FACT_KEYS", "FactPair", "ProbeFactsInput", "StaticProbeFactsInput"]

"""CALIBER：日志证据合并 + 逐表 LLM 列角色判定（维度/指标/日期一次定完）。

legacy 的 ``DimensionCaliberTaskHandler``（纯确定性合并）与
``MetricCaliberTaskHandler``（逐表 LLM）在此合成一个任务，对应新架构的
六合四。四个阶段：

1. **确定性合并**：dimension_usage 证据 left-join 进列元数据（只更新已有列，
   绝不新增行）；metric_expr 证据展开成列级 lookup 后合并，**派生指标名会
   新增行**（``data_type=derived``）——这是维度与指标证据的关键行为差异。
2. **逐表 LLM 判定**：每表一个单元并发执行（legacy 串行）。响应非法、缺列
   或角色越界时逐列走 ``_fallback_role`` / ``_fallback_time_window`` 兜底，
   ``role_confidence=0.0``；LLM 未配置时整体走兜底跑完（D4）。
3. **实体名对齐**：在同一 domain 内分别对齐 Dimension / Metric，优先
   复用 workspace 正式目录的 canonical name，未命中的候选按域批量交给 LLM。
4. **关系收口**：剔除当前数据源已确认的关系，只删除完全重复候选；
   同自然键的不同口径保留为待审冲突。

判定范围（D3）：``working_tables()`` 里能解析出 domain 的表——CALIBER 吃的
是 dataset 逻辑层，legacy 的 ``view_`` 前缀开关不再需要。

产物 :class:`CaliberSnapshot` 按契约列输出，另携带列注释 / 枚举 /
日志计数等额外列（契约是「必须有」不是「只能有」），供 DICT 聚合使用。
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..evidence import DomainEvidence, TableEvidence, TaskEvidence
from ..parser.sql_analysis import SqlAnalysis
from ..snapshots import CaliberSnapshot
from ..snapshots.caliber import METRIC_TIME_WINDOWS, ColumnRole
from .base import TaskHandler
from .llm import AsyncLlmClient, extract_json_payload
from .prompts import (
    build_column_role_system_prompt,
    build_column_role_user_prompt,
    build_semantic_entity_match_system_prompt,
    build_semantic_entity_match_user_prompt,
)
from .runner import LlmRunner, LlmUnit
from .validators import (
    as_confidence,
    as_enum_list,
    validate_name_normalization_items,
)

logger = logging.getLogger(__name__)

COLUMN_ROLES: tuple[str, ...] = tuple(role.value for role in ColumnRole)

#: 枚举值只是角色判定的参考，过长会挤占 prompt，按此长度截断。
_MAX_ENUM_CHARS = 200

#: LLM 判定结果写入的列；构造 payload 时剔除，不给模型看旧答案。
_ROLE_COLUMNS = (
    "column_role",
    "dimension_name",
    "metric_name",
    "time_window",
    "additivity",
    "expr",
    "role_reason",
    "role_confidence",
    "is_dimension",
    "is_metric",
    "is_date",
)

#: 可加性取值域（MG v2.1 L7）。
ADDITIVITY_VALUES: tuple[str, ...] = ("additive", "semi_additive", "non_additive")


# ===========================================================================
# 阶段 1：日志证据 → 列元数据（移植自 profiler_dimension_caliber / metric_caliber）
# ===========================================================================


def explode_table_refs(
    records: pd.DataFrame,
    table_names: set[str],
    *,
    lineage: Mapping[str, list[str]] | None = None,
) -> pd.DataFrame:
    """把 ``"a,b"`` 多表引用展开为每表一行，滤掉 ``"_"`` 与未知表。

    ``lineage``：物理表 → 逻辑表（dataset）的血缘映射。日志证据引用的是
    物理表，而 CALIBER 工作在 dataset 逻辑层——列既然按 parents 完整继承，
    证据同样沿血缘继承；一张物理表喂多个 dataset 时证据各记一份。
    物理表自身也在工作集时（dataset_derived_from_tables_enabled 直通路径）走恒等映射。
    """
    if records.empty or "table_name" not in records.columns:
        return records

    def extract(value: Any) -> list[str]:
        physical = [
            ref.rsplit(".", 1)[-1]
            for item in str(value or "").split(",")
            if (ref := item.strip()) and ref != "_"
        ]
        if lineage is None:
            return physical
        targets: list[str] = []
        for name in physical:
            for target in lineage.get(name, [name] if name in table_names else []):
                if target not in targets:
                    targets.append(target)
        return targets

    transformed = records.copy()
    transformed["table_name"] = transformed["table_name"].map(extract)
    transformed = transformed.explode("table_name", ignore_index=True)
    transformed = transformed[transformed["table_name"].notna()]
    if table_names:
        transformed = transformed[transformed["table_name"].isin(table_names)]
    return transformed.reset_index(drop=True)


def _aggregate_dimension_records(records: pd.DataFrame) -> pd.DataFrame:
    """展开后重复主键先聚合，否则 merge 会让列行成倍膨胀。"""
    if records.empty or not {"table_name", "column_name"}.issubset(records.columns):
        return records
    frame = records.copy()
    frame["table_name"] = frame["table_name"].astype(str)
    frame["column_name"] = frame["column_name"].astype(str)
    frame["count"] = (
        pd.to_numeric(frame.get("count"), errors="coerce").fillna(0).astype(int)
        if "count" in frame.columns
        else 1
    )
    if "sources" not in frame.columns:
        frame["sources"] = [[] for _ in range(len(frame))]

    def union_sources(values: Any) -> list[str]:
        merged: list[str] = []
        for value in values:
            for item in value if isinstance(value, (list, tuple)) else [value]:
                text = str(item)
                if text and text not in merged:
                    merged.append(text)
        return merged

    return (
        frame.groupby(["table_name", "column_name"], as_index=False)
        .agg(count=("count", "sum"), sources=("sources", union_sources))
        .reset_index(drop=True)
    )


def merge_dimension_evidence(
    records: pd.DataFrame | None,
    table_map: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """维度证据 left-join：追加 ``query_log_clause_*`` 与 ``is_dimension_in_query_logs``。"""
    empty_lookup = pd.DataFrame(
        columns=["column_name", "query_log_clause_count", "query_log_clause_type"]
    )
    if records is None or records.empty:
        lookup = empty_lookup
    else:
        lookup = records.rename(
            columns={
                "sources": "query_log_clause_type",
                "count": "query_log_clause_count",
            }
        )
        for col in ("table_name", "column_name"):
            lookup[col] = lookup[col].astype(str)

    merged_map: dict[str, pd.DataFrame] = {}
    for table_name, table_df in table_map.items():
        current = table_df.copy()
        current["column_name"] = current["column_name"].astype(str)
        if "table_name" in lookup.columns and not lookup.empty:
            table_lookup = lookup[lookup["table_name"] == str(table_name)][
                ["column_name", "query_log_clause_count", "query_log_clause_type"]
            ].copy()
        else:
            table_lookup = empty_lookup
        drop_cols = [
            c
            for c in (
                "query_log_clause_count",
                "query_log_clause_type",
                "is_dimension_in_query_logs",
            )
            if c in current.columns
        ]
        if drop_cols:
            current = current.drop(columns=drop_cols)
        merged = current.merge(table_lookup, on="column_name", how="left")
        merged["is_dimension_in_query_logs"] = (
            merged["query_log_clause_count"].notna().map(lambda x: "yes" if x else "no")
        )
        merged_map[str(table_name)] = merged
    return merged_map


def expand_metric_column_lookup(metrics_df: pd.DataFrame) -> pd.DataFrame:
    """指标聚合结果 → 列级 lookup；一条指标展开成物理聚合列行 + 派生指标名行。"""
    columns = [
        "column_name",
        "table_name",
        "query_log_aggregation_count",
        "query_log_aggregation_type",
        "formula",
        "metric_name",
        "is_derived_metric",
    ]
    if metrics_df is None or metrics_df.empty:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    for _, row in metrics_df.iterrows():
        table_names = [
            name.strip()
            for name in str(row.get("table_name") or "").split(",")
            if name.strip() and name.strip() != "_"
        ]
        if not table_names:
            continue

        count = int(row.get("count") or 0)
        agg_type = str(row.get("agg_function") or "").strip()
        formula = str(row.get("formula") or "").strip()
        metric_name = str(row.get("metric_name") or "").strip()
        agg_cols = [
            col.strip()
            for col in str(row.get("agg_column") or "").split(",")
            if col.strip() and col.strip() != "*"
        ]

        for table_name in table_names:
            for col in agg_cols:
                rows.append(
                    {
                        "table_name": table_name,
                        "column_name": col,
                        "query_log_aggregation_count": count,
                        "query_log_aggregation_type": agg_type,
                        "formula": formula,
                        "metric_name": metric_name,
                        "is_derived_metric": False,
                    }
                )
            if metric_name and (metric_name not in agg_cols or not agg_cols):
                rows.append(
                    {
                        "table_name": table_name,
                        "column_name": metric_name,
                        "query_log_aggregation_count": count,
                        "query_log_aggregation_type": agg_type or "expression",
                        "formula": formula or metric_name,
                        "metric_name": metric_name,
                        "is_derived_metric": True,
                    }
                )

    if not rows:
        return pd.DataFrame(columns=columns)
    expanded = pd.DataFrame(rows)
    return expanded.groupby(["table_name", "column_name"], as_index=False).agg(
        query_log_aggregation_count=("query_log_aggregation_count", "sum"),
        query_log_aggregation_type=(
            "query_log_aggregation_type",
            lambda values: list(dict.fromkeys([v for v in values if v])),
        ),
        formula=("formula", "first"),
        metric_name=("metric_name", "first"),
        is_derived_metric=("is_derived_metric", "max"),
    )


def merge_metric_evidence(
    records: pd.DataFrame | None,
    table_map: dict[str, pd.DataFrame],
    *,
    allow_new_columns: bool = True,
) -> dict[str, pd.DataFrame]:
    """指标证据合并；派生指标名不在原表时**追加新行**（``data_type=derived``）。"""
    lookup = expand_metric_column_lookup(
        records if isinstance(records, pd.DataFrame) else pd.DataFrame()
    )
    for col in ("table_name", "column_name"):
        if col in lookup.columns:
            lookup[col] = lookup[col].astype(str)

    evidence_cols = [
        "column_name",
        "query_log_aggregation_count",
        "query_log_aggregation_type",
        "formula",
        "metric_name",
        "is_derived_metric",
    ]

    merged_map: dict[str, pd.DataFrame] = {}
    for table_name, table_df in table_map.items():
        current = table_df.copy()
        current["column_name"] = current["column_name"].astype(str)

        drop_cols = [
            c
            for c in (
                "query_log_aggregation_count",
                "query_log_aggregation_type",
                "is_metric_in_query_logs",
                "query_log_metric_name",
                "metric_formula",
                "metric_name",
                "is_derived_metric",
                *_ROLE_COLUMNS,
            )
            if c in current.columns
        ]
        if drop_cols:
            current = current.drop(columns=drop_cols)

        if lookup.empty:
            table_lookup = pd.DataFrame(columns=evidence_cols)
        else:
            table_lookup = lookup[lookup["table_name"] == str(table_name)][
                evidence_cols
            ].copy()
        # 日志侧 metric_name 是 SQL 别名，列角色阶段会把 metric_name 写成语义名
        table_lookup = table_lookup.rename(
            columns={"metric_name": "query_log_metric_name"}
        )
        merged = current.merge(table_lookup, on="column_name", how="left")

        if allow_new_columns and not table_lookup.empty:
            existing = set(merged["column_name"].astype(str))
            new_rows = table_lookup[
                ~table_lookup["column_name"].astype(str).isin(existing)
            ].copy()
            if not new_rows.empty:
                for col in merged.columns:
                    if col not in new_rows.columns:
                        new_rows[col] = None
                new_rows["table_name"] = str(table_name)
                if "column_comment" in merged.columns:
                    new_rows["column_comment"] = new_rows["formula"]
                if "data_type" in merged.columns:
                    new_rows["data_type"] = new_rows["data_type"].fillna("derived")
                merged = pd.concat(
                    [merged, new_rows[merged.columns]], ignore_index=True
                )

        merged = merged.rename(columns={"formula": "metric_formula"})
        merged["is_metric_in_query_logs"] = (
            merged["query_log_aggregation_count"]
            .notna()
            .map(lambda x: "yes" if x else "no")
        )
        merged_map[str(table_name)] = merged
    return merged_map


# ===========================================================================
# 阶段 2：逐表 LLM 判定（移植自 MetricCaliberTaskHandler）
# ===========================================================================


def _build_column_role_payload(table_frame: pd.DataFrame) -> list[dict[str, Any]]:
    """列详情 → JSON Prompt 记录；剔除角色列，截断超长枚举。"""
    records: list[dict[str, Any]] = []
    for row in table_frame.to_dict(orient="records"):
        record: dict[str, Any] = {}
        for key, value in row.items():
            if key in _ROLE_COLUMNS:
                if (
                    key == "metric_name"
                    and not row.get("query_log_metric_name")
                    and value is not None
                    and not (isinstance(value, float) and pd.isna(value))
                ):
                    record["query_log_metric_name"] = value
                continue
            if value is None or (isinstance(value, float) and pd.isna(value)):
                record[key] = None
            elif key == "column_enums":
                text = str(value)
                record[key] = (
                    text
                    if len(text) <= _MAX_ENUM_CHARS
                    else f"{text[:_MAX_ENUM_CHARS]}..."
                )
            else:
                record[key] = value
        records.append(record)
    return records


def _fallback_role(row: Mapping[str, Any]) -> str:
    """LLM 没给出可用答案时，按日志证据与数据类型推一个角色。"""
    data_type = str(row.get("data_type") or "").lower()
    if any(token in data_type for token in ("date", "time", "timestamp")):
        return "date"
    if str(row.get("is_metric_in_query_logs") or "").lower() == "yes":
        return "metric"
    if str(row.get("is_dimension_in_query_logs") or "").lower() == "yes":
        return "dimension"
    return "other"


def _fallback_time_window(row: Mapping[str, Any], table_name: str) -> str:
    """模型未给有效时间窗口时，从表名、指标名和公式做保守推断。

    值域 v2.1：``1d/7d/30d/mtd``。月度语义归 ``mtd``（月初至今），
    证据不足时保守取 ``1d`` —— 窗口是候选态必经人确认，宁可给最常见的
    日粒度让人改，也不发明规范外的值。
    """
    metric_evidence = " ".join(
        str(value or "").lower()
        for value in (
            row.get("column_name"),
            row.get("query_log_metric_name"),
            row.get("metric_name"),
            row.get("metric_formula"),
            row.get("column_comment"),
        )
    )

    def infer(evidence: str) -> str | None:
        if "30d" in evidence or "30_day" in evidence:
            return "30d"
        if any(token in evidence for token in ("7d", "weekly", "7_day", "周")):
            return "7d"
        if any(token in evidence for token in ("mtd", "1m", "monthly", "month", "月")):
            return "mtd"
        if any(token in evidence for token in ("1d", "daily", "当日", "单日")):
            return "1d"
        return None

    inferred = infer(metric_evidence) or infer(str(table_name).lower())
    return inferred if inferred is not None else "1d"


def _fallback_additivity(row: Mapping[str, Any]) -> str:
    """模型未给有效可加性时按表达式与名字做规则兜底。

    比率/均值类 → non_additive；余额/库存/在手类 → semi_additive；
    其余 → additive。错判成本可控 —— 本列是候选态，必经人确认。
    """
    expr_evidence = " ".join(
        str(value or "").lower()
        for value in (
            row.get("metric_formula"),
            row.get("column_name"),
            row.get("metric_name"),
            row.get("column_comment"),
        )
    )
    if any(
        token in expr_evidence
        for token in (
            "avg(",
            "rate",
            "ratio",
            "pct",
            "percent",
            "率",
            "占比",
            "比例",
            "均值",
            "平均",
        )
    ) or "/" in str(row.get("metric_formula") or ""):
        return "non_additive"
    if any(
        token in expr_evidence
        for token in (
            "balance",
            "inventory",
            "stock",
            "on_hand",
            "余额",
            "库存",
            "在手",
            "存量",
        )
    ):
        return "semi_additive"
    return "additive"


def _semantic_name(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value).strip() or None


def apply_column_roles(
    table_frame: pd.DataFrame,
    response: str,
    *,
    table_name: str = "",
) -> pd.DataFrame:
    """校验列名并把 LLM 的角色判定合并回当前表（逐列兜底，不整批作废）。"""
    try:
        parsed = extract_json_payload(response)
        items = parsed if isinstance(parsed, list) else []
    except Exception as exc:
        logger.warning("Invalid column role response: %s", exc)
        items = []

    roles_by_column = {
        str(item.get("column_name") or ""): item
        for item in items
        if isinstance(item, Mapping) and str(item.get("column_name") or "")
    }
    resolved = table_frame.copy()
    if "query_log_metric_name" not in resolved.columns:
        resolved["query_log_metric_name"] = (
            resolved["metric_name"] if "metric_name" in resolved.columns else None
        )
    roles: list[str] = []
    dimension_names: list[str | None] = []
    metric_names: list[str | None] = []
    time_windows: list[str | None] = []
    additivities: list[str | None] = []
    exprs: list[str | None] = []
    reasons: list[str] = []
    confidences: list[float] = []

    for row in resolved.to_dict(orient="records"):
        column_name = str(row.get("column_name") or "")
        item = roles_by_column.get(column_name, {})
        role = str(item.get("column_role") or "").strip().lower()
        if role in COLUMN_ROLES:
            reason = str(item.get("reason") or "").strip()
            confidence = as_confidence(item.get("confidence"))
        else:
            role = _fallback_role(row)
            reason = "LLM 未给出可用判定，按日志证据与数据类型兜底"
            confidence = 0.0

        dimension_name = None
        metric_name = None
        time_window = None
        additivity = None
        expr = None
        if role in {"dimension", "date"}:
            dimension_name = _semantic_name(item.get("dimension_name"))
            if dimension_name is None:
                dimension_name = column_name
                confidence = min(confidence, 0.5)
        elif role == "metric":
            metric_name = _semantic_name(item.get("metric_name"))
            if metric_name is None:
                metric_name = (
                    _semantic_name(row.get("query_log_metric_name")) or column_name
                )
                confidence = min(confidence, 0.5)
            proposed_window = str(item.get("time_window") or "").strip().lower()
            time_window = (
                proposed_window
                if proposed_window in METRIC_TIME_WINDOWS
                else _fallback_time_window(row, table_name)
            )
            if proposed_window not in METRIC_TIME_WINDOWS:
                # time_window 是 metric 的必填输出且进唯一键；规则兜底不保留高置信度
                confidence = min(confidence, 0.5)
            proposed_additivity = str(item.get("additivity") or "").strip().lower()
            additivity = (
                proposed_additivity
                if proposed_additivity in ADDITIVITY_VALUES
                else _fallback_additivity(row)
            )
            # L7 expr：日志里抽到的聚合公式是硬证据、优先；没有时用模型起草的
            # SQL 片段（候选态，人确认）；两者都缺时留空，投影层回退列名保 NOT NULL
            expr = _semantic_name(row.get("metric_formula")) or _semantic_name(
                item.get("expr")
            )

        roles.append(role)
        dimension_names.append(dimension_name)
        metric_names.append(metric_name)
        time_windows.append(time_window)
        additivities.append(additivity)
        exprs.append(expr)
        reasons.append(reason)
        confidences.append(confidence)

    resolved["column_role"] = roles
    resolved["dimension_name"] = dimension_names
    resolved["metric_name"] = metric_names
    resolved["time_window"] = time_windows
    resolved["additivity"] = additivities
    resolved["expr"] = exprs
    resolved["role_reason"] = reasons
    resolved["role_confidence"] = confidences
    return resolved


# ===========================================================================
# 实体目录 + 当前源已确认关系
# ===========================================================================

_DIMENSION_KIND = "dimension"
_METRIC_KIND = "metric"


def _aliases(value: Any) -> tuple[str, ...]:
    """读取正式实体的 synonyms，兼容 snapshot 列表与 DB ``$$$`` 串。

    不按空格切分：英文业务名（如 ``daily active users``）
    本身就可以含空格。带完整方括号的字符串按列表解析；非法列表
    忽略别名，不回退拆分，避免将括号碎片用于实体匹配。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, (list, tuple, set)):
                return _aliases(parsed)
            logger.warning("Invalid synonyms list; aliases ignored")
            return ()
        values = text.split("$$$") if "$$$" in text else [text]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    elif hasattr(value, "tolist"):
        listed = value.tolist()
        values = listed if isinstance(listed, list) else [listed]
    else:
        values = [value]

    unique: list[str] = []
    for item in values:
        name = _semantic_name(item)
        if name is not None and name not in unique:
            unique.append(name)
    return tuple(unique)


@dataclass(frozen=True)
class _CatalogEntry:
    kind: str
    domain_name: str
    name: str
    description: str | None
    synonyms: tuple[str, ...]
    dimension_role: str | None = None

    def prompt_record(self) -> dict[str, Any]:
        """严格投影可复用目录，绝不带表、公式、窗口或样例值。"""
        record: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "synonyms": list(self.synonyms),
        }
        if self.kind == _DIMENSION_KIND:
            record["dimension_role"] = self.dimension_role
        return record


class _SemanticCatalog:
    """``(kind, domain)`` 分区的 workspace 正式实体目录。"""

    def __init__(self, entries: list[_CatalogEntry] | None = None) -> None:
        by_scope: dict[tuple[str, str], list[_CatalogEntry]] = {}
        seen: set[tuple[str, str, str]] = set()
        for entry in entries or []:
            key = (entry.kind, entry.domain_name, entry.name)
            if key in seen:
                continue
            seen.add(key)
            by_scope.setdefault((entry.kind, entry.domain_name), []).append(entry)
        self._by_scope = {
            scope: tuple(sorted(items, key=lambda item: item.name))
            for scope, items in by_scope.items()
        }

    @classmethod
    def from_evidence(cls, value: Any | None) -> _SemanticCatalog:
        entries: list[_CatalogEntry] = []
        for kind, name_field, frame in (
            (
                _DIMENSION_KIND,
                "dimension_name",
                getattr(value, "dimensions", None),
            ),
            (_METRIC_KIND, "metric_name", getattr(value, "metrics", None)),
        ):
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            for row in frame.to_dict(orient="records"):
                domain_name = _semantic_name(row.get("domain_name"))
                name = _semantic_name(row.get(name_field))
                if domain_name is None or name is None:
                    continue
                entries.append(
                    _CatalogEntry(
                        kind=kind,
                        domain_name=domain_name,
                        name=name,
                        description=_semantic_name(row.get("description")),
                        synonyms=_aliases(row.get("synonyms")),
                        # 正式表在某些读面叫 dimension_type；
                        # evidence 标准投影名是 dimension_role。
                        dimension_role=(
                            _semantic_name(row.get("dimension_role"))
                            or _semantic_name(row.get("dimension_type"))
                        ),
                    )
                )
        return cls(entries)

    def entries(self, kind: str, domain_name: str) -> tuple[_CatalogEntry, ...]:
        return self._by_scope.get((kind, domain_name), ())

    def prompt_records(self, kind: str, domain_name: str) -> list[dict[str, Any]]:
        return [entry.prompt_record() for entry in self.entries(kind, domain_name)]

    def match(self, kind: str, domain_name: str, value: Any) -> str | None:
        """精确 canonical → 唯一 casefold canonical → 唯一 synonym。"""
        name = _semantic_name(value)
        if name is None:
            return None
        entries = self.entries(kind, domain_name)

        exact = {entry.name for entry in entries if entry.name == name}
        if len(exact) == 1:
            return next(iter(exact))

        folded = name.casefold()
        casefold_hits = {
            entry.name for entry in entries if entry.name.casefold() == folded
        }
        if casefold_hits:
            return next(iter(casefold_hits)) if len(casefold_hits) == 1 else None

        synonym_hits = {
            entry.name
            for entry in entries
            if any(alias.casefold() == folded for alias in entry.synonyms)
        }
        if len(synonym_hits) == 1:
            return next(iter(synonym_hits))
        return None


_PinnedNames = dict[tuple[str, str], tuple[str, ...]]


def _match_pinned(
    pinned: _PinnedNames,
    kind: str,
    domain_name: str,
    value: Any,
) -> str | None:
    """Pinned 名称无元信息，只做精确 / 唯一 casefold 匹配。"""
    name = _semantic_name(value)
    if name is None:
        return None
    names = pinned.get((kind, domain_name), ())
    if name in names:
        return name
    hits = {candidate for candidate in names if candidate.casefold() == name.casefold()}
    return next(iter(hits)) if len(hits) == 1 else None


def _known_name(
    catalog: _SemanticCatalog,
    pinned: _PinnedNames,
    kind: str,
    domain_name: str,
    value: Any,
) -> str | None:
    """正式 catalog 优先，pinned 仅补尚未落库的本源名称。"""
    return catalog.match(kind, domain_name, value) or _match_pinned(
        pinned, kind, domain_name, value
    )


def _caliber_frame(value: Any | None) -> pd.DataFrame:
    frame = getattr(value, "calibers", None)
    return frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def _confirmed_staged_frame(value: Any | None) -> pd.DataFrame:
    frame = _caliber_frame(value)
    if frame.empty:
        return frame
    if "probe_status" not in frame.columns:
        if not bool(getattr(value, "allow_unstatused", True)):
            return frame.iloc[0:0].copy()
        # 纯离线或本轮内存 snapshot 无生命周期列，按可信上游产物处理。
        return frame
    status = frame["probe_status"].map(
        lambda value: str(value).strip().upper() if value is not None else ""
    )
    return frame.loc[status == "CONFIRMED"].copy()


def _kind_and_name(row: Mapping[str, Any]) -> tuple[str, str] | None:
    role = (_semantic_name(row.get("column_role")) or "").lower()
    if role in {_DIMENSION_KIND, "date"}:
        name = _semantic_name(row.get("dimension_name"))
        return (_DIMENSION_KIND, name) if name is not None else None
    if role == _METRIC_KIND:
        name = _semantic_name(row.get("metric_name"))
        return (_METRIC_KIND, name) if name is not None else None
    return None


def _prior_rows(evidence: TaskEvidence) -> list[dict[str, Any]]:
    confirmed = _caliber_frame(evidence.prior_confirmed)
    staged = _confirmed_staged_frame(evidence.prior_staged)
    frames = [frame for frame in (confirmed, staged) if not frame.empty]
    if not frames:
        return []
    return pd.concat(frames, ignore_index=True, sort=False).to_dict(orient="records")


def _build_pinned_names(
    prior_rows: list[dict[str, Any]],
    catalog: _SemanticCatalog,
    domain_by_table: Mapping[str, str],
) -> _PinnedNames:
    names: dict[tuple[str, str], list[str]] = {}
    for row in prior_rows:
        identified = _kind_and_name(row)
        table_name = _semantic_name(row.get("table_name"))
        domain_name = _semantic_name(row.get("domain_name")) or (
            domain_by_table.get(table_name or "")
        )
        if identified is None or domain_name is None:
            continue
        kind, name = identified
        if catalog.match(kind, domain_name, name) is not None:
            continue
        scope = (kind, domain_name)
        if name not in names.setdefault(scope, []):
            names[scope].append(name)
    return {scope: tuple(values) for scope, values in names.items()}


def _relation_key(
    row: Mapping[str, Any],
    *,
    domain_by_table: Mapping[str, str] | None = None,
) -> tuple[str, ...] | None:
    identified = _kind_and_name(row)
    table_name = _semantic_name(row.get("table_name"))
    domain_name = _semantic_name(row.get("domain_name"))
    if domain_name is None and domain_by_table is not None:
        domain_name = domain_by_table.get(table_name or "")
    if identified is None or table_name is None or domain_name is None:
        return None
    kind, name = identified
    if kind == _DIMENSION_KIND:
        return (kind, domain_name, table_name, name)
    time_window = _semantic_name(row.get("time_window"))
    if time_window is None:
        return None
    return (kind, domain_name, table_name, name, time_window.lower())


def _build_locked_relations(
    prior_rows: list[dict[str, Any]],
    catalog: _SemanticCatalog,
    pinned: _PinnedNames,
    domain_by_table: Mapping[str, str],
) -> set[tuple[str, ...]]:
    locked: set[tuple[str, ...]] = set()
    for original in prior_rows:
        row = dict(original)
        identified = _kind_and_name(row)
        table_name = _semantic_name(row.get("table_name"))
        domain_name = _semantic_name(row.get("domain_name")) or (
            domain_by_table.get(table_name or "")
        )
        if identified is None or domain_name is None:
            continue
        kind, name = identified
        canonical = _known_name(catalog, pinned, kind, domain_name, name) or name
        row["domain_name"] = domain_name
        row["dimension_name" if kind == _DIMENSION_KIND else "metric_name"] = canonical
        key = _relation_key(row)
        if key is not None:
            locked.add(key)
    return locked


def _freeze(value: Any) -> Any:
    """把 list / numpy / NaN 变成可比较的稳定值，用于完全重复去重。"""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(_freeze(item) for item in value)
    if hasattr(value, "tolist") and not isinstance(value, str):
        return _freeze(value.tolist())
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _relation_content(row: Mapping[str, Any], kind: str) -> tuple[Any, ...]:
    """按 complete 的正式持久化 payload 比较，忽略内部血缘与审计列。"""
    # provider 投影在 expr 缺失时会回退 column_name，这里必须使用同一
    # 有效值，否则两个不同列的 DimensionBinding 会被错判为重复。
    expr = _semantic_name(row.get("expr")) or _semantic_name(row.get("column_name"))
    if kind == _DIMENSION_KIND:
        return (expr, _freeze(row.get("value_example")))
    return (expr, _freeze(row.get("additivity")))


def _filter_locked_and_dedupe(
    frame: pd.DataFrame,
    locked: set[tuple[str, ...]],
) -> pd.DataFrame:
    """剔除已确认关系；同自然键的冲突内容保留，只删完全重复。"""
    kept: list[dict[str, Any]] = []
    seen: dict[tuple[str, ...], set[tuple[Any, ...]]] = {}
    locked_count = duplicate_count = 0
    for row in frame.to_dict(orient="records"):
        key = _relation_key(row)
        if key is None:
            kept.append(row)
            continue
        if key in locked:
            locked_count += 1
            continue
        content = _relation_content(row, key[0])
        if content in seen.setdefault(key, set()):
            duplicate_count += 1
            continue
        seen[key].add(content)
        kept.append(row)
    if locked_count or duplicate_count:
        logger.info(
            "caliber: filtered locked=%s exact_duplicates=%s",
            locked_count,
            duplicate_count,
        )
    return pd.DataFrame(kept, columns=frame.columns)


# ===========================================================================
# Handler
# ===========================================================================

#: 判定结果之外、随行带给 DICT 的证据列。
_CARRY_COLUMNS = (
    "column_comment",
    "column_name_cn",
    "column_enums",
    "data_type",
    "metric_formula",
    "query_log_metric_name",
    "query_log_clause_count",
    "query_log_aggregation_count",
    "is_derived_metric",
)


class CaliberHandler(TaskHandler):
    """产出 :class:`CaliberSnapshot`（每 (表, 列) 一行的角色判定）。"""

    def __init__(
        self,
        llm: AsyncLlmClient | None,
        *,
        runner: LlmRunner | None = None,
        language: str = "zh",
        dialect: str = "postgres",
        #: 运行模式（create / refine）。CALIBER 本期不做逐草稿 refine，
        #: 两种模式统一走 canonicalize / 去锁定 / 去重流程。
        mode: str = "create",
    ) -> None:
        self._llm = llm
        self._runner = runner
        self._language = language
        self._dialect = dialect

    def _parent_lineage(
        self,
        working: TableEvidence,
        known: set[str],
    ) -> dict[str, list[str]]:
        """物理父表（短名）→ 依赖它的工作表清单。

        parents 列缺失/为空时回退解析 ``sql_content``：v2.1 的 staging /
        正式表只存单指针 ``source``，SQL 派生数据集经 DB 事实往返读回后
        parents 必为 None —— 不解析血缘就断，日志证据会被
        ``explode_table_refs`` 全部滤掉（静默降级）。血缘消费点自己补，
        staged / confirmed 两侧同愈。"""
        lineage: dict[str, list[str]] = {}
        meta = working.table_meta
        if "parents" not in meta.columns and "sql_content" not in meta.columns:
            return lineage

        def _cell_of(row, key: str) -> str | None:
            raw = getattr(row, key, None)
            if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                return None
            text = str(raw).strip()
            return text or None

        for row in meta.itertuples(index=False):
            name = str(getattr(row, "table_name", "") or "")
            if name not in known:
                continue
            raw = _cell_of(row, "parents")
            parents: list[str] = []
            if raw is not None:
                parents = [item.strip() for item in raw.split(",") if item.strip()]
            else:
                sql = _cell_of(row, "sql_content")
                if sql is not None:
                    try:
                        parents = sorted(
                            SqlAnalysis.create_from_sql(
                                sql, dialect=self._dialect
                            ).physical_table_names
                        )
                    except Exception as exc:
                        logger.info(
                            "caliber %s: sql_content 血缘解析失败，日志证据降级: %s",
                            name,
                            exc,
                        )
            for item in parents:
                parent = item.rsplit(".", 1)[-1]
                if parent and name not in lineage.setdefault(parent, []):
                    lineage[parent].append(name)
        return lineage

    @staticmethod
    def _domain_by_table(
        working: TableEvidence,
        domains: DomainEvidence | None,
    ) -> dict[str, str]:
        """表 → domain。dataset 的 table_meta 自带 domain_name，物理表退回查 members。"""
        mapping: dict[str, str] = {}
        meta = working.table_meta
        if "domain_name" in meta.columns:
            for row in meta.itertuples(index=False):
                name = _semantic_name(getattr(row, "table_name", None)) or ""
                domain = _semantic_name(getattr(row, "domain_name", None))
                if name and domain is not None:
                    mapping[name] = domain
        if domains is not None:
            for name in working.names():
                if name not in mapping:
                    hit = domains.domain_of(name)
                    if hit:
                        mapping[name] = hit
        return mapping

    @staticmethod
    def _candidate_record(
        name: str,
        kind: str,
        members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """名称匹配只使用当前源的名称 / 列注释 / 维度类型证据。"""

        def unique_values(*fields: str) -> list[str]:
            values: list[str] = []
            for member in members:
                for field in fields:
                    value = _semantic_name(member.get(field))
                    if value is not None and value not in values:
                        values.append(value)
            return values

        record: dict[str, Any] = {
            "name": name,
            "column_names": unique_values("column_name"),
            "column_comments": unique_values("column_comment", "column_name_cn"),
        }
        if kind == _DIMENSION_KIND:
            record["dimension_role"] = (
                "time"
                if any(
                    (_semantic_name(member.get("column_role")) or "").lower() == "date"
                    for member in members
                )
                else "categorical"
            )
        return record

    async def _canonicalize_names(
        self,
        frame: pd.DataFrame,
        catalog: _SemanticCatalog,
        pinned: _PinnedNames,
    ) -> pd.DataFrame:
        """确定性匹配优先；未命中候选按 (domain, kind) 整域交给 LLM。"""
        members_by_candidate: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in frame.to_dict(orient="records"):
            identified = _kind_and_name(row)
            domain_name = _semantic_name(row.get("domain_name"))
            if identified is None or domain_name is None:
                continue
            kind, name = identified
            members_by_candidate.setdefault((kind, domain_name, name), []).append(row)

        mapping: dict[tuple[str, str, str], str] = {}
        unresolved_by_scope: dict[tuple[str, str], list[str]] = {}
        for kind, domain_name, name in sorted(members_by_candidate):
            canonical = _known_name(catalog, pinned, kind, domain_name, name)
            if canonical is not None:
                mapping[(kind, domain_name, name)] = canonical
            else:
                mapping[(kind, domain_name, name)] = name
                unresolved_by_scope.setdefault((kind, domain_name), []).append(name)

        scopes = sorted(unresolved_by_scope)
        if scopes and self._llm is not None and self._runner is not None:
            units: list[LlmUnit] = []
            for kind, domain_name in scopes:
                names = sorted(unresolved_by_scope[(kind, domain_name)])
                candidates = [
                    self._candidate_record(
                        name,
                        kind,
                        members_by_candidate[(kind, domain_name, name)],
                    )
                    for name in names
                ]
                prompt_kind = "维度" if kind == _DIMENSION_KIND else "指标"
                units.append(
                    LlmUnit(
                        label=f"caliber:{kind}:match:{domain_name or '(none)'}",
                        system_prompt=build_semantic_entity_match_system_prompt(
                            prompt_kind, self._language
                        ),
                        user_prompt=build_semantic_entity_match_user_prompt(
                            prompt_kind,
                            domain_name,
                            candidates,
                            catalog.prompt_records(kind, domain_name),
                            list(pinned.get((kind, domain_name), ())),
                            self._language,
                        ),
                    )
                )

            responses = await self._runner.run_units(units)
            for (kind, domain_name), response in zip(scopes, responses):
                names = unresolved_by_scope[(kind, domain_name)]
                if response is None:
                    continue
                try:
                    proposed = validate_name_normalization_items(
                        extract_json_payload(response), set(names)
                    )
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "caliber %s semantic match failed for domain %s; "
                        "keep provisional names: %s",
                        kind,
                        domain_name or "(none)",
                        exc,
                    )
                    continue
                for name, value in proposed.items():
                    canonical = (
                        _known_name(catalog, pinned, kind, domain_name, value)
                        or _semantic_name(value)
                        or name
                    )
                    mapping[(kind, domain_name, name)] = canonical

        def collapse(key: tuple[str, str, str]) -> str:
            """LLM 可把 A 指向另一候选 B；跟随映射保证最终收口一致。"""
            kind, domain_name, original = key
            target = mapping.get(key, original)
            visited: list[str] = [original]
            while target not in visited:
                next_value = mapping.get((kind, domain_name, target))
                if next_value is None or next_value == target:
                    return target
                visited.append(target)
                target = next_value
            # 非法循环时只在环本身选稳定 canonical；指向环的前缀节点必须
            # 跟随同一结果，不能参与 min 后分裂出另一个名称。
            cycle = visited[visited.index(target) :]
            return min(set(cycle), key=lambda value: (value.casefold(), value))

        resolved = frame.copy()
        changed = 0
        for index, row in resolved.iterrows():
            record = row.to_dict()
            identified = _kind_and_name(record)
            domain_name = _semantic_name(record.get("domain_name"))
            if identified is None or domain_name is None:
                continue
            kind, name = identified
            canonical = collapse((kind, domain_name, name))
            if canonical != name:
                changed += 1
            field = "dimension_name" if kind == _DIMENSION_KIND else "metric_name"
            resolved.at[index, field] = canonical
        if changed:
            logger.info("caliber: canonicalized semantic names rows=%s", changed)
        return resolved

    async def handle(self, evidence: TaskEvidence) -> CaliberSnapshot:
        working = evidence.working_tables()
        if working is None or working.is_empty():
            logger.info("caliber: 无工作表集，产出空快照")
            return CaliberSnapshot.build(None)

        domains = self._domain_by_table(working, evidence.domains)
        table_map = {
            name: frame.copy()
            for name, frame in working.column_meta.items()
            if name in domains and isinstance(frame, pd.DataFrame) and len(frame)
        }
        if not table_map:
            # 两种成因分开报 —— 合并成一条会把排查引向错误方向：
            # 「无列」是上游快照不完整，「无 domain」才是归属缺失
            if not any(
                isinstance(f, pd.DataFrame) and len(f)
                for f in working.column_meta.values()
            ):
                logger.info(
                    "caliber: 工作表集无列元数据（上游快照不完整？），产出空快照"
                )
            else:
                logger.info("caliber: 无带 domain 的表，产出空快照")
            return CaliberSnapshot.build(None)

        # -- 阶段 1：确定性证据合并（日志证据沿 parents 血缘映射到逻辑层）--
        known = set(table_map)
        lineage = self._parent_lineage(working, known)
        dim_records = _aggregate_dimension_records(
            explode_table_refs(
                evidence.logs.of("dimension_usage")
                if evidence.logs is not None
                else pd.DataFrame(),
                known,
                lineage=lineage,
            )
        )
        table_map = merge_dimension_evidence(
            dim_records if isinstance(dim_records, pd.DataFrame) else None, table_map
        )
        metric_records = explode_table_refs(
            evidence.logs.of("metric_expr")
            if evidence.logs is not None
            else pd.DataFrame(),
            known,
            lineage=lineage,
        )
        table_map = merge_metric_evidence(
            metric_records, table_map, allow_new_columns=True
        )
        logger.info("caliber: 证据合并完成 tables=%s", len(table_map))

        # -- 阶段 2：逐表 LLM 判定（未配置 LLM 时整体兜底，D4）--
        names = sorted(table_map)
        if self._llm is not None and self._runner is not None:
            units = [
                LlmUnit(
                    label=f"caliber:{name}",
                    system_prompt=build_column_role_system_prompt(self._language),
                    user_prompt=build_column_role_user_prompt(
                        name,
                        _build_column_role_payload(table_map[name]),
                        self._language,
                    ),
                )
                for name in names
            ]
            responses = await self._runner.run_units(units)
        else:
            logger.warning("caliber: 未配置 LLM，全部列走规则兜底（D4 降级）")
            responses = [None] * len(names)

        rows: list[dict[str, Any]] = []
        for name, response in zip(names, responses):
            classified = apply_column_roles(
                table_map[name], response or "[]", table_name=name
            )
            role_counts = classified["column_role"].value_counts().to_dict()
            logger.info("caliber %s: %s", name, role_counts)
            for row in classified.to_dict(orient="records"):
                role = str(row.get("column_role") or "")
                # 维度侧样例值：绑定层 value_example 的来源（v2.1：枚举下沉到
                # (dimension, dataset) 绑定，随口径行走，不再聚到维度字典上）
                value_example = (
                    as_enum_list(row.get("column_enums"))
                    if role in ("dimension", "date")
                    else None
                )
                out: dict[str, Any] = {
                    "table_name": name,
                    "column_name": row.get("column_name"),
                    "domain_name": domains.get(name),
                    "column_role": role,
                    "dimension_name": row.get("dimension_name"),
                    "value_example": value_example,
                    "metric_name": row.get("metric_name"),
                    "time_window": row.get("time_window"),
                    # 口径表达式：日志公式优先，其次模型起草的聚合片段（apply_column_roles
                    # 已合并）；两者都缺时投影层回退列名保 NOT NULL
                    "expr": row.get("expr"),
                    "additivity": row.get("additivity"),
                    "role_reason": row.get("role_reason"),
                    "role_confidence": row.get("role_confidence"),
                }
                for key in _CARRY_COLUMNS:
                    if key not in out and key in row:
                        out[key] = row[key]
                rows.append(out)

        candidates = pd.DataFrame(rows)

        # -- 阶段 3：名称对齐（只换名，不触碰本源口径内容）--
        catalog = _SemanticCatalog.from_evidence(
            getattr(evidence, "semantic_catalog", None)
        )
        prior_rows = _prior_rows(evidence)
        pinned = _build_pinned_names(prior_rows, catalog, domains)
        candidates = await self._canonicalize_names(candidates, catalog, pinned)

        # -- 阶段 4：只锁当前源已确认关系，再做完全重复去重 --
        locked = _build_locked_relations(prior_rows, catalog, pinned, domains)
        candidates = _filter_locked_and_dedupe(candidates, locked)
        return CaliberSnapshot.build(candidates)


__all__ = [
    "COLUMN_ROLES",
    "CaliberHandler",
    "apply_column_roles",
    "expand_metric_column_lookup",
    "explode_table_refs",
    "merge_dimension_evidence",
    "merge_metric_evidence",
]

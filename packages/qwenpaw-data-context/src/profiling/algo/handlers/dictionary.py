"""DICT：按 CALIBER 已确定的名称跨表聚合 + 描述补全，产出维度库与指标库。

legacy 的 ``DimensionManageTaskHandler`` / ``MetricManageTaskHandler``
（同一模板类的两个薄子类）在此合成一个任务。每条聚合线分两步：

1. 从 CALIBER 结果里筛目标列（维度线收 ``dimension/date``、指标线收 ``metric``；
   date 列是 L4 dimension_role=time 的来源）；按 ``(domain_name, canonical name)``
   跨表聚合；
2. **LLM 分批补描述**（每批 40）：单批失败只影响该批，漏答退回列注释。

名称归一与已有实体匹配属于 CALIBER 的职责。DICT 不得再改变名称，否则
``staging_*_caliber`` 通过名称引用实体时会发生断链。

与 legacy 的产物差异：多值字段（synonyms/formulas/tables）按新
snapshot 契约存 ``list[str]``，不再用 ``$$$`` 拼接串。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pandas as pd

from ..evidence import TaskEvidence
from ..snapshots import DictSnapshot
from .base import TaskHandler
from .llm import AsyncLlmClient, extract_json_payload
from .prompts import (
    build_dimension_meta_system_prompt,
    build_dimension_meta_user_prompt,
    build_metric_system_prompt,
    build_metric_user_prompt,
)
from .runner import LlmRunner, LlmUnit
from .validators import (
    as_enum_list,
    as_int,
    validate_description_items,
)

logger = logging.getLogger(__name__)

#: 单个条目进入**产物**的枚举/公式上限。
_MAX_ENUMS = 50
#: 单个条目进入 **Prompt** 的枚举样例上限。
_PROMPT_ENUMS = 8
#: 单个枚举样例在 Prompt 里的最大长度。
_PROMPT_ENUM_CHARS = 24
#: 每次描述调用处理的条目数量。
_BATCH_SIZE = 40

_Members = dict[tuple[str, str], list[dict[str, Any]]]


# ===========================================================================
# 成员工具（移植自 _SemanticAggregationTaskHandler）
# ===========================================================================


def _clean_str(value: Any) -> str:
    """成员行取值 → 非空字符串；``None`` / ``NaN`` 归空串。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text in {"None", "nan"} else text


def _columns_of(members: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for member in members:
        name = _clean_str(member.get("column_name"))
        if name and name not in columns:
            columns.append(name)
    return columns


def _comments_of(members: list[dict[str, Any]]) -> list[str]:
    comments: list[str] = []
    for member in members:
        for key in ("column_comment", "column_name_cn"):
            text = _clean_str(member.get(key))
            if text and text not in comments:
                comments.append(text)
    return comments


def _tables_of(members: list[dict[str, Any]]) -> list[str]:
    tables: list[str] = []
    for member in members:
        name = _clean_str(member.get("table_name"))
        if name and name not in tables:
            tables.append(name)
    return tables


def _enums_of(members: list[dict[str, Any]]) -> list[str]:
    merged: list[str] = []
    for member in members:
        for enum in as_enum_list(member.get("column_enums")):
            if enum not in merged:
                merged.append(enum)
            if len(merged) >= _MAX_ENUMS:
                return merged
    return merged


def _dimension_role_of(members: list[dict[str, Any]]) -> str:
    """维度类型（v2.1 自动档）：任一成员列是时间语义 → time，否则 categorical。

    规则即可靠 —— CALIBER 已把时间列判成 ``column_role=date``；data_type
    含 date/time/timestamp 是硬证据。不上 LLM。
    """
    for member in members:
        if _clean_str(member.get("column_role")).lower() == "date":
            return "time"
        data_type = _clean_str(member.get("data_type")).lower()
        if any(token in data_type for token in ("date", "time", "timestamp")):
            return "time"
    return "categorical"


def _total_query_count(members: list[dict[str, Any]]) -> int:
    return sum(
        as_int(member.get("query_log_clause_count"))
        + as_int(member.get("query_log_aggregation_count"))
        for member in members
    )


def _dedupe_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 一个物理列可以为同一 Metric 提供多个 window / formula；仅按
    # (table, column) 去重会把这些合法关系吞掉。这里按关系身份与持久内容
    # 去重，既折叠 formal + staging 的完全重复证据，也保留不同本地口径。
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for member in members:
        key = (
            _clean_str(member.get("table_name")),
            _clean_str(member.get("column_name")),
            _clean_str(member.get("column_role")).lower(),
            _clean_str(member.get("dimension_name")),
            _clean_str(member.get("metric_name")),
            _clean_str(member.get("time_window")).lower(),
            _clean_str(member.get("expr")),
            _clean_str(member.get("additivity")),
            tuple(as_enum_list(member.get("value_example"))),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(member)
    return unique


# ===========================================================================
# 两条聚合线的差异描述（维度 / 指标）
# ===========================================================================


class _DimensionLine:
    kind = "维度"
    name_field = "dimension_name"
    description_field = "dimension_description"

    @staticmethod
    def accepts(row: Mapping[str, Any]) -> bool:
        # v2.1：date 角色也进维度字典（L4 dimension_role=time 的唯一来源）；
        # CALIBER 已为 date 列产出 dimension_name，此前只是被这里滤掉。
        return str(row.get("column_role") or "").strip().lower() in (
            "dimension",
            "date",
        )

    @staticmethod
    def semantic_name(row: Mapping[str, Any]) -> str:
        return _clean_str(row.get("dimension_name")) or _clean_str(
            row.get("column_name")
        )

    @staticmethod
    def build_system_prompt(language: str = "zh") -> str:
        return build_dimension_meta_system_prompt(language)

    @staticmethod
    def build_user_prompt(payload: list[dict[str, Any]], language: str = "zh") -> str:
        return build_dimension_meta_user_prompt(payload, language)

    @staticmethod
    def extra_payload(members: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "enums_sample": [
                enum[:_PROMPT_ENUM_CHARS] for enum in _enums_of(members)[:_PROMPT_ENUMS]
            ]
        }

    @staticmethod
    def final_row(
        domain_name: str,
        name: str,
        description: str | None,
        members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # v2.1：enums 不再聚到维度字典（样例值下沉绑定层 value_example）；
        # dimension_role 按成员列规则推断（自动档）。
        return {
            "domain_name": domain_name or None,
            "dimension_name": name,
            "description": description,
            "synonyms": _columns_of(members),
            "dimension_role": _dimension_role_of(members),
            "tables": _tables_of(members),
        }


class _MetricLine:
    kind = "指标"
    name_field = "metric_name"
    description_field = "metric_description"

    @staticmethod
    def accepts(row: Mapping[str, Any]) -> bool:
        return str(row.get("column_role") or "").strip().lower() == "metric"

    @staticmethod
    def semantic_name(row: Mapping[str, Any]) -> str:
        return (
            _clean_str(row.get("metric_name"))
            or _clean_str(row.get("query_log_metric_name"))
            or _clean_str(row.get("column_name"))
        )

    @staticmethod
    def build_system_prompt(language: str = "zh") -> str:
        return build_metric_system_prompt(language)

    @staticmethod
    def build_user_prompt(payload: list[dict[str, Any]], language: str = "zh") -> str:
        return build_metric_user_prompt(payload, language)

    @staticmethod
    def _formulas_of(members: list[dict[str, Any]]) -> list[str]:
        formulas: list[str] = []
        for member in members:
            text = _clean_str(member.get("expr")) or _clean_str(
                member.get("metric_formula")
            )
            if not text:
                text = _clean_str(member.get("column_name"))
            if text and text not in formulas:
                formulas.append(text)
        return formulas[:_MAX_ENUMS]

    @staticmethod
    def _time_windows_of(members: list[dict[str, Any]]) -> list[str]:
        return sorted(
            {
                text
                for member in members
                if (text := _clean_str(member.get("time_window")))
            }
        )

    @classmethod
    def extra_payload(cls, members: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "time_windows": cls._time_windows_of(members),
            "formulas": cls._formulas_of(members)[:_PROMPT_ENUMS],
        }

    @classmethod
    def final_row(
        cls,
        domain_name: str,
        name: str,
        description: str | None,
        members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "domain_name": domain_name or None,
            "metric_name": name,
            "description": description,
            "synonyms": _columns_of(members),
            "formulas": cls._formulas_of(members),
            "time_windows": cls._time_windows_of(members),
            "tables": _tables_of(members),
        }


# ===========================================================================
# Handler
# ===========================================================================


class DictHandler(TaskHandler):
    """产出 :class:`DictSnapshot`（维度库 + 指标库）。"""

    def __init__(
        self,
        llm: AsyncLlmClient | None,
        *,
        runner: LlmRunner | None = None,
        batch_size: int = _BATCH_SIZE,
        language: str = "zh",
        #: 运行模式（create / refine）。本阶段仅 DOMAIN 实现 refine，
        #: 这里接收即忽略 —— 待推广时不必再改装配签名
        mode: str = "create",
    ) -> None:
        self._llm = llm
        self._runner = runner
        self._batch_size = max(1, int(batch_size))
        self._language = language

    # -- 聚合 ------------------------------------------------------------

    @staticmethod
    def _group(calibers: pd.DataFrame, line) -> _Members:
        grouped: _Members = {}
        for row in calibers.to_dict(orient="records"):
            if not line.accepts(row):
                continue
            name = line.semantic_name(row)
            if not name:
                continue
            domain_name = _clean_str(row.get("domain_name"))
            grouped.setdefault((domain_name, name), []).append(row)
        return {key: _dedupe_members(members) for key, members in grouped.items()}

    # -- LLM 阶段 --------------------------------------------------------

    def _payload_for(
        self, members: _Members, keys: list[tuple[str, str]], line
    ) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for domain_name, name in keys:
            group = members[(domain_name, name)]
            item = {
                "domain_name": domain_name,
                line.name_field: name,
                "table_count": len(_tables_of(group)),
                "total_query_count": _total_query_count(group),
                "columns": _columns_of(group),
                "column_comments": _comments_of(group),
            }
            item.update(line.extra_payload(group))
            payload.append(item)
        return payload

    async def _describe(self, members: _Members, line) -> dict[tuple[str, str], str]:
        keys = sorted(members)
        batches = [
            keys[start : start + self._batch_size]
            for start in range(0, len(keys), self._batch_size)
        ]
        units = [
            LlmUnit(
                label=f"dict:{line.name_field}:describe:{index + 1}/{len(batches)}",
                system_prompt=line.build_system_prompt(self._language),
                user_prompt=line.build_user_prompt(
                    self._payload_for(members, batch, line), self._language
                ),
            )
            for index, batch in enumerate(batches)
        ]
        described: dict[tuple[str, str], str] = {}
        responses = await self._runner.run_units(units)
        for batch, response in zip(batches, responses):
            if response is None:
                continue
            try:
                described.update(
                    validate_description_items(
                        extract_json_payload(response),
                        set(batch),
                        name_field=line.name_field,
                        description_field=line.description_field,
                        kind=line.kind,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "%s descriptions batch failed, fall back to comments: %s",
                    line.name_field,
                    exc,
                )
        return described

    async def _run_line(self, calibers: pd.DataFrame, line) -> pd.DataFrame:
        grouped = self._group(calibers, line)
        logger.info(
            "dict[%s]: %s items from %s columns",
            line.name_field,
            len(grouped),
            sum(len(m) for m in grouped.values()),
        )
        if not grouped:
            return pd.DataFrame()

        members = grouped

        if self._llm is not None and self._runner is not None:
            described = await self._describe(members, line)
        else:
            logger.warning(
                "dict[%s]: 未配置 LLM，跳过描述生成（D4 降级）", line.name_field
            )
            described = {}

        rows = []
        for (domain_name, name), group in sorted(members.items()):
            comments = _comments_of(group)
            description = described.get((domain_name, name)) or (
                comments[0] if comments else None
            )
            rows.append(line.final_row(domain_name, name, description, group))
        return pd.DataFrame(rows)

    # -- 主流程 ----------------------------------------------------------

    async def handle(self, evidence: TaskEvidence) -> DictSnapshot:
        calibers = (
            evidence.calibers.calibers
            if evidence.calibers is not None
            else pd.DataFrame()
        )
        if calibers.empty:
            logger.info("dict: 无口径结果，产出空快照")
            return DictSnapshot.build(None, None)

        dimensions = await self._run_line(calibers, _DimensionLine)
        metrics = await self._run_line(calibers, _MetricLine)
        return DictSnapshot.build(dimensions, metrics)


__all__ = ["DictHandler"]

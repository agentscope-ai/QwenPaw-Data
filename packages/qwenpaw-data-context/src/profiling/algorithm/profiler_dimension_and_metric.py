"""维度 / 指标语义层的 LLM 产物校验。

handler.py 中 DimensionManageTaskHandler / MetricManageTaskHandler 在把 LLM 响应写回黑板前，
用本模块的 validate_* 函数校验结构并规范化为 ``{(key...): 值}`` 映射。
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, float) and pd.isna(value):
        return default
    text = str(value).strip()
    return text if text else default


def validate_dimension_description_items(
    items: Any,
    expected_keys: set[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """校验 LLM 补充的维度描述，返回 ``{(domain_name, dimension_name): 描述}``。

    输出的结果示例::

        {("Sales", "行业"): "客户所属行业，用于按行业分析客户结构与收入分布",
         ("Sales", "地区"): "客户注册地所在省份，用于区域维度下钻"}

    Corner cases：
        - 不是数组、元素不是对象时抛 ``ValueError``；
        - ``expected_keys`` 有遗漏时抛 ``ValueError``，由 handler 兜底成列注释；
        - 模型多返回的组合直接忽略，不进入结果。
    """
    if not isinstance(items, list):
        raise ValueError("dimension description 输出必须是数组")

    described: dict[tuple[str, str], str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("dimension description 数组元素必须是对象")
        key = (
            _safe_str(item.get("domain_name")),
            _safe_str(item.get("dimension_name")),
        )
        if key not in expected_keys or key in described:
            continue
        described[key] = _safe_str(item.get("dimension_description"))

    missing = expected_keys - set(described)
    if missing:
        raise ValueError(f"以下维度未生成描述: {sorted(missing)[:10]}")
    return described


def validate_name_normalization_items(
    items: Any,
    expected_names: set[str],
) -> dict[str, str]:
    """校验跨表名称归一结果，返回 ``{原名称: 归一后名称}``。

    输出的结果示例::

        {"分区日期": "日期", "日期分区": "日期", "统计日期": "日期",
         "创建时间": "创建时间"}

    Corner cases：
        - 不是数组、元素不是对象时抛 ``ValueError``；
        - ``expected_names`` 有遗漏时抛 ``ValueError``，由 handler 兜底为不归一；
        - 模型多返回的名称直接忽略；``canonical_name`` 为空时退回原名。
    """
    if not isinstance(items, list):
        raise ValueError("name normalization 输出必须是数组")

    mapping: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("name normalization 数组元素必须是对象")
        name = _safe_str(item.get("name"))
        if name not in expected_names or name in mapping:
            continue
        mapping[name] = _safe_str(item.get("canonical_name")) or name

    missing = expected_names - set(mapping)
    if missing:
        raise ValueError(f"以下名称未给出归一结果: {sorted(missing)[:10]}")
    return mapping


def validate_metric_description_items(
    items: Any,
    expected_keys: set[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """校验 LLM 补充的指标描述，返回 ``{(domain_name, metric_name): 描述}``。

    输出的结果示例::

        {("sales", "成交额"): "当日成交总金额，用于收入监控与同比分析",
         ("sales", "日活跃用户数"): "当日去重活跃用户数"}

    Corner cases 与 :func:`validate_dimension_description_items` 一致：
        - 不是数组、元素不是对象时抛 ``ValueError``；
        - ``expected_keys`` 有遗漏时抛 ``ValueError``，由 handler 兜底成列注释；
        - 模型多返回的组合直接忽略。
    """
    if not isinstance(items, list):
        raise ValueError("metric description 输出必须是数组")

    described: dict[tuple[str, str], str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("metric description 数组元素必须是对象")
        key = (
            _safe_str(item.get("domain_name")),
            _safe_str(item.get("metric_name")),
        )
        if key not in expected_keys or key in described:
            continue
        described[key] = _safe_str(item.get("metric_description"))

    missing = expected_keys - set(described)
    if missing:
        raise ValueError(f"以下指标未生成描述: {sorted(missing)[:10]}")
    return described

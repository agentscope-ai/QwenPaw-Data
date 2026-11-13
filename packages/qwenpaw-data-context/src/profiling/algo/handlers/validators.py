"""LLM 响应校验器与值归一工具。

校验风格统一：结构非法 / 期望 key 有遗漏 → ``ValueError``（由
handler 决定降级），模型多返回的条目静默忽略。
"""

from __future__ import annotations

import ast
import re
from typing import Any

import pandas as pd


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, float) and pd.isna(value):
        return default
    text = str(value).strip()
    return text if text else default


# ===========================================================================
# dataset
# ===========================================================================


def validate_dataset_meta_items(
    items: Any,
    expected_sqls: set[str],
) -> list[dict[str, Any]]:
    """校验 LLM 生成的视图元信息，保证每条输入 SQL 都被覆盖。"""
    if not isinstance(items, list):
        raise ValueError("dataset metadata 输出必须是数组")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            raise ValueError("dataset metadata 数组元素必须是对象")

        sql = str(item.get("sql", "")).strip()
        if not sql:
            raise ValueError("dataset metadata 元素缺少 sql 字段")

        normalized.append(
            {
                "sql": sql,
                "parents": str(item.get("parents", "")).strip(),
                "name": str(item.get("name", "")).strip(),
                "description": str(item.get("description", "")).strip(),
            }
        )
        seen.add(sql)

    missing = expected_sqls - seen
    if missing:
        raise ValueError(f"以下 SQL 未生成 metadata: {len(missing)} 条")
    return normalized


def merge_dataset_views(
    df: pd.DataFrame,
    views: list[dict[str, Any]],
) -> pd.DataFrame:
    """将 LLM 生成的命名与描述合并回原始模板行（右侧追加 name/description）。"""
    if df.empty:
        return df.copy()

    view_map = {item["sql"]: item for item in views}
    enriched = df.copy()
    enriched["name"] = enriched["repeated_sql"].map(
        lambda sql: view_map.get(str(sql), {}).get("name", "")
    )
    enriched["description"] = enriched["repeated_sql"].map(
        lambda sql: view_map.get(str(sql), {}).get("description", "")
    )
    return enriched


# ===========================================================================
# name normalization / semantic matching / descriptions
# ===========================================================================


def validate_name_normalization_items(
    items: Any,
    expected_names: set[str],
) -> dict[str, str]:
    """严格校验名称匹配结果，返回 ``{原名称: 规范名称}``。

    semantic matcher 的响应是一次整批决策：额外名称、重复名称或非字符串
    canonical name 都说明模型没有遵守契约，必须让调用方整批降级，而不能
    静默忽略后留下半套映射。
    """
    if not isinstance(items, list):
        raise TypeError("name normalization 输出必须是数组")

    mapping: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise TypeError("name normalization 数组元素必须是对象")
        name_value = item.get("name")
        canonical_value = item.get("canonical_name")
        if not isinstance(name_value, str):
            raise TypeError("name normalization 的 name 必须是字符串")
        name = name_value.strip()
        if name_value != name:
            raise ValueError(f"name normalization 未原样返回名称: {name_value!r}")
        if name not in expected_names:
            raise ValueError(f"name normalization 返回了未知名称: {name!r}")
        if name in mapping:
            raise ValueError(f"name normalization 重复返回名称: {name!r}")
        if not isinstance(canonical_value, str) or not canonical_value.strip():
            raise TypeError(
                f"name normalization 的 canonical_name 必须是非空字符串: {name!r}"
            )
        mapping[name] = canonical_value.strip()

    missing = expected_names - set(mapping)
    if missing:
        raise ValueError(f"以下名称未给出归一结果: {sorted(missing)[:10]}")
    return mapping


def validate_description_items(
    items: Any,
    expected_keys: set[tuple[str, str]],
    *,
    name_field: str,
    description_field: str,
    kind: str,
) -> dict[tuple[str, str], str]:
    """校验 LLM 补充的描述，返回 ``{(domain_name, 业务名): 描述}``。

    旧版 dimension / metric 各有一份逐字相同的实现，仅字段名不同——合并成
    一个参数化函数，行为不变。
    """
    if not isinstance(items, list):
        raise ValueError(f"{kind} description 输出必须是数组")

    described: dict[tuple[str, str], str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{kind} description 数组元素必须是对象")
        key = (_safe_str(item.get("domain_name")), _safe_str(item.get(name_field)))
        if key not in expected_keys or key in described:
            continue
        described[key] = _safe_str(item.get(description_field))

    missing = expected_keys - set(described)
    if missing:
        raise ValueError(f"以下{kind}未生成描述: {sorted(missing)[:10]}")
    return described


# ===========================================================================
# 值归一（移植自 legacy handler.py 顶部私有函数）
# ===========================================================================


def as_int(value: Any, *, default: int = 0) -> int:
    """计数字段转 int；``None`` / ``NaN`` / 非数值退回默认值。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_confidence(value: Any, *, default: float = 0.0) -> float:
    """置信度归一到 ``[0, 1]``；解析不出数值时用默认值。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return min(1.0, max(0.0, number))


def as_source_list(value: Any) -> list[str]:
    """把 ``sources`` 归一化为字符串列表（兼容 CSV 回读的字面量串与 numpy）。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, (list, tuple, set)):
                return [str(item) for item in parsed]
        return [text]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    if hasattr(value, "tolist"):
        listed = value.tolist()
        if isinstance(listed, list):
            return [str(item) for item in listed]
    return [str(value)]


_QUOTED_TOKEN_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def as_enum_list(value: Any) -> list[str]:
    """把枚举字段归一为去重后的字符串列表。

    兼容三种落盘形态：numpy 数组 repr（按引号切分——**绝不能**用
    ``ast.literal_eval``，相邻字符串字面量会被静默拼接）、数值数组 repr、
    逗号拼接文本。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    elif hasattr(value, "tolist") and not isinstance(value, str):
        listed = value.tolist()
        items = [str(item) for item in listed] if isinstance(listed, list) else []
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        quoted = _QUOTED_TOKEN_RE.findall(text)
        if quoted:
            items = [single or double for single, double in quoted]
        else:
            items = re.split(r"[,\s]+", text)

    seen: list[str] = []
    for item in items:
        cleaned = str(item).strip().strip("'\"")
        if cleaned and cleaned.lower() != "nan" and cleaned not in seen:
            seen.append(cleaned)
    return seen


__all__ = [
    "as_confidence",
    "as_enum_list",
    "as_int",
    "as_source_list",
    "merge_dataset_views",
    "validate_dataset_meta_items",
    "validate_description_items",
    "validate_name_normalization_items",
]

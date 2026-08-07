"""Shared helpers for semantic-layer graph properties (API alignment)."""
from __future__ import annotations

import json
from typing import Any


def str_list(val: Any) -> list[str]:
    if not val:
        return []
    if isinstance(val, str):
        parts = [p.strip() for p in val.replace("|", ",").split(",") if p.strip()]
        return parts if "," in val or "|" in val else ([val.strip()] if val.strip() else [])
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val if x is not None and str(x).strip()]
    return [str(val)]


def metric_role_from_props(props: dict[str, Any]) -> str:
    """Map stored flags to API ``MetricSummary.role``."""
    if props.get("is_north_star"):
        return "north_star"
    if props.get("is_display_distribution"):
        return "display_distribution"
    if props.get("is_display"):
        return "display"
    role = str(props.get("role") or "").strip()
    if role in ("north_star", "display", "display_distribution"):
        return role
    return ""



def anomaly_rules_to_json(rules: Any) -> str:
    if not rules:
        return "[]"
    if isinstance(rules, str):
        return rules
    try:
        return json.dumps(rules, ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]"


def anomaly_rules_from_json(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    return []


def dim_value_rows(
    domain: str,
    dim_name: str,
    values: Any,
    *,
    default_occur: int = 0,
    datasource_id: str = "",
) -> list[dict[str, Any]]:
    """Build DimensionValue rows with optional occur_cnt (desc rank if only list)."""
    from .keys import dim_value_key

    if not values:
        return []
    rows: list[dict[str, Any]] = []
    if isinstance(values, list):
        n = len(values)
        for i, item in enumerate(values):
            if isinstance(item, dict):
                val = str(item.get("value") or item.get("dimension_value") or "")
                cnt = int(item.get("occur_cnt") or item.get("dimension_occur_cnt") or default_occur or (n - i))
            else:
                val = str(item)
                cnt = default_occur or max(1, n - i)
            if not val.strip():
                continue
            rows.append(
                {
                    "dv_key": dim_value_key(domain, dim_name, val, datasource_id),
                    "value": val,
                    "label": val,
                    "occur_cnt": cnt,
                }
            )
    return rows

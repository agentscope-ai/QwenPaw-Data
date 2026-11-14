"""ds 归属判定（mgw3-filter F1，画布/图 API datasource_id 过滤统一面）。

ds 过滤不能只认 ``datasource_id`` 属性等值：物理层 Table/Column 节点从不落
该属性（归属表达在 key 前缀 ``tbl:<datasource_id>:`` / ``col:<datasource_id>:``），
旧严格判定让 ds scope 下的画布整体丢失物理层，且与 usage_graph 统计路径的
IS NULL 容忍语义自相矛盾（见 specs/mgw3-filter.md §1 F1）。

统一三段式归属判定：

1. ``datasource_id`` 属性非空 → 属性等值判定；
2. 属性为空 + 物理 key 前缀（tbl:/col:）：
   - 新形 key（冒号分段 ``tbl:<ds>:<ns.table>``）→ ds 段前缀匹配；
   - 旧形 key（无冒号段，ds 不可考）→ 放行；
3. 属性为空 + 非物理 key → 无归属证据 → 放行。

阶段 2（ds 闭包重定义）后逻辑实体改经 Dataset 闭包判定，本模块保留物理段
与无证据放行语义，逻辑段由闭包谓词替换。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["node_in_datasource"]

_PHYSICAL_KEY_PREFIXES = ("tbl:", "col:")


def _key_declares_datasource(key: str, datasource_id: str) -> bool | None:
    """物理 key 前缀的 ds 声明。

    返回 ``True/False`` = 新形 key 声明 ds 且匹配/不匹配；
    ``None`` = 非物理 key，或旧形物理 key（无冒号段，ds 不可考，按放行处理）。
    """
    for prefix in _PHYSICAL_KEY_PREFIXES:
        if key.startswith(prefix):
            rest = key[len(prefix) :]
            if ":" in rest:
                return rest.split(":", 1)[0] == datasource_id
            return None
    return None


def node_in_datasource(
    props: Mapping[str, Any] | None,
    datasource_id: str,
) -> bool:
    """三段式归属判定（Python 侧：Holo 结构化路径 / 响应后过滤共用）。"""
    ds = (datasource_id or "").strip()
    if not ds:
        return True
    p = props or {}
    ds_attr = str(p.get("datasource_id") or "").strip()
    if ds_attr:
        return ds_attr == ds
    declared = _key_declares_datasource(str(p.get("key") or ""), ds)
    if declared is None:
        return True
    return declared


def ds_ownership_cypher(var):
    """Apply the same explicit-ID / physical-key / unowned rules in the DB."""
    key = f"coalesce({var}.key, '')"
    ds = f"trim(coalesce({var}.datasource_id, ''))"
    return (
        f"({ds} = $datasource_id OR ({ds} = '' AND ("
        f"NOT ({key} STARTS WITH 'tbl:' OR {key} STARTS WITH 'col:') "
        f"OR NOT substring({key}, 4) CONTAINS ':' "
        f"OR {key} STARTS WITH 'tbl:' + $datasource_id + ':' "
        f"OR {key} STARTS WITH 'col:' + $datasource_id + ':')))"
    )

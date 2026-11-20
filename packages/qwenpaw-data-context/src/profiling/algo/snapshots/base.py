"""快照的公共基础：协议、契约工具、值归一。

------------------------------------------------------------------------
本包是叶子
------------------------------------------------------------------------
``snapshots/`` **不 import algo 内任何其他模块**，也不依赖 sqlglot。
6 个快照类型的生产者横跨 ``inputs``（外部来源）与 handlers（任务产物）两层，
放在任何一边都会让另一边反向依赖，因此独立成包，所有人向下依赖。

------------------------------------------------------------------------
三方职责
------------------------------------------------------------------------
- **Snapshot**：我由哪些命名 frame 组成（结构知识，纯内存分解）
- **LocalStore**：一个 frame 怎么落盘（格式知识）
- **SnapshotContext**：什么时候存、什么时候读缓存（策略）

谁都不知道另外两个的细节。store 的协议因此不随快照类型增长 ——
它永远只需要 ``write(key, frame)``。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

import pandas as pd

logger = logging.getLogger(__name__)

#: ``column_enums`` 之类列表字段的元素上限；超出截断，避免 prompt 被撑爆。
MAX_LIST_ITEMS = 100


@runtime_checkable
class Snapshot(Protocol):
    """一份产出。frozen；结构知识归自己。"""

    def to_frames(self) -> Mapping[str, pd.DataFrame]:
        """分解成命名 frame。键即落盘的 key。"""
        ...

    @classmethod
    def from_frames(cls, frames: Mapping[str, pd.DataFrame]) -> Any:
        """由命名 frame 重建。缺 frame 时补带列空表，不抛。"""
        ...

    def is_empty(self) -> bool: ...


# ===========================================================================
# 契约工具
# ===========================================================================


def empty_frame(required: Sequence[str]) -> pd.DataFrame:
    """带必需列的空表。空结果也必须有列，否则下游 merge 会炸。"""
    return pd.DataFrame({name: pd.Series(dtype="object") for name in required})


def ensure_required(
    frame: pd.DataFrame | None,
    required: Sequence[str],
    *,
    label: str = "",
) -> pd.DataFrame:
    """补齐缺失的必需列（填 None），**保留全部额外列**，必需列排在前面。

    **非破坏性**：只加不减。契约是「必须有」不是「只能有」—— 入口丢信息
    不可逆，而下游要不要用是下游的事。

    缺列时记 warning：来源忘了产出某一列是真实的 bug（PG 的主键列曾经就
    这样静默丢过），不该靠悄悄补 None 掩盖。
    """
    if frame is None or len(frame) == 0:
        return empty_frame(required)

    out = frame.copy()
    missing = [name for name in required if name not in out.columns]
    if missing:
        logger.warning(
            "%s缺少必需列 %s，已补空值 —— 请检查来源是否遗漏了字段",
            f"{label} " if label else "",
            missing,
        )
        for name in missing:
            out[name] = None

    extras = [c for c in out.columns if c not in required]
    return out[list(required) + extras].reset_index(drop=True)


def frames_equal(left: pd.DataFrame | None, right: pd.DataFrame | None) -> bool:
    """往返保真断言用。列序、dtype 差异忽略，值与形状必须一致。"""
    if left is None or right is None:
        return left is right
    if len(left) != len(right) or set(left.columns) != set(right.columns):
        return False
    ordered = right[list(left.columns)]
    for column in left.columns:
        a, b = list(left[column]), list(ordered[column])
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if isinstance(x, (list, tuple)) or isinstance(y, (list, tuple)):
                if list(x or []) != list(y or []):
                    return False
            elif pd.isna(x) and pd.isna(y):
                continue
            elif x != y:
                return False
    return True


# ===========================================================================
# 值归一（导出给外部 schema 的适配器与回读侧；快照内部不自动调用）
# ===========================================================================


def as_yn(value: Any) -> str:
    """归一为 ``"Y"`` / ``"N"``。识别不了按 ``"N"``。"""
    if isinstance(value, str):
        return "Y" if value.strip().upper() in {"Y", "YES", "TRUE", "T", "1"} else "N"
    if value is None:
        return "N"
    try:
        if pd.isna(value):
            return "N"
    except (TypeError, ValueError):
        pass
    return "Y" if bool(value) else "N"


# TODO: 当前的前端要求其实是将str使用"$$$"来进行连接，这些都呕可以进行适配
def as_str_list(value: Any, *, limit: int = MAX_LIST_ITEMS) -> list[str]:
    """归一为 ``list[str]``，去重保序、丢 nan、截断。

    parquet 原生保 list，因此不需要旧版那套解析 numpy repr 的启发式代码
    （``['a' 'b']`` 用 ``literal_eval`` 会静默拼成 ``["ab"]``）。
    但 parquet 把 ``list<string>`` **读回成 numpy array**，所以回读侧仍要过
    一次本函数 —— 那是确定性转换，不是靠猜。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        items: list[Any] = text.split(",") if text else []
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    elif hasattr(value, "tolist"):
        listed = value.tolist()
        items = listed if isinstance(listed, list) else [listed]
    else:
        try:
            if pd.isna(value):
                return []
        except (TypeError, ValueError):
            pass
        items = [value]

    seen: list[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if not text or text.lower() == "nan" or text in seen:
            continue
        seen.append(text)
        if len(seen) >= limit:
            break
    return seen


# TODO: 不应该如此处理，溢出不等价于None
def as_int_or_none(value: Any) -> int | None:
    """计数；``"100+"`` 这类溢出标记归一为 ``None``。"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
        return None
    try:
        if pd.isna(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# TODO: 本质上其实就是转float
def as_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "MAX_LIST_ITEMS",
    "Snapshot",
    "as_float_or_none",
    "as_int_or_none",
    "as_str_list",
    "as_yn",
    "empty_frame",
    "ensure_required",
    "frames_equal",
]

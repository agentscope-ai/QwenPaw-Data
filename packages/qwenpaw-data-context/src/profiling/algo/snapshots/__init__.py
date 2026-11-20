"""6 个产出类型。**本包是叶子** —— 不 import algo 内任何其他模块。

::

    TableSnapshot         table_meta + column_meta__{名}    ← 物理表与 dataset 共用
    RawQueryLogSnapshot   raw_logs                          ← 原始 SQL 文本
    QueryLogSnapshot      evidence__{kind} × 4 + failed     ← 解析后的证据
    DomainSnapshot        domains + members
    CaliberSnapshot       calibers
    DictSnapshot          dimensions + metrics

统一协议：``to_frames()`` / ``from_frames()`` / ``is_empty()``。
结构知识归 snapshot，格式知识归 store，落盘时机归 context ——
store 的协议因此不随快照类型增长。

------------------------------------------------------------------------
门面 = 「读产物所需」
------------------------------------------------------------------------
产物类型、frame 的键、词表——编排层投影 ``Candidate*`` 就靠这些。

**契约常量与归一工具不在门面**：``REQUIRED_*`` / ``*_PREFIX`` /
``as_*`` / ``conform_*`` / ``empty_*`` / ``ensure_required`` /
``MAX_LIST_ITEMS`` 只有**写生产者**的人需要，去对应的 snapshot 模块拿
（``from ..snapshots.table import REQUIRED_COLUMN_META``）。
在这里再转发一遍，就是给同一个符号开了第二条 import 路径。
"""

from __future__ import annotations

from .base import Snapshot, frames_equal
from .caliber import CALIBERS_KEY, CaliberSnapshot, ColumnRole
from .dictionary import DIMENSIONS_KEY, METRICS_KEY, DictSnapshot
from .domain import DOMAINS_KEY, MEMBERS_KEY, DomainSnapshot
from .query_log import EVIDENCE_KINDS, QueryLogSnapshot, RawQueryLogSnapshot
from .table import (
    COLUMN_META_PREFIX,
    TABLE_META_KEY,
    DatasetSnapshot,
    TableSnapshot,
)

__all__ = [
    # 产物类型
    "Snapshot",
    "CaliberSnapshot",
    "DatasetSnapshot",
    "DictSnapshot",
    "DomainSnapshot",
    "QueryLogSnapshot",
    "RawQueryLogSnapshot",
    "TableSnapshot",
    # 产物 frame 的键 —— 编排层按它取帧
    "CALIBERS_KEY",
    "COLUMN_META_PREFIX",
    "DIMENSIONS_KEY",
    "DOMAINS_KEY",
    "EVIDENCE_KINDS",
    "MEMBERS_KEY",
    "METRICS_KEY",
    "TABLE_META_KEY",
    # 词表与比较
    "ColumnRole",
    "frames_equal",
]

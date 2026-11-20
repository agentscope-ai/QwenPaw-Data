"""表层快照：物理表与逻辑视图**共用同一个类型**。

------------------------------------------------------------------------
为什么 DatasetSnapshot 不是另一个类
------------------------------------------------------------------------
CALIBER 接的表层可以是物理表也可以是 dataset，两者必须产出同一形状 ——
否则消费者就得区分来源，「表层可替换」这条设计就白搭了。

dataset 多出来的 ``sql_content`` / ``parents`` / ``domain_name`` 走
「额外列一律保留」，不需要新类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Container, Mapping, TypeAlias

import pandas as pd

from .base import (
    as_int_or_none,
    as_str_list,
    as_yn,
    empty_frame,
    ensure_required,
)

# ===========================================================================
# 最小契约：「必须有」，不是「只能有」
# ===========================================================================

#: 表清单。主键恒为 table_name —— 物理表填表名，dataset 填 dataset 名。
#: 算法层全程只说 table_name；dataset_name 只出现在编排层的投影里。
REQUIRED_TABLE_META: tuple[str, ...] = (
    "table_name",
    "table_comment",
)

#: v2.1：column_name_cn / column_enums / column_distinct_num 退出必需列 ——
#: 业务语义归 L3 dataset_field.description，样例值归 P3 sample_values；
#: 枚举探查开启时 column_enums / column_distinct_num 仍作为**额外列**保留。
REQUIRED_COLUMN_META: tuple[str, ...] = (
    "table_name",
    "column_name",
    "data_type",
    "is_primary",  # "Y" / "N"
    "is_nullable",  # "Y" / "N"
    "column_comment",
)

TABLE_META_KEY = "table_meta"
COLUMN_META_PREFIX = "column_meta__"


def empty_table_meta() -> pd.DataFrame:
    return empty_frame(REQUIRED_TABLE_META)


def empty_column_meta() -> pd.DataFrame:
    return empty_frame(REQUIRED_COLUMN_META)


def conform_table_meta(frame: pd.DataFrame | None) -> pd.DataFrame:
    """补必需列 + 类型归一，保留额外列。

    **只给外部 schema 的适配器与 parquet 回读用。** 来源自己产出正确形状
    （PG 的 SQL 直接 ``AS is_primary``），不靠事后改装。
    """
    out = ensure_required(frame, REQUIRED_TABLE_META, label="表元数据")
    if len(out):
        out["table_name"] = out["table_name"].astype(str)
    return out


def conform_column_meta(
    frame: pd.DataFrame | None,
    table_name: str | None = None,
) -> pd.DataFrame:
    """同上。列元数据版。"""
    out = ensure_required(frame, REQUIRED_COLUMN_META, label="列元数据")
    if not len(out):
        return out
    if table_name is not None:
        out["table_name"] = str(table_name)
    out["table_name"] = out["table_name"].astype(str)
    out["column_name"] = out["column_name"].astype(str)
    out["is_primary"] = out["is_primary"].map(as_yn)
    out["is_nullable"] = out["is_nullable"].map(as_yn)
    # 枚举列是额外列（探查开启时才有）：存在才归一，不再兜底创建
    if "column_enums" in out.columns:
        out["column_enums"] = out["column_enums"].map(as_str_list)
    if "column_distinct_num" in out.columns:
        out["column_distinct_num"] = out["column_distinct_num"].map(as_int_or_none)
    return out


# ===========================================================================
# 快照
# ===========================================================================


@dataclass(frozen=True)
class TableSnapshot:
    """某一时刻的表元数据 + 每张表的列元数据。

    「快照」而非「目录」：frozen，是 ``fetch()`` 返回那一刻的完整事实，
    不是可查询的活对象。
    """

    table_meta: pd.DataFrame = field(default_factory=empty_table_meta)
    column_meta: Mapping[str, pd.DataFrame] = field(default_factory=dict)

    # -- Snapshot 协议 ---------------------------------------------------

    def to_frames(self) -> Mapping[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {TABLE_META_KEY: self.table_meta}
        for name, frame in self.column_meta.items():
            frames[f"{COLUMN_META_PREFIX}{name}"] = frame
        return frames

    @classmethod
    def from_frames(cls, frames: Mapping[str, pd.DataFrame]) -> TableSnapshot:
        """回读后过一次 ``conform_*`` —— parquet 把 list 读回成 numpy array。"""
        table_meta = conform_table_meta(frames.get(TABLE_META_KEY))
        column_meta: dict[str, pd.DataFrame] = {}
        for key, frame in frames.items():
            if not key.startswith(COLUMN_META_PREFIX):
                continue
            name = key[len(COLUMN_META_PREFIX) :]
            column_meta[name] = conform_column_meta(frame, table_name=name)
        return cls(table_meta=table_meta, column_meta=column_meta)

    def is_empty(self) -> bool:
        return len(self.names()) == 0

    # -- 组装 ≠ 改装 ------------------------------------------------------

    @classmethod
    def build(
        cls,
        table_meta: pd.DataFrame | None,
        column_meta: Mapping[str, pd.DataFrame] | None = None,
    ) -> TableSnapshot:
        """**只补齐必需列并 warning**，不改名、不转值、不丢额外列。

        与 ``conform_*`` 的区别是刻意的：组装不等于改装。来源产出什么就是
        什么，缺必需列时把问题指回来源，而不是在这里悄悄抹平。
        """
        clean = ensure_required(table_meta, REQUIRED_TABLE_META, label="表元数据")
        if len(clean):
            clean["table_name"] = clean["table_name"].astype(str)
        known = set(clean["table_name"]) if len(clean) else set()
        return cls(
            table_meta=clean,
            column_meta={
                str(name): ensure_required(
                    frame, REQUIRED_COLUMN_META, label=f"列元数据[{name}]"
                )
                for name, frame in (column_meta or {}).items()
                if str(name) in known and frame is not None and len(frame)
            },
        )

    # -- 读 --------------------------------------------------------------

    def names(self) -> frozenset[str]:
        if self.table_meta is None or len(self.table_meta) == 0:
            return frozenset()
        return frozenset(self.table_meta["table_name"].astype(str))

    def column_meta_of(self, table_name: str) -> pd.DataFrame:
        """某张表的列元数据；没有则返回带必需列的空表。"""
        frame = self.column_meta.get(str(table_name))
        return frame if frame is not None else empty_column_meta()

    def all_column_meta(self) -> pd.DataFrame:
        """全部表的列元数据纵向拼接。各表额外列不同时按并集对齐。"""
        frames = [f for f in self.column_meta.values() if f is not None and len(f)]
        return pd.concat(frames, ignore_index=True) if frames else empty_column_meta()

    def restricted_to(self, names: Container[str]) -> TableSnapshot:
        if self.table_meta is None or len(self.table_meta) == 0:
            return TableSnapshot()
        keep = self.table_meta[self.table_meta["table_name"].astype(str).isin(names)]
        return TableSnapshot(
            table_meta=keep.reset_index(drop=True),
            column_meta={k: v for k, v in self.column_meta.items() if k in names},
        )


#: 语义别名。dataset 层用它，但类型与物理层完全相同 —— 这正是
#: 「表层可替换」的前提。同 ``DatasetTableInput = StaticTableInput``。
DatasetSnapshot: TypeAlias = TableSnapshot


__all__ = [
    "COLUMN_META_PREFIX",
    "REQUIRED_COLUMN_META",
    "REQUIRED_TABLE_META",
    "TABLE_META_KEY",
    "DatasetSnapshot",
    "TableSnapshot",
    "conform_column_meta",
    "conform_table_meta",
    "empty_column_meta",
    "empty_table_meta",
]

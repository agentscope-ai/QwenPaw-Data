"""语义词典快照：维度库与指标库。

``columns_dict`` 而非 ``columns_manage``：它产出的是**词典**，不是「管理」
这个动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import pandas as pd

from .base import as_str_list, empty_frame, ensure_required

DIMENSIONS_KEY = "dimensions"
METRICS_KEY = "metrics"

#: 跨表聚合后的业务维度。synonyms 是聚到一起的物理列名。
#: v2.1：enums 退役（样例值下沉到绑定层 value_example），新增 dimension_role。
REQUIRED_DIMENSIONS: tuple[str, ...] = (
    "domain_name",
    "dimension_name",
    "description",
    "synonyms",  # list[str]：来源物理列名
    "dimension_role",  # categorical / time（自动档，规则推断）
    "tables",  # list[str]：出现在哪些表
)

REQUIRED_METRICS: tuple[str, ...] = (
    "domain_name",
    "metric_name",
    "description",
    "synonyms",  # list[str]
    "formulas",  # list[str]：口径表达式（expr）
    "time_windows",  # list[str]：出现过的统计窗口（1d/7d/30d/mtd）
    "tables",  # list[str]
)


@dataclass(frozen=True)
class DictSnapshot:
    """两张表：主键分别是 (domain, dimension_name) 与 (domain, metric_name)。

    与 DomainSnapshot 同理——主键不同，合并不了，也不该合并。
    """

    dimensions: pd.DataFrame = field(
        default_factory=lambda: empty_frame(REQUIRED_DIMENSIONS)
    )
    metrics: pd.DataFrame = field(default_factory=lambda: empty_frame(REQUIRED_METRICS))

    def to_frames(self) -> Mapping[str, pd.DataFrame]:
        return {DIMENSIONS_KEY: self.dimensions, METRICS_KEY: self.metrics}

    @classmethod
    def from_frames(cls, frames: Mapping[str, pd.DataFrame]) -> DictSnapshot:
        return cls.build(frames.get(DIMENSIONS_KEY), frames.get(METRICS_KEY))

    @classmethod
    def build(
        cls,
        dimensions: pd.DataFrame | None,
        metrics: pd.DataFrame | None,
    ) -> DictSnapshot:
        dims = ensure_required(dimensions, REQUIRED_DIMENSIONS, label="维度库")
        mets = ensure_required(metrics, REQUIRED_METRICS, label="指标库")
        # 列表字段过一次归一：parquet 回读是 numpy array
        for frame, columns in (
            (dims, ("synonyms", "tables")),
            (mets, ("synonyms", "formulas", "time_windows", "tables")),
        ):
            if len(frame):
                for column in columns:
                    frame[column] = frame[column].map(as_str_list)
        return cls(dimensions=dims, metrics=mets)

    def is_empty(self) -> bool:
        return len(self.dimensions) == 0 and len(self.metrics) == 0


__all__ = [
    "DIMENSIONS_KEY",
    "METRICS_KEY",
    "REQUIRED_DIMENSIONS",
    "REQUIRED_METRICS",
    "DictSnapshot",
]

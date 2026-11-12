"""列口径快照：一次判定同时定完维度 / 指标 / 日期。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

import pandas as pd

from .base import as_float_or_none, empty_frame, ensure_required


class ColumnRole(str, Enum):
    """列角色。**一次 LLM 调用同时决定维度和指标** —— 这正是维度口径与
    指标口径能合并成一个 CALIBER 任务的原因。
    """

    DIMENSION = "dimension"
    METRIC = "metric"
    DATE = "date"
    OTHER = "other"
    #: 日志里抽出的派生指标候选，但无业务保留价值（COUNT(*)、MAX(ds) 等）
    DISCARDED = "discarded"


#: 指标时间窗口取值域（MG v2.1 L7：1d/7d/30d/mtd，进 metric_caliber 唯一键）。
METRIC_TIME_WINDOWS: tuple[str, ...] = ("1d", "7d", "30d", "mtd")

CALIBERS_KEY = "calibers"

REQUIRED_CALIBERS: tuple[str, ...] = (
    "table_name",  # 归属的 dataset（物理表直通时即物理表名）
    "column_name",
    "domain_name",
    "column_role",
    "dimension_name",  # role == dimension 时有值
    "value_example",  # role == dimension 时的样例值（list[str]，投影层 $$$ 拼接）
    "metric_name",  # role == metric 时有值
    "time_window",  # role == metric 时有值（∈ METRIC_TIME_WINDOWS）
    "expr",  # 口径表达式（SQL 片段或列名；D5 允许 prose）
    "additivity",  # role == metric 时有值（additive/semi_additive/non_additive）
    "role_reason",
    "role_confidence",
)


@dataclass(frozen=True)
class CaliberSnapshot:
    """每个 (表, 列) 一行的角色判定结果。

    单张表就够——列角色是列的属性，没有第二个主键维度。
    """

    calibers: pd.DataFrame = field(
        default_factory=lambda: empty_frame(REQUIRED_CALIBERS)
    )

    def to_frames(self) -> Mapping[str, pd.DataFrame]:
        return {CALIBERS_KEY: self.calibers}

    @classmethod
    def from_frames(cls, frames: Mapping[str, pd.DataFrame]) -> CaliberSnapshot:
        return cls.build(frames.get(CALIBERS_KEY))

    @classmethod
    def build(cls, calibers: pd.DataFrame | None) -> CaliberSnapshot:
        clean = ensure_required(calibers, REQUIRED_CALIBERS, label="列口径")
        if len(clean):
            clean["table_name"] = clean["table_name"].astype(str)
            clean["column_name"] = clean["column_name"].astype(str)
            clean["role_confidence"] = clean["role_confidence"].map(as_float_or_none)
        return cls(calibers=clean)

    def is_empty(self) -> bool:
        return len(self.calibers) == 0

    # -- 投影 ------------------------------------------------------------

    def of_role(self, role: ColumnRole | str) -> pd.DataFrame:
        """筛出某一角色的列。DICT 任务按此切分维度与指标两条聚合线。"""
        if len(self.calibers) == 0:
            return self.calibers
        value = role.value if isinstance(role, ColumnRole) else str(role)
        return self.calibers[
            self.calibers["column_role"].astype(str) == value
        ].reset_index(drop=True)

    def tables(self) -> frozenset[str]:
        """已判定过的表。续跑时用它算 pending。"""
        if len(self.calibers) == 0:
            return frozenset()
        return frozenset(self.calibers["table_name"].astype(str))


__all__ = [
    "CALIBERS_KEY",
    "METRIC_TIME_WINDOWS",
    "REQUIRED_CALIBERS",
    "CaliberSnapshot",
    "ColumnRole",
]

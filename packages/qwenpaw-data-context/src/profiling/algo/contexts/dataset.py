"""逻辑视图。DATASET 的产出，是 CALIBER 的**主工作集**。

快照类型与物理表**完全相同**（``DatasetSnapshot = TableSnapshot``）——
这正是「表层可替换」的前提：``dataset_derived_from_tables_enabled`` 直通的物理对象
与日志派生的逻辑视图在这里合流，CALIBER 不需要区分来源。
"""

from __future__ import annotations

from typing import Mapping

import pandas as pd

from ..evidence import TableEvidence
from ..snapshots import DatasetSnapshot, TableSnapshot
from ..tasks import ProfileTask
from .base import ArtifactContext


class DatasetContext(ArtifactContext[TableSnapshot]):
    """dataset 清单 + 继承来的列元数据（表式来源=物理列、SQL 式来源=投影列）。

    **列必须在 DATASET 环节继承好** —— 下游只认 dataset，不回头查物理表，
    这里没继承上的列就永远补不上了。
    """

    #: 默认名。装配层给 staged / confirmed 两个实例各自的全名
    name = "dataset"
    SUPPORTED_TASKS = frozenset({ProfileTask.CALIBER, ProfileTask.DICT})

    def empty(self) -> TableSnapshot:
        return TableSnapshot()

    def rebuild(self, frames: Mapping[str, pd.DataFrame]) -> TableSnapshot:
        return DatasetSnapshot.from_frames(frames)

    def evidence_for_task(self, task: ProfileTask) -> TableEvidence | None:
        if not self.supports(task):
            return None
        # CALIBER 要全量（列角色判定）；DICT 只用 table_meta 取域归属，
        # 但多给列元数据无害，且省一次分支。
        return TableEvidence(snapshot=self.current())


__all__ = ["DatasetContext"]

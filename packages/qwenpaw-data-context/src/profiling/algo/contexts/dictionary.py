"""语义词典。除自身 prior 外，为 CALIBER 提供只读正式实体目录。"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from ..evidence import SemanticCatalogEvidence
from ..snapshots import DictSnapshot
from ..tasks import ProfileTask
from .base import ArtifactContext, Lifecycle

_DIMENSION_CATALOG_COLUMNS = (
    "domain_name",
    "dimension_name",
    "description",
    "synonyms",
    "dimension_role",
)
_METRIC_CATALOG_COLUMNS = (
    "domain_name",
    "metric_name",
    "description",
    "synonyms",
)


class DictContext(ArtifactContext[DictSnapshot]):
    """维度库 + 指标库。

    ``columns_dict`` 而非 ``columns_manage``：产出的是**词典**，
    不是「管理」这个动作。
    """

    #: 默认名。装配层给 staged / confirmed 两个实例各自的全名
    name = "columns_dict"
    # DICT 自身两态作为 prior；CALIBER 只允许读取 confirmed 实例（见 supports）。
    SUPPORTED_TASKS = frozenset({ProfileTask.CALIBER, ProfileTask.DICT})

    def empty(self) -> DictSnapshot:
        return DictSnapshot()

    def rebuild(self, frames: Mapping[str, pd.DataFrame]) -> DictSnapshot:
        return DictSnapshot.from_frames(frames)

    def supports(self, task: ProfileTask) -> bool:
        """staged 词典绝不进入跨数据源复用目录。"""
        if task is ProfileTask.CALIBER and self.lifecycle is not Lifecycle.CONFIRMED:
            return False
        return super().supports(task)

    def evidence_for_task(
        self, task: ProfileTask
    ) -> SemanticCatalogEvidence | DictSnapshot | None:
        if not self.supports(task):
            return None
        snapshot = self.current()
        if task is ProfileTask.DICT:
            # 自身 prior 必须保留完整产物及 staged 生命周期列；CALIBER 的
            # 严格白名单仅适用于跨数据源复用目录，不能反向裁剪 DICT 历史。
            return snapshot
        # 显式白名单投影，防止 Dataset 级 tables/formulas/time_windows 以及未来
        # 新增的关系字段意外泄漏给 CALIBER。
        return SemanticCatalogEvidence(
            dimensions=snapshot.dimensions.loc[
                :, list(_DIMENSION_CATALOG_COLUMNS)
            ].copy(),
            metrics=snapshot.metrics.loc[:, list(_METRIC_CATALOG_COLUMNS)].copy(),
        )


__all__ = ["DictContext"]

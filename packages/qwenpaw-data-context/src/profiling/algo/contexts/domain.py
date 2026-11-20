"""业务域。DOMAIN 的产出，服务其后三个任务。"""

from __future__ import annotations

from typing import Mapping

import pandas as pd

from ..evidence import DomainEvidence
from ..snapshots import DomainSnapshot
from ..tasks import ProfileTask
from .base import ArtifactContext


class DomainContext(ArtifactContext[DomainSnapshot]):
    """域清单 + 成员归属。

    **权威优先**：用户在界面上改过的域归属必须压过本地快照，否则修改不生效
    而且不报错。
    """

    #: 默认名。装配层给 staged / confirmed 两个实例各自的全名
    name = "domain"
    #: DOMAIN 自身也在其列 —— 再生成时把上一轮的域定义与归属作为既有状态
    #: 交给 handler（装配层把自身产出对路由进 ``prior_*``，不与下游任务的
    #: ``domains`` 字段混淆）。
    SUPPORTED_TASKS = frozenset(
        {ProfileTask.DOMAIN, ProfileTask.DATASET, ProfileTask.CALIBER, ProfileTask.DICT}
    )

    def empty(self) -> DomainSnapshot:
        return DomainSnapshot()

    def rebuild(self, frames: Mapping[str, pd.DataFrame]) -> DomainSnapshot:
        return DomainSnapshot.from_frames(frames)

    def evidence_for_task(self, task: ProfileTask) -> DomainEvidence | None:
        if not self.supports(task):
            return None
        snapshot = self.current()
        # DICT 要按域分组做名称归一，需要域本身的描述与别名；DOMAIN 再生成
        # 需要完整的域定义（staged 侧还带行级 probe_status / source_type）；
        # DATASET 还需要正式目录，为缺少已确认归属的表补做受限分配；
        # formal / staging 的目录边界由 assemble 保留，不能混成可选目录。
        include_domains = task in (
            ProfileTask.DICT,
            ProfileTask.DOMAIN,
            ProfileTask.DATASET,
        )
        return DomainEvidence(
            members=snapshot.members,
            domains=snapshot.domains if include_domains else None,
        )


__all__ = ["DomainContext"]

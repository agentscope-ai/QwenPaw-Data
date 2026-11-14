"""列口径。CALIBER 的产出，服务自身重跑与 DICT。"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from ..evidence import CaliberEvidence
from ..snapshots import CaliberSnapshot
from ..tasks import ProfileTask
from .base import ArtifactContext, Lifecycle


class CaliberContext(ArtifactContext[CaliberSnapshot]):
    """每个 (表, 列) 一行的角色判定。

    维度与指标在**同一张表**里，靠 ``column_role`` 区分 —— 一次 LLM 调用
    同时定完，这正是维度口径与指标口径能合并成一个任务的原因。

    ------------------------------------------------------------------------
    两种消费方式
    ------------------------------------------------------------------------
    CALIBER 自己读取 staged / confirmed 两态 prior，用来锁住当前数据源已
    确认的关系。DICT 则消费 evidence 层按自然键合并的 formal + staging
    CONFIRMED 关系；PENDING / SKIPPED 不会向下游暴露。
    """

    #: 默认名。装配层给 staged / confirmed 两个实例各自的全名
    name = "columns_caliber"
    # CALIBER 自身也需要读取两态 prior，用于锁住当前数据源已经确认的关系；
    # DICT 则消费两态经 evidence 层合并后的 confirmed-only 关系。
    SUPPORTED_TASKS = frozenset({ProfileTask.CALIBER, ProfileTask.DICT})

    def empty(self) -> CaliberSnapshot:
        return CaliberSnapshot()

    def rebuild(self, frames: Mapping[str, pd.DataFrame]) -> CaliberSnapshot:
        return CaliberSnapshot.from_frames(frames)

    async def load(self, *, refresh: bool = False) -> CaliberSnapshot:
        if refresh or not self.is_loaded():
            # 在线重新加载可能回落旧缓存，不能继承上次 handler 产物的许可。
            # 普通 load 命中本轮内存结果时保留许可，供同一流水线的 DICT 使用。
            self._allow_unstatused = self._fact is None
        return await super().load(refresh=refresh)

    async def save(self, snapshot: CaliberSnapshot, *, emit: bool = True) -> None:
        # ``emit=False`` 只由在线 facts 刷新触发；这类 staged 快照若没有
        # probe_status 不能被当成已确认。正常 handler 输出（emit=True）和
        # 纯离线 context 则允许无状态快照在同一流水线内继续交给 DICT。
        self._allow_unstatused = emit or self._fact is None
        await super().save(snapshot, emit=emit)

    def evidence_for_task(self, task: ProfileTask) -> CaliberEvidence | None:
        if not self.supports(task):
            return None
        # 本轮刚生成的无状态 staged 只可串给 DICT；若再次运行 CALIBER，
        # 它仍是未审核草稿，不能反过来成为自己的 locked prior。
        allow_unstatused = self.lifecycle is Lifecycle.CONFIRMED or (
            task is ProfileTask.DICT
            and getattr(self, "_allow_unstatused", self._fact is None)
        )
        return CaliberEvidence(
            calibers=self.current().calibers,
            allow_unstatused=allow_unstatused,
        )


__all__ = ["CaliberContext"]

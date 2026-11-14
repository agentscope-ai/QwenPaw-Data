"""物理表层。只服务 DOMAIN 与 DATASET —— **CALIBER 之后不再碰物理表**。"""

from __future__ import annotations

import logging
from typing import Mapping

import pandas as pd

from ..evidence import TableEvidence
from ..snapshots import TableSnapshot
from ..tasks import ProfileTask
from .base import SourceContext

logger = logging.getLogger(__name__)


class SourceTableContext(SourceContext[TableSnapshot]):
    """远程库的表结构。

    DATASET 环节把物理表加工成 dataset 并**完整继承列元数据**，此后
    CALIBER / DICT 都在 dataset 之上工作。这条分界让 dataset 成为可插拔
    环节，也让旧版的 ``view_`` 前缀猜测整个消失。
    """

    name = "source_table"
    SUPPORTED_TASKS = frozenset({ProfileTask.DOMAIN, ProfileTask.DATASET})

    def empty(self) -> TableSnapshot:
        return TableSnapshot()

    def rebuild(self, frames: Mapping[str, pd.DataFrame]) -> TableSnapshot:
        return TableSnapshot.from_frames(frames)

    async def read_local(self) -> TableSnapshot | None:
        """回读后加一道结构校验：**有表无列的缓存按作废处理**。

        这种形态不是任何合法拉取的产物（SQL 表至少有一列），只可能来自
        陈旧/不完整的落盘（快照格式变更、半途中断）。指纹只校验 config，
        看不出内容残缺 —— 接受它会让直通 dataset 拿不到列，CALIBER 随之
        全空。作废后走 fetch 重拉自愈。
        """
        snapshot = await super().read_local()
        if (
            snapshot is not None
            and len(snapshot.table_meta)
            and not any(len(f) for f in snapshot.column_meta.values())
        ):
            logger.info("%s: 缓存有表无列（陈旧或不完整），作废重拉", self.name)
            return None
        return snapshot

    def evidence_for_task(self, task: ProfileTask) -> TableEvidence | None:
        if not self.supports(task):
            return None
        # DOMAIN 用表名 + 注释归域；DATASET 还要列元数据来继承
        return TableEvidence(snapshot=self.current())


__all__ = ["SourceTableContext"]

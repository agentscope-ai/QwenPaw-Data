"""原始查询日志。**不服务任何任务** —— 只喂 ``QueryLogContext``。

拆出它是为了**两级缓存各自失效**：采集逻辑一改（采集器是会经常改的东西），
只需重跑 parse，不必重新拉 10 万条日志——原始日志根本没变。
"""

from __future__ import annotations

from typing import Mapping

import pandas as pd

from ..snapshots import RawQueryLogSnapshot
from ..tasks import ProfileTask
from .base import SourceContext


class RawQueryLogContext(SourceContext[RawQueryLogSnapshot]):
    """从远程库拉回的 SQL 文本，未解析。"""

    name = "query_log_raw"
    SUPPORTED_TASKS = frozenset()  # 上游专用

    def empty(self) -> RawQueryLogSnapshot:
        return RawQueryLogSnapshot.build()

    def rebuild(self, frames: Mapping[str, pd.DataFrame]) -> RawQueryLogSnapshot:
        return RawQueryLogSnapshot.from_frames(frames)

    def evidence_for_task(self, task: ProfileTask) -> None:
        return None


__all__ = ["RawQueryLogContext"]

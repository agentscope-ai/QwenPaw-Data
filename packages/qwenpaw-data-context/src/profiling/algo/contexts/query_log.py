"""解析后的日志证据。服务全部四个任务。

------------------------------------------------------------------------
它是派生 context
------------------------------------------------------------------------
fetch 不查远程库，而是**读上游 RawQueryLogContext 再转换**。context 的
fetch 本来就只是「一个能产出 snapshot 的协程」，是查库、跑转换还是读另一个
context，对它没有区别。

parse 因此不必成为一个 task —— 它没有独立证据需求、没有 LLM、没有用户审核
阶段，塞进 TASK_ORDER 会在编排层的 6 stage 映射里多出一个没有归宿的成员。
"""

from __future__ import annotations

from typing import Mapping

import pandas as pd

from ..evidence import LogEvidence
from ..snapshots import EVIDENCE_KINDS, QueryLogSnapshot
from ..tasks import ProfileTask
from .base import SourceContext

#: 每个任务要哪几类证据。日志**永远是增强项**，缺失只降级不阻断。
LOG_KINDS_OF_TASK: dict[ProfileTask, tuple[str, ...]] = {
    ProfileTask.DOMAIN: ("join",),
    ProfileTask.DATASET: ("sql_template",),
    # 两类一起要：一次列角色判定同时定维度与指标
    ProfileTask.CALIBER: ("dimension_usage", "metric_expr"),
    # DICT 用同两类做用量统计，帮模型判断哪个业务名更常用
    ProfileTask.DICT: ("dimension_usage", "metric_expr"),
}


class QueryLogContext(SourceContext[QueryLogSnapshot]):
    """四类原始证据。**AST 不在这里** —— 缓存成果，不缓存中间态。"""

    name = "query_log"
    SUPPORTED_TASKS = frozenset(LOG_KINDS_OF_TASK)

    def empty(self) -> QueryLogSnapshot:
        return QueryLogSnapshot.build()

    def rebuild(self, frames: Mapping[str, pd.DataFrame]) -> QueryLogSnapshot:
        return QueryLogSnapshot.from_frames(frames)

    def evidence_for_task(self, task: ProfileTask) -> LogEvidence | None:
        kinds = LOG_KINDS_OF_TASK.get(task)
        if not kinds:
            return None
        snapshot = self.current()
        return LogEvidence(frames={kind: snapshot.of(kind) for kind in kinds})


__all__ = ["LOG_KINDS_OF_TASK", "QueryLogContext"]

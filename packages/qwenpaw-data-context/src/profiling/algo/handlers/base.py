"""handler 层的注入点协议。

``TaskHandler`` 是流水线第 ④ 步的形状：吃 ``TaskEvidence``、吐 snapshot，
落盘由流水线第 ⑤ 步统一做。住本包（而不是 ``algo_profiler``）：协议描述的
是 handler 长什么样，写新 handler 的人在本包里就能拿到全部依据；
``AlgoProfiler`` 只按协议查表调用，不定义它。
"""

from __future__ import annotations

from typing import Protocol

from ..evidence import TaskEvidence
from ..snapshots import Snapshot


class TaskHandler(Protocol):
    """任务实现。**产出一份 snapshot**，由 profiler 存回对应 context。

    默认实现在 ``handlers/``；测试可用任意满足本协议的桩。
    """

    async def handle(self, evidence: TaskEvidence) -> Snapshot: ...


__all__ = ["TaskHandler"]

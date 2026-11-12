"""取消与进度。**叶子模块** —— 不 import algo 内任何东西。

住这里而不住 ``profiler.py``：两个使用者都要它 —— ``AlgoContexts.build()``
把 ``report_progress`` 传给 parse（36652 条 SQL 是分钟级），``AlgoProfiler``
在任务循环里用两者。放进任一方都会让另一方反向依赖，成环。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


def _noop() -> None: ...
def _noop_progress(pct: int, message: str) -> None: ...


@dataclass(frozen=True)
class RunHooks:
    """取消与进度。

    ``raise_if_cancelled`` 必须能到达**长循环内部**：采集 10 万条 SQL、逐表
    调 LLM 都是分钟级，只在任务边界查一次等于不可取消。

    ``report_progress`` 是**同步**回调 —— algo 的 CPU 段跑在 ``to_thread``
    里，协程回调在那儿没法 await。编排层的协程回调由 provider 架桥
    （见 ``algo_provider._progress_bridge``）。
    """

    raise_if_cancelled: Callable[[], None] = _noop
    report_progress: Callable[[int, str], None] = _noop_progress


__all__ = ["RunHooks"]

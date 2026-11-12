"""语义探查算法层。

约定见 ``CLAUDE.md``，参数见 ``config.py``，测试见 ``tests/profiling/``（相对包根目录）。编排层通过
``profiling.provider`` 的契约调用本包（现成实现：``profiling.algo_provider``），
**算法侧不查库、不感知租户与权限**。

------------------------------------------------------------------------
分层
------------------------------------------------------------------------
::

    config.py      全部参数。**叶子** —— 不 import algo 内任何模块
    snapshots/     6 个产出类型。**叶子** —— 只 import config
    inputs/        外部来源（远程库 → 原始数据）。不引入 sqlglot
    parser/        原始 SQL → 四类证据（算法移植自 profiling/algorithm）
    stores.py      落盘去向：parquet / excel / emit / memory
    contexts/      7 个 context：管理 snapshot 的本地生命周期
    evidence.py    投影 → 装配 → 门禁
    handlers/      LLM handler 层：④ 的四个任务实现（算法移植自 algorithm）
    profiler.py    五步流水线 + 组合根

------------------------------------------------------------------------
五步流水线
------------------------------------------------------------------------
::

    ① 加载支持本任务的 context（并发）
    ② 各自 evidence_for_task 投影
    ③ 装配成具名 TaskEvidence + 门禁
    ④ handler 产出 snapshot          ← handlers/ 的 LLM 层
    ⑤ 存回本任务的 output context

上一个任务 ⑤ 存的，就是下一个任务 ① 读到的本地快照。

------------------------------------------------------------------------
包根只导出「驱动一次运行」所需
------------------------------------------------------------------------
其余一律走子模块，**不在包根开第二条路**：

    产物形状      ``profiling.algo.snapshots``   （``*_KEY`` / ``*Snapshot`` / ``ColumnRole``）
    落盘去向      ``profiling.algo.stores``
    证据装配      ``profiling.algo.evidence``
    context 容器  ``profiling.algo.contexts``
    任务映射      ``profiling.algo.tasks``       （``OUTPUT_CONTEXT``）

同一个符号两条 import 路径，迟早会分叉——上一版 35 个 re-export 里只有 4 个
被真正用过，而消费者真正需要的 ``*_KEY`` 一个都不在其中。

驱动一次运行::

    AlgoContexts.build(config, facts=…) → AlgoProfiler.build(contexts) → run_all()

快速自检::

    PYTHONPATH=src python tests/profiling/integration/algo/test_spider_sqlite.py
"""

from __future__ import annotations

from .config import AlgoConfig
from .algo_contexts import AlgoContexts
from .algo_profiler import AlgoProfiler, Outcome
from .hooks import RunHooks
from .tasks import TASK_ORDER, ProfileTask

__all__ = [
    "TASK_ORDER",
    "AlgoConfig",
    "AlgoContexts",
    "AlgoProfiler",
    "Outcome",
    "ProfileTask",
    "RunHooks",
]

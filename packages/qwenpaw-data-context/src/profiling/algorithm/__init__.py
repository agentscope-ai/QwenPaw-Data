"""语义探查算法实现（业务域 / 数据集 / 维度 / 指标）。

编排层（``profiling.services``）只通过 ``profiling.provider`` 的契约调用本包，
算法侧不查库、不感知租户与权限。

模块分工：
    - ``base``：任务枚举、证据值对象与 TablesContext / QueryLogsContext /
      LlmClient / ProfileTaskHandler / Profiler 五个抽象；
    - ``data_context`` / ``query_context``：源表黑板与查询日志上下文的 pandas 实现；
    - ``profiler_*``：各阶段从 SQL 日志与源表结构中抽取证据的自由函数；
    - ``handler``：六个阶段各自的 LLM 提示构造与响应解析策略；
    - ``factory``：组合根，把配置装配成可运行的 Profiler。
"""

from __future__ import annotations

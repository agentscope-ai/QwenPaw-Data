#!/usr/bin/env python3
"""按 ``docs/graph_topology_v4.md`` 把 NL2SQL 三层图谱灌入 Neo4j。

阶段（按依赖顺序）：

1. 约束 / 索引 (§5 + §13.3)
2. 物理层 (§3.1)：从 PG 反射出 Database/Schema/Table/Column
3. JOINS_ON 推断 (§7)：profile 白名单 + id-like 正则 + ds overlap + 手动 override
4. 语义层 (§3.2)：provider 栈（schema_auto / metrics_dict / 自定义）
5. 知识图：external_events.yaml → Event / Entity（v4 无 Policy / BRIDGES_TO）
6. 轨迹图 (§10)：trace_tasks.yaml（+ 可选 trace_bridges.yaml）→ Task/Step/ToolCall/Claim

用法：

    python scripts/setup/build_topology.py                            # appdata（从 NEO4J_DATABASE 推断）
    python scripts/setup/build_topology.py --drop-topology            # 先清空拓扑再灌
    python scripts/setup/build_topology.py --no-physical              # 跳过 PG 反射
    python scripts/setup/build_topology.py --list-profiles            # 列出所有内置 profile
    python scripts/setup/build_topology.py --list-semantic-providers  # 列出语义 provider
    python scripts/setup/build_topology.py --semantic-provider schema_auto,metrics_dict

``--dataset NAME`` 根据 DatasetProfile 自动决定 JOINS_ON 白名单、semantic provider 栈、
knowledge / trace 路径。profile 不存在时退化到 generic。

详见 ``docs/dataset_profiles.md``。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from context_manager.graph.runner import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

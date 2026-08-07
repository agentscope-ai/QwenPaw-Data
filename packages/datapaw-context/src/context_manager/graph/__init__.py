"""Default NL2SQL Graph RAG 图拓扑构建器。

按三层图（Metadata / Trace / Knowledge + 跨图边）灌入 Neo4j：

- :mod:`context_manager.graph.keys`           §2 ID 规范
- :mod:`context_manager.graph.schema_init`    §5 + §13.3 唯一约束 / 全文 / 向量 / 普通索引
- :mod:`context_manager.graph.profile`        v3.1 DatasetProfile（per-dataset 配置）
- :mod:`context_manager.graph.physical`       §3.1 / §6.1 物理层（Database/Schema/Table/Column）
- :mod:`context_manager.graph.joins`          §7 JOINS_ON 启发式 + profile 白名单
- :mod:`context_manager.graph.semantic`       §3.2 / §6.1 语义层（metrics_dict provider）
- :mod:`context_manager.graph.semantic_auto`  v3.1 schema_auto provider（通用自动派生）
- :mod:`context_manager.graph.knowledge`      v4 知识图：Event / Entity（跨图 SURFACE_METRIC 等）
- :mod:`context_manager.graph.trace`          §10 轨迹图：Task/Step/ToolCall/Claim
- :mod:`context_manager.graph.runner`         一键编排所有阶段

只用 ``MERGE``，反复跑幂等。
"""

from .keys import (
    DEFAULT_DB_ID,
    DEFAULT_SCHEMA,
    METADATA_ZONE,
    TRACE_ZONE,
    KNOWLEDGE_ZONE,
    SHARED_ZONE,
    caliber_key,
    column_key,
    database_key,
    derive_layer,
    dim_key,
    dim_value_key,
    domain_key,
    formula_key,
    metric_key,
    operator_key,
    schema_key,
    split_qualified_column,
    table_key,
)
from .profile import (
    DatasetProfile,
    get_profile,
    profile_for_dataset,
    register_profile,
    registered_profile_names,
)
from .runner import TopologyRunner, build_topology
from .semantic_pipeline import (
    SemanticStageInput,
    register_semantic_provider,
    run_semantic_stage,
    semantic_provider_names,
)

__all__ = [
    "DEFAULT_DB_ID",
    "DEFAULT_SCHEMA",
    "KNOWLEDGE_ZONE",
    "METADATA_ZONE",
    "SHARED_ZONE",
    "TRACE_ZONE",
    "DatasetProfile",
    "TopologyRunner",
    "SemanticStageInput",
    "build_topology",
    "caliber_key",
    "column_key",
    "database_key",
    "derive_layer",
    "dim_key",
    "dim_value_key",
    "domain_key",
    "formula_key",
    "get_profile",
    "metric_key",
    "operator_key",
    "profile_for_dataset",
    "register_profile",
    "register_semantic_provider",
    "registered_profile_names",
    "run_semantic_stage",
    "schema_key",
    "semantic_provider_names",
    "split_qualified_column",
    "table_key",
]

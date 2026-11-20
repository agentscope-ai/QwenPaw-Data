---
name: bi-semantic-layer-guide
description: 通过当前 Context Manager 图工具查询业务语义。需要指标定义、维度绑定、业务口径或消歧时参考；保留 OSS 指南名称以兼容已有分析流程。
---
# 业务语义查询指南

先加载 `cm-skills`，使用原生图读取，不把 App 的旧兼容工具作为默认入口。

- 已知术语：`search_graph_fulltext(text, node_types, domain_id)`。
- 只有业务描述：`search_graph_vector(text, node_types, domain_id)`。
- 查完整结构和授权范围：`get_graph_schema()`。
- 按 key 补齐指标口径、维度绑定及物理映射：`query_graph(cypher, params, limit)`，先加载对应 backend 方言。
- 真实业务数据及枚举探针：`execute_read(datasource_id, sql, max_rows, timeout_ms)`。

## 指标匹配

精确名称优先于同义词，名称相同但跨域时限定已确认 domain_id；排名和 lexical_matches 不代表口径正确。完整口径沿 Metric → MetricCaliber → Dataset 查询，核对 expr、time_window、additivity、is_default、单位和过滤条件，不能仅取搜索片段。

**no_match 必须拦截**：空候选/缺少口径表示定义缺失，不是值为 0 或无数据。换成有依据的精确查询仍未命中时向用户说明缺口，禁止猜测同名指标或用相似定义冒充。

## 指标属性与维度

保留返回的 is_north_star、is_display、is_display_distribution、priority 等语义属性；缺失则标空，不自行补默认业务事实。经共同 Dataset 查询可用 Dimension，再沿 MAPS_TO_FIELD 核对字段表达式、value_example 和 total_value_marker。示例不是全量枚举，需要时在同一 datasource_id 上只读 DISTINCT 探测。

若图保留贡献/展示标记，只使用明确允许贡献拆解的维度。合法绑定不证明两个维度统计总体等价；层级以实际 schema 和关系事实为准。

管理员同样只读当前身份个人图，不传 owner/workspace 参数扩权。SQL 引用的 Dataset、DatasetField、Table、Column 必须与已确认来源一致；truncated 表示未取全，错误不当作空结果。

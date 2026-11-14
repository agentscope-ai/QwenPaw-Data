# MCP

CM 以 `toolkit` 服务名挂载到 `/mcp/v1/cm`，使用无状态 Streamable HTTP。
初始化、ping 和工具列表可匿名访问；所有工具调用均需认证。

## 模块分工

- [server.py](server.py)：定义路径、解析请求身份、注册和挂载工具。
- [graph.py](graph.py)：图读取工具通过 Runtime 签发本人 read Scope；向量检索使用认证用户的 ModelService。
- [sql.py](sql.py)：三个 SQL 工具，通过 Runtime 签发读写 Scope、使用 SQL 执行池。

[认证入口](../api/authentication.py) 管理匿名发现白名单；
[生命周期](../api/lifespan.py) 管理 MCP session manager，与 HTTP 共用 Runtime。

## 工具

| 工具 | 用途 |
| --- | --- |
| `get_graph_schema` | 获取逻辑图结构、属性类型和实际后端。 |
| `query_graph` | 在本人授权范围执行原生 Neo4j Cypher 或完整 Holo AGE SQL，Neo4j 自动添加用户范围条件，Holo SQL 保持原样。 |
| `search_graph_vector` | 输入文本，自动生成查询向量，在本人图内跨类型召回相关节点。 |
| `search_graph_fulltext` | 输入普通关键词，在本人图内跨类型全文召回节点。 |
| `execute_read` | 执行 SELECT/WITH，返回行预览和 CSV 下载地址。 |
| `execute_create` | 执行 CREATE TABLE/VIEW，包括 CTAS。 |
| `execute_modify` | 执行 INSERT/UPDATE。 |

图后端由 Workspace 选择（Neo4j / Holo AGE）；SQL 数据源由 `datasource_id` 显式选择。
SQL 写入要求 Workspace 管理权限和数据源写权限，成功返回前完成提交。

两个检索工具在 Neo4j / Holo AGE 上均接受 `text`（1–8000 字符，非空白）、
`limit`（默认 20，范围 1–100）及可选 `node_types`（如 `["Metric", "Table"]`）。
`node_types` 不传或为 null 时搜索与过滤条件兼容的可搜索公开类型；指定列表则在检索前限制类型，
重复类型去重，空列表、未知类型、内部类型及不支持对应检索方式的类型报错。
返回结构沿用 `query_graph`，顶层增加 `searched_node_types`；非空结果的列为
`node_type`、`node` 和 `retrieval`；node 为公开属性 map（含 key），不包含 embedding 或 score。
结果遵守总行数与 1 MB 限制，`truncated` 由多取一条及响应体积判断；向量工具耗时包含 embedding。
检索编排归属 [GraphQueryService](../cypher/service.py)，Store 按全文原始分数或向量相似度降序返回，
并列时按类型名和 key 排序，无类型配额、跨索引分数归一化或混合融合。
找到节点后可用 `get_graph_schema` 和 `query_graph` 按 key 查关系。
向量工具内部生成查询向量，不返回向量，也不重建节点向量。
ModelService 检查工作区向量空间约定并关闭操作级客户端，沿用既有用量记录和缓存。
embedding client 仍遵守部署的输入截断上限。
模型未配置、约定冲突或 Provider 请求失败时明确报错，不降级为空结果。

参数、限制和返回结构以工具 description 为准，结果为 MCP text content 中的 JSON。
执行细节见 [图查询](../cypher/README.md) 和 [SQL](../sql/README.md)。

## 检索范围与依据

两个检索工具均支持可选的 `domain_id`、`datasource_id`，在排序和 Top K 截断前取交集。
这是本人图内的业务属性过滤，不改变授权范围，也不沿关系推断归属。

| 节点层次 | 节点类型 | 支持的过滤 |
| --- | --- | --- |
| Domain 层 | Domain、Metric、Dimension、VerifiedQuery | `domain_id` |
| Datasource 层 | DataSource、Table、Column | `datasource_id` |
| 同时连接两层 | Dataset、DatasetField、MetricCaliber | 两者 |
| 其他可检索节点 | Entity、Event、UserMemory | 均不支持 |

Table、Column 仅支持全文检索。`domain_id` 使用 Domain.domain_id 的值，匹配 Domain 的
`domain_id` 或其他域节点的 `domain`，不能传显示名称或图 key。`datasource_id` 使用
DataSource.datasource_id 的值，直接匹配节点自身的同名属性，不能传 DataSource 的图 key。
属性未设置的节点不会命中过滤。上述层次是当前检索契约，不是按 Schema 中是否存在可空的
`datasource_id` 属性机械分类；Metric、Dimension 的数据源归属须通过 `query_graph` 核对绑定。

省略 `node_types` 时只搜索与所给过滤条件兼容的可检索类型；显式传入不兼容类型则报错，
不忽略条件、不回退到全图。结果顶层 `searched_node_types` 列出实际搜索类型，包括空结果。
向量检索在调用 embedding 前完成类型和过滤参数校验。

每行在 `node_type`、公开属性 `node` 外增加 `retrieval`：

```json
{
  "method": "fulltext",
  "lexical_matches": [{"field": "synonyms", "relation": "exact"}]
}
```

`method` 为 `fulltext` 或 `vector`。字面比较仅做去除首尾空白和 casefold：
`name`、`datasource_name`、`metric_name`、`canonical_name` 全值相等，或 `synonyms`、`aliases`
某个元素相等时标记 `exact`；`description`、`comment` 包含完整查询文本时标记 `contains`。
仅比较公开属性；不调用模型、不进行分词推断。空列表表示没有上述字面证据，仍可为相关候选。
这不是底层评分解释或业务正确率，即使名称完全匹配也须核对定义、统计总体及绑定。
保持原有排序，不返回原始分数，不将 Neo4j/Holo 的排名解释为可横向比较的置信度。

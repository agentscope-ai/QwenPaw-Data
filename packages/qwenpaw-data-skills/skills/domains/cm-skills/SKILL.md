---
name: cm-skills
description: 使用 Context Manager 检索业务上下文并通过 MCP 执行业务 SQL。查询指标、口径、数据集、表列、维度、知识或已验证查询，以及编写、改写、排查 CM 图查询时加载；包含 Neo4j 与 Holo AGE 方言和完整查询配方。
---

# Context Manager

CM 负责业务语义和知识的构建、维护与检索，为 Agent 提供指标定义、计算口径、数据集、物理表列、维度、知识和已验证查询等上下文，并通过 MCP 提供图检索与业务 SQL 执行能力。Host 和业务 Skill 负责理解任务、制定计划、组织答案；本 Skill 负责 CM 工具的使用方式。

## 选择工具

| 信息或操作 | 工具 |
| --- | --- |
| 已知名称、术语或关键词，寻找候选节点 | `search_graph_fulltext` |
| 只有业务描述，不知道标准名称，寻找语义候选 | `search_graph_vector` |
| 确认图后端、节点、关系、属性类型和授权范围 | `get_graph_schema` |
| 按精确名称 / key 查询事实、关系、完整名单或聚合 | `query_graph` |
| 读取业务表数据 | `execute_read` |
| 用户要求创建业务表或视图 | `execute_create` |
| 用户要求插入或更新业务数据 | `execute_modify` |

按需要选工具，无需连续调用所有检索入口。搜索无需预查 schema；已有足够的节点属性时可以直接使用。调用参数、结果结构、限制、SQL 写入条件与通用错误处理见 [MCP 工具参考](references/mcp-tools.md)，按所用工具阅读对应章节。

## 编写图查询

`Skill` 只加载本文，不会自动加载引用文件。首次 `query_graph` 前必须按顺序执行：`get_graph_schema` → 根据 `backend` 用 `Read` 加载对应方言文件的语法部分 → 编写并执行查询。未成功加载前不调用 `query_graph`；只加载实际后端，配方也在同一文件中。

| backend | 查询前必须加载的文件 |
| --- | --- |
| `neo4j` | [references/neo4j.md](references/neo4j.md) |
| `holoage` | [references/holoage.md](references/holoage.md) |

同一 Workspace 可复用已加载的 schema 和方言；切换 Workspace 或后端后重新确认。标签、关系、属性和类型以 schema 为准，声明不代表已有数据或索引就绪。

## 确认检索范围

业务域与数据源是两个独立维度，不能相互替代：

| 对象 | 显示名称 | 用于过滤的身份 |
| --- | --- | --- |
| Domain | `name` | `domain_id`；其他业务域节点的 `domain` 属性存这个 ID |
| DataSource | `datasource_name` | `datasource_id`；来源节点的同名属性存这个 ID |

- 上下文已有确认的 ID 时直接复用，无需重复查列表。只有名称时，先搜索对应的 Domain / DataSource，或按名称精确查询，核对名称、描述并取得 ID；不将显示名或图 key 当作 ID。
- 业务域尚未明确时，可先搜索指标等候选，再用其 `domain` 查 Domain 核对业务含义；候选不等于已选定范围。数据源尚未确定时，保留已知域约束，从口径、数据集或 DataSource 返回的 `datasource_id` 发现候选。证据唯一且符合问题时采用，多候选先消歧，仍有歧义再询问用户。
- 搜索和图查询按下方类型规则沿用已确认的 ID；跨层范围通过口径、数据集或字段绑定核对。不将未知 ID 填为 null/空串参与等值过滤。

## SQL 操作的数据源一致性

**以已确认的 `datasource_id` 和业务域 ID 作为检索、消歧与选择节点的首要依据；用户已指定数据源时以其选择为准。不同数据源、不同业务域可能存在同名或相似节点，名称和搜索排名不能代替归属核对。**

**生成和执行 SQL 前，必须确认所依据的 `Table`、`Column`、`Dataset`、`DatasetField` 节点，其 `datasource_id` 属性均与已确认的目标数据源一致。** 此规则适用于 `execute_read`、`execute_create` 和 `execute_modify`。

- 搜索 `Domain`、`Metric`、`Dimension`、`VerifiedQuery` 只支持 `domain_id` 过滤；`DataSource`、`Table`、`Column` 只支持 `datasource_id`；`Dataset`、`DatasetField`、`MetricCaliber` 支持两者。完整类型表见 [MCP 参考](references/mcp-tools.md#搜索节点)。Metric/Dimension 的来源沿口径或字段绑定核对。
- 对 schema 中声明了 `domain` 的节点（如 `Metric`、`MetricCaliber`、`Dimension`、`Dataset`、`DatasetField`），按已确认的业务域过滤和核对。`Table`、`Column` 没有 `domain` 属性，通过语义对象和物理映射核对其业务用途，不虚构该属性。
- 两个 ID 都省略时才不做域/数据源过滤，查询文本不会自动选择范围。查看 `searched_node_types` 确认实际搜索类型；类型与过滤条件不兼容时修正类型或查询路径，不为消除报错而取消已确认范围。
- 使用图查询配方时，在相应节点上补齐业务域/数据源过滤并返回归属字段。搜索命中须按上述规则核对；属性未返回时按 key 补查。JOIN、子查询和复用已验证 SQL 时同样核对涉及的表、列、数据集及数据集字段，不能仅凭存在关联边或 SQL 连接正确就认定来源一致。
- 节点归属不一致、应核对的属性缺失或范围尚未确认时，不得将其作为本次 SQL 的依据。先在指定数据源和已确认业务域内重新检索；仍无法确认时说明缺失或冲突并向用户澄清，不能用其他数据源或业务域的同名节点替代。
- SQL 工具的 `datasource_id` 沿用已确认的目标数据源；用户已指定来源时，只有用户明确变更选择后才能切换，并重新核对相关对象。创建尚不存在的表或视图时，目标创建在该数据源中，源查询涉及的已有对象仍须按上述规则核对。

## 结果使用

- `retrieval.lexical_matches` 区分名称/别名精确匹配、描述包含与没有字面证据的相关候选；空列表不表示无关，精确匹配也不证明业务口径正确。匹配证据不参与重排，第一名不自动成为目标对象，搜索到的少量节点不能替代完整口径或字段列表。
- 同域近义指标或维度仍可能绑定不同数据集、字段和统计总体。按返回的 key 补查 `Metric → MetricCaliber → Dataset` 或 `Dimension → DatasetField`，对照问题核对定义、计算表达式、时间聚合及所需维度/汇总值。绑定合法不能证明两个口径等价；已有证据足以确定时无需继续检索。
- 搜索为空或只有无法确认的相关候选时，针对缺失信息选择下一步：已知标准名称，用域内精确名称/别名查询并检查绑定；只有业务描述，用向量寻找候选。同工具同参数已成功返回且没有新证据时不重复调用，也不轮流调用全文/向量来重复确认同一缺口。
- 精确查询仍缺少所需指标、口径或绑定时，说明缺失的事实。仅在已有定义和字段足以推导时给出依据并标明推导；统计总体、分母等存在会改变结果的歧义时向用户澄清，不用相似指标或通用默认规则填补。
- 检查 `truncated`，它只表示工具截断，不能识别查询自身 LIMIT 隐去的行。完整名单使用 `query_graph` 按配方分页；搜索不支持 offset。空结果、截断和执行失败分别处理，不能把失败当作没有数据。
- 图用于元数据与知识，业务表行通过 `execute_read` 获取。
- `execute_read` 的内联行只是预览；Host 返回 `file_path` 时读取 CSV 再分析，`truncated=true` 表示 CSV 也不完整。
- 图工具只读取当前用户的个人图，管理员也遵守该范围；查询文本或参数不能扩大权限。业务 SQL 的方言由所选数据源决定，与图后端无关；ODPS 查询继续遵循 `query-odps`。

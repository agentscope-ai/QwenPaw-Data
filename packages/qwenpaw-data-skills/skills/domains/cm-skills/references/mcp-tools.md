# CM MCP 工具参考

CM MCP 服务名为 `toolkit`，路径为 `/mcp/v1/cm`；工具调用需要认证。以下使用工具原名，客户端展示的名称可能带服务前缀。参数与能力以实际工具 schema 为准。

## 搜索节点

`search_graph_fulltext` 和 `search_graph_vector` 在两种图后端使用相同参数：

| 参数 | 约定 |
| --- | --- |
| `text` | 1–8000 字符且非空白；全文接受普通关键词，不是子串匹配或 Lucene 表达式；向量接受自然语言描述 |
| `limit` | 1–100，默认 20；跨类型合计的返回上限 |
| `node_types` | 可选类型列表，如 `["Metric"]`；省略或 null 搜索与所给过滤条件兼容的可检索公开类型。重复项去重，空列表、未知、内部、不支持该检索方式或与过滤条件不兼容的显式类型报错 |
| `domain_id` | 可选，默认 null；精确匹配 Domain.domain_id 或其他域节点的 domain。使用实际业务域 ID，不是 Domain.name 或图 key；1–512 字符，无首尾空白 |
| `datasource_id` | 可选，默认 null；精确匹配节点自身的 datasource_id，不沿关系推断归属。使用实际数据源 ID，不是图 key；1–512 字符，无首尾空白 |

类型与过滤条件在排序和 Top K 截断前生效；两个 ID 同时传入时取交集。两者都省略时搜索跨域、跨数据源的个人图，文本中的业务名称不会自动成为范围条件。

| 节点层次 | 类型 | 可用过滤 |
| --- | --- | --- |
| Domain 层 | Domain、Metric、Dimension、VerifiedQuery | domain_id |
| Datasource 层 | DataSource、Table、Column | datasource_id |
| 同时连接两层 | Dataset、DatasetField、MetricCaliber | 两者 |
| 其他可检索节点 | Entity、Event、UserMemory | 均不支持 |

Table、Column 仅支持全文。已知域和数据源时，Metric/Dimension 先按域检索，再核对绑定来源；同时给搜索传两个 ID 只会覆盖 Dataset、DatasetField、MetricCaliber，不能用来查 Metric/Dimension。

全文按原始全文分数降序，向量按相似度降序；不交错类型、不设类型配额或跨索引分数归一化。目标类型明确时传 `node_types`，类型不确定时省略。

两者不需要图名、索引名或身份参数。向量工具通过用户已配置的 embedding 模型生成查询向量，不重建节点向量；没有向量的节点无法命中，模型或 Provider 失败会报错。

返回 `{columns, rows, row_count, truncated, elapsed_ms, searched_node_types}`，最后一项列出实际搜索的类型，空结果时也返回。每行包含 `node_type`、`node` 和 `retrieval`；`node` 为公开属性对象，不含分数、embedding 或关系。结果约 1 MB 封顶，向量耗时包含 embedding 生成。搜索参数直接传给搜索工具，不放入 `query_graph.params`。

`retrieval.method` 为 `fulltext` 或 `vector`；`lexical_matches` 是 `{field, relation}` 列表。比较只做 trim/casefold：名称字段（name、datasource_name、metric_name、canonical_name）或 synonyms/aliases 元素全值相等时标记 `exact`；description/comment 包含完整查询时标记 `contains`。空列表表示没有上述字面证据。证据不参与排序，不解释底层评分，也不表示业务正确率；名称/别名匹配后仍按 [结果使用规则](../SKILL.md#结果使用) 核对业务含义。

调用示例（输入示意；domain-id、source-id 须替换为已确认的真实 ID）：

| 目的 | 工具与参数 |
| --- | --- |
| 域内找指标 | `search_graph_fulltext({"text":"GMV","node_types":["Metric"],"domain_id":"domain-id"})` |
| 指定来源找物理列 | `search_graph_fulltext({"text":"user_id","node_types":["Column"],"datasource_id":"source-id"})` |
| 描述找域内指标 | `search_graph_vector({"text":"衡量用户每天活跃程度的指标","node_types":["Metric"],"domain_id":"domain-id"})` |
| 同时限定域和来源找语义字段 | `search_graph_vector({"text":"用户选择的模型","node_types":["DatasetField"],"domain_id":"domain-id","datasource_id":"source-id"})` |

## 图结构

`get_graph_schema()` 返回 `{version, backend, zones, nodes, edges, query_scope}`：

- `nodes` / `edges` 声明公开属性、`property_types` 和拓扑；不提供实时数量、索引健康或非空保证。
- `query_scope` 给出当前 workspace 和 owner 范围；调用方不能通过提交其他身份扩大范围。
- Holo 另返回 `graphs`，包含可查询的确切 schema 名称；Neo4j 可全文检索的节点另含 `fulltext_index`。

## 原生图查询

`query_graph(cypher, params=null, limit=100)`：

- `cypher` 为一条只读查询，最多 50,000 字符。语言、绑定方式、准入规则和业务配方按 backend 读取 [Neo4j](neo4j.md) 或 [Holo AGE](holoage.md)。
- `params` 为 JSON 对象。查询中的业务值通过参数绑定，不能拼接用户输入。
- `limit` 范围 1–1000；查询中的 LIMIT 不会提高工具上限，例如查询需要 500 行时也应传 `limit=500`。结果约 1 MB 封顶，数据库执行超时 10 秒。
- 返回 `{columns, rows, row_count, truncated, elapsed_ms}`；行是以投影列名为 key 的对象，空结果的 `columns` / `rows` 均为空。驱动多取一行并结合体积预算判断截断。
- `truncated` 只反映工具的行数/体积上限；查询自身 LIMIT 截掉的行不可见。取全量时不在查询内设 LIMIT，使用工具 `limit`，按唯一 key 稳定排序并通过 SKIP 分页；每页将 offset 增加实际 row_count，直到 truncated=false。体积限制导致空页时先缩减投影或收窄范围。

## 业务 SQL

调用前遵循 [数据源一致性规则](../SKILL.md#sql-操作的数据源一致性)，按用户指定的数据源和问题相关的业务域核对节点。工具按传入 ID 选择连接，不会代替 Agent 核对所引用的图节点。

三个工具都显式接收 `datasource_id`、`sql`，以及可选参数 `max_rows=2000`（1–10000）、`timeout_ms=10000`（100–30000）。每次提交一条语句，最多 50,000 字符。

连接来自当前 Workspace 的数据源目录，不要求图中一定存在 DataSource 节点。支持 PostgreSQL、Hologres、MySQL、SQLite、BigQuery 和 ODPS。ODPS 使用数据源配置中的 AccessKey/STS 凭证连接，不传执行项目参数；跨项目表在 SQL 中写作 `project.table`。

### execute_read

仅接受只读 SELECT/WITH。返回 `{columns, rows, row_count, total_row_count, truncated, elapsed_ms, download_url}`：

- `rows` 是与 `columns` 对齐的数组，最多预览 20 行；`row_count` 是预览行数。
- CSV 最多包含 `max_rows` 行。`total_row_count` 优先采用驱动计数，否则使用抓取数量，可能只是下界。
- Host 用同一用户认证下载 CSV，并将 `download_url` 替换为 `file_path`；空结果没有下载地址。下载默认保留 24 小时，实际以部署配置为准。
- `timeout_ms` 用于 PostgreSQL/Hologres、MySQL、SQLite；ODPS/BigQuery 使用 SDK 的作业等待机制。

### execute_create / execute_modify

仅用于用户明确要求的业务数据写操作，需要 Workspace 管理权限和数据库写权限；SQLite 还需数据源配置 `read_only=false`。

| 工具 | 支持语句 | 不支持 |
| --- | --- | --- |
| `execute_create` | CREATE TABLE、CREATE VIEW、CTAS | CREATE INDEX、OR REPLACE、DROP、ALTER、TRUNCATE |
| `execute_modify` | INSERT、UPDATE，含受支持的 upsert、INSERT SELECT、RETURNING | WITH 开头的写入、DELETE、MERGE、DDL |

写前用 `execute_read` 检查目标及 UPDATE 的 WHERE 范围，成功后查询验证；结果不确定时先检查目标再决定是否重试。ODPS 建表应指定 LIFECYCLE，UPDATE 要求事务表；成功作业立即生效。

两者返回 `{columns, rows, row_count, truncated, elapsed_ms}`，不生成 CSV。`row_count` 为影响行数，未知时为 -1；`max_rows` 只限制返回行，不能限制实际写入量。成功返回前已提交整条语句，返回行被截断也不影响提交。写入超时设置用于 PostgreSQL/Hologres、SQLite；MySQL 和 ODPS/BigQuery 使用驱动或 SDK 的等待机制。

## 通用错误处理

| 情况 | 处理 |
| --- | --- |
| 成功但为空 | 核对精确名称、同义词、大小写和业务条件；搜索未命中某类型不证明该类型没有数据，不用模型先验补事实 |
| 有结果但无法确认业务含义 | 检查字面证据、定义及绑定；不要因排名靠前采用候选。无新证据时不重复相同成功请求 |
| 类型与过滤条件不兼容 | 按类型表选择可用参数；Metric/Dimension 的来源通过绑定核对，不靠取消已确认范围绕过错误 |
| `truncated=true` | 按入口提供的分页能力收窄或分批，未取齐前说明结果可能不完整 |
| 向量模型未配置 | 文本检索足以继续时改用全文或精确查询；否则报告缺少模型。其他 Provider 错误按原因处理 |
| 权限拒绝 / `Graph read is forbidden` / `Context runtime is unavailable` | 说明阻塞，按任务 Skill 处理，不反复重试 |
| `Graph query execution is busy` / `SQL execution is busy` | 可稍后重试一次 |
| 原生图查询语法、类型或准入错误 | 用 Database code、Message、Position 定位 SQL / Cypher 层，按方言规则修正并沿用到后续查询；保留已确认的域、来源和业务条件 |

同一条其他报错不连续重试超过一次。原生查询诊断中的 Position 若存在，是提交语句中从 1 开始的字符位置；未返回时不猜测。固定搜索按工具级错误处理，不要求或改写内部 SQL。通过准入不代表实例支持所有函数或已建好索引。

# Cypher

`GraphQueryService` 校验模型查询的只读语法和授权范围，再交给 Store 执行。
`query_graph` 的 cypher 参数在 Neo4j 接收原生 Cypher，在 Holo 接收完整 AGE SQL；
Service 为 Neo4j 注入用户范围条件；Holo SQL 保持原样。不改写 LIMIT、不推断返回列。

`get_graph_schema` 返回 version/backend/zones/nodes/edges/query_scope。
MCP 仅访问当前用户的完整个人图，管理员也不能通过查询中的用户 ID 扩大范围。
Holo 的 graphs 是 Store 已验证归属的个人 schema；管理员跨用户读取由相应业务 primitive 提供。
Neo4j 可全文检索的节点声明 fulltext_index，供模型选择真实索引名，不表示索引健康状态。

## 查询准入

- Neo4j：每个 MATCH 节点须命名；匿名定长关系由服务端补内部变量名（此时 WITH / RETURN
  须显式列出返回项，不支持投影 `*`）。服务端在每个 MATCH / OPTIONAL MATCH 注入
  workspace_id 和当前 owner_id 条件，并括号包裹已有 WHERE。隔离参数由服务端绑定，
  管理员同样只能读取自己的图，不包含共享 owner。UNWIND 和 UNION 的各分支沿用相同隔离。
- Holo：所有表引用（含 CTE/子查询）须属于授权 schema；不检查标签表是否出现在公开 schema 清单。
  `ag_catalog.cypher` 的图名和正文须是字面量，第三个参数为 JSON 对象字符串，可通过驱动绑定。
- Neo4j 全文仅开放 `db.index.fulltext.queryNodes(index,text)`，索引必须来自 Schema，
  服务端在 YIELD 处为返回节点注入 workspace_id/owner_id 条件；不允许 options 提前截断候选。
  向量使用受范围约束的 MATCH 加 `vector.similarity.cosine` / `euclidean`，再排序取前 k 条。
  两种检索后继续沿边查询时，节点、边由同一隔离器添加归属条件。
- Cypher 节点标签可省略，不做公开标签校验。Neo4j 不接受其他 CALL、图子查询、模式推导或变长关系；
  完整个人 Holo 图允许数据库支持的只读模式。两端均限制函数调用并拒绝写入和事务控制。

Store 执行前仍校验 Scope 与目标，原生查询本身不承担任意语句的访问范围分析。
Neo4j 使用 EXPLAIN 确认只读，Holo 使用只读事务，数据库查询超时均为 10 秒。

## 结果与限制

`GraphQueryService.search_keyword/search_vector` 提供固定的跨类型节点召回，直接使用
Store 的全文/向量搜索，不经过模型语句准入，也不改写 Cypher。向量入口借用 ModelService
生成查询向量，并将同步配置读取和数据库操作交给调用方提供的读执行器，不持有 Runtime。
MCP 校验文本、limit、node_types 和域/数据源 ID 的参数形状并签发本人 Scope，Store 在执行时实施范围约束。
node_types 可省略或为 null，此时搜索与过滤条件兼容的可搜索公开类型；非空列表在 Service 解析为 NodeSpec 后
传入 Store，只检索选中的类型。重复名称去重；未知、内部或不支持该检索方式的类型报错，
向量检索在调用 embedding 前校验类型与过滤参数。
`domain_id`、`datasource_id` 是可选的直接属性等值过滤，类型支持范围见
[MCP 检索契约](../mcp/README.md#检索范围与依据)。Service 将 Domain.domain_id 与其他域节点的
domain 映射为按类型的过滤条件，通过 Store.filters_by_type 在排序和截断前应用；不沿绑定关系推断。
显式类型与任一过滤条件不兼容即报错；两个条件取交集，不改变授权 Scope。
检索结果列为 `node_type`、`node`（公开属性 map，含 key）和 `retrieval`（检索方式与确定性字面证据），
顶层 `searched_node_types` 返回实际搜索类型。字面证据不等于底层评分依据或业务等价结论。
共享原生查询的字段投影、
行数截断及体积预算；不返回向量或分数。全文按原始分数降序，向量按相似度降序，不设类型配额。

查询最多 50,000 字符；默认 100、最多 1,000 行，结果预算约 1 MB。
驱动取 limit+1 行判断截断，空结果 columns/rows 为空；超时不等于整体请求 deadline。

Neo4j 原生图对象保留类型信息供字段投影。Holo 由 SQL 显式选列和转换：字符串 `::text`，
整数 `::bigint`，map/列表 `agtype_to_json`；未转换 agtype 保留驱动文本。
目标 Holo 的 agtype_to_json 不适用于整数，agtype varchar 转换不支持真实表查询。
隐藏字段投影不是保密边界，别名或表达式仍可读取当前授权图中的属性。

`cypher_ast.py` 的解析、遍历和范围注入由 MCP 与旧 Explorer/Raw 共享。
Explorer 的固定查询继续使用 primitive，协议和权限差异见 [API](../api/README.md#cypher)。

# Neo4j 查询规则

适用于 `get_graph_schema.backend=neo4j`。本文件包含该后端的语法和业务配方；先加载语法，再按任务读取配方，无需另读独立配方文件。工具参数、预算和通用错误见 [MCP 参考](mcp-tools.md)。

- [语法](#语法)
- [业务配方](#业务配方)

## 语法

`query_graph.cypher` 直接传 Cypher；业务变量 `$name` 对应 `params.name`，如 `$names` 对应 `{"names":["GMV","订单量"]}`。

CM 为每个 MATCH / OPTIONAL MATCH 的节点和关系注入 workspace/owner 条件，并在全文 YIELD 处过滤；无需手写身份条件。`scoped_ws_id` / `scoped_uid` 由服务端绑定，调用方同名值会被覆盖。自行使用的 `$ws` / `$owner` 仍须绑定，且与服务端条件同时生效；直连数据库没有此注入保证。

### 语法与准入

| 能力 | 当前规则 |
| --- | --- |
| MATCH / OPTIONAL MATCH | 节点须具名；定长关系可匿名，服务端会补内部变量，此时 WITH / RETURN 须显式投影，不能用 `*`。允许无标签、多标签、变量复用及固定多跳 |
| WHERE | 支持比较、AND/OR/NOT、IN 和字符串匹配；身份条件与完整业务条件以 AND 组合，保留 OR 分组 |
| WITH / RETURN | 支持别名、DISTINCT、列表、map、count/collect/min/max/sum/avg；WITH 后只保留投影变量，后续 MATCH 继续注入范围 |
| ORDER BY / SKIP / LIMIT | 使用唯一 key 稳定排序后分页 |
| UNWIND / UNION | 支持列表展开和分支合并；各 MATCH 仍受范围限制，UNION 返回列兼容 |

拒绝匿名节点、变长关系、图子查询、模式推导、表达式内图遍历，以及写入、DDL、管理命令和未准入函数。标签和属性按实际 schema 选择，服务端不做公开标签白名单校验。`$term IN m.synonyms` 匹配列表元素，`CONTAINS` / `STARTS WITH` 匹配文本。

### Neo4j 原生全文与向量

普通召回用搜索工具；需要原生查询时：

- 全文仅允许 `CALL db.index.fulltext.queryNodes($index,$text) YIELD node,score`；索引取自 schema 的 `fulltext_index`，不传第三个 options 参数，不调用其他过程，可在 YIELD 后继续 MATCH。
- 已有与节点 embedding 模型及维度一致的向量时，用范围内 MATCH 加 `vector.similarity.cosine` / `euclidean` 排序；不调用向量索引过程或编造向量。

### Neo4j 诊断

按具体消息修正语法与准入错误。全文索引不可用且文本匹配足够时可用 MATCH 文本条件；`UnsupportedAdministrationCommand` 应核对服务版本和入口，更换检索词无效。

## 业务配方

以下为查询模板，标签、关系和属性以当前 schema 为准，不代表当前实例已经执行验证。Cypher 直接传给 `query_graph.cypher`，业务值通过 `params` 绑定。查询 LIMIT 不提高工具上限，按配方同时设置工具 `limit`。


下方 domain_id 均指 Domain.domain_id。涉及业务节点的其余配方也须按已确认范围补齐对应属性过滤；选择业务 SQL 连接不会自动限定图查询。

来源尚未确定时，先用口径配方发现候选：将 `WHERE c.domain = $domain_id AND c.datasource_id = $datasource_id` 改为 `WHERE c.domain = $domain_id`，从 params 移除 datasource_id，保留返回的 datasource_id 和 dataset。已有数据集名时也可用第 4 节查询来源。唯一候选可采用，多候选需消歧；确定后恢复来源过滤，再核对口径和字段。不要传 null/空串冒充 ID，也不能撤销已确认的域或来源约束。

下方带 offset 的配方取完整集合：首轮 offset=0，设置各节指定的工具 limit，不在 Cypher 内另加 LIMIT。truncated=true 时保留过滤和排序，将 offset 增加本页实际 row_count 后继续，直到 truncated=false；体积截断导致空页时先缩减投影或收窄范围。其他带 LIMIT 的示例仅返回候选页，不能凭 truncated=false 宣称全量。

### 从搜索结果继续查询

已确认 Metric 候选后，批量取得指定域和来源中的口径。工具 `limit=200`；Table / MetricCaliber 的 key 不能代作 Metric 的 key。

`params={"keys":["搜索返回的Metric.key"],"domain_id":"已确认的业务域ID","datasource_id":"已确认的数据源ID","offset":0}`。

```cypher
MATCH (m:Metric)
WHERE m.key IN $keys AND m.domain = $domain_id
OPTIONAL MATCH (m)-[r:HAS]->(c:MetricCaliber)
WHERE c.domain = $domain_id AND c.datasource_id = $datasource_id
RETURN m.key AS metric_key, m.domain AS domain, m.name AS metric,
       m.description AS description, c.key AS caliber_key,
       c.domain AS caliber_domain, c.datasource_id AS datasource_id,
       c.dataset AS dataset, c.expr AS expr, c.time_window AS time_window,
       c.additivity AS additivity, c.is_default AS is_default
ORDER BY m.key, c.key
SKIP $offset
```

OPTIONAL MATCH 保留当前来源没有口径的指标；空口径字段只说明该范围内未匹配，不表示其他来源也没有。查询 Dataset / Table / Dimension 时也可将下方名称条件替换为 `d.key IN $keys` / `t.key IN $keys` / `dim.key IN $keys`，保留业务范围和返回列。

### 1. 业务域列表

`params={}`。

```cypher
MATCH (d:Domain)
RETURN d.domain_id AS domain_id, d.name AS name, d.description AS description
ORDER BY d.domain_id
```

后续 `domain_id` 参数使用返回的 domain_id，name 仅作显示。

### 2. 按名称或同义词找指标，并带出全部口径

标准名称已知时，精确核对域内指标及当前来源的口径，工具 `limit=50`。

`params={"term":"GMV","domain_id":"已确认的业务域ID","datasource_id":"已确认的数据源ID","offset":0}`。

```cypher
MATCH (m:Metric)
WHERE m.domain = $domain_id AND (m.name = $term OR $term IN m.synonyms)
OPTIONAL MATCH (m)-[r:HAS]->(c:MetricCaliber)
WHERE c.domain = $domain_id AND c.datasource_id = $datasource_id
RETURN m.key AS metric_key, m.domain AS domain, m.name AS metric,
       m.synonyms AS synonyms, m.description AS description, m.unit AS unit, m.priority AS priority,
       c.key AS caliber_key, c.domain AS caliber_domain,
       c.datasource_id AS datasource_id, c.dataset AS dataset, c.expr AS expr,
       c.time_window AS time_window, c.additivity AS additivity,
       c.is_default AS is_default
ORDER BY m.key, c.key
SKIP $offset
```

OPTIONAL MATCH 保留没有匹配口径的指标；按上述分页规则取齐后，才是所选范围内的全部匹配口径。`$term IN m.synonyms` 匹配别名元素；批量精确名可将名称/别名条件换成 `m.name IN $names`。确需片段探索时使用全文搜索或 CONTAINS，并保留域约束；精确查询为空不等于业务数据不存在。

### 3. 数据集字段

工具 `limit=500`，`params={"dataset":"数据集名","domain_id":"已确认的业务域ID","datasource_id":"已确认的数据源ID","offset":0}`。

```cypher
MATCH (d:Dataset)-[r:HAS]->(f:DatasetField)
WHERE d.name = $dataset AND d.domain = $domain_id
  AND d.datasource_id = $datasource_id
  AND f.domain = $domain_id AND f.datasource_id = $datasource_id
RETURN d.name AS dataset, d.domain AS domain, d.datasource_id AS datasource_id,
       d.source AS physical_source, f.name AS field, f.datasource_id AS field_datasource_id,
       f.data_type AS data_type, f.description AS description, f.is_primary AS is_primary
ORDER BY d.key, f.key
SKIP $offset
```

### 4. 数据集所在数据源（取得 `datasource_id`）

`params={"dataset":"数据集名"}`。

```cypher
MATCH (d:Dataset)-[cm_edge_1:AVAILABLE_IN]->(s:DataSource)
WHERE d.name = $dataset
RETURN d.name AS dataset, s.datasource_id AS datasource_id,
       s.datasource_name AS datasource_name, s.datasource_type AS datasource_type
ORDER BY s.datasource_name
```

### 5. 表与列（物理区）

查表及其数据源，`params={"table":"表名片段"}`。

```cypher
MATCH (s:DataSource)-[cm_edge_1:HAS]->(t:Table)
WHERE toLower(t.name) CONTAINS toLower($table)
RETURN s.datasource_id AS datasource_id, s.datasource_type AS datasource_type,
       t.name AS table_name, t.comment AS comment, t.partition_keys AS partition_keys
ORDER BY t.name
LIMIT 50
```

查指定数据源中的表列，`params={"table":"完整表名","datasource_id":"上一步返回的数据源ID"}`。

```cypher
MATCH (t:Table)-[cm_edge_1:HAS]->(c:Column)
WHERE t.name = $table AND t.datasource_id = $datasource_id
RETURN t.datasource_id AS datasource_id, t.name AS table_name, c.name AS column_name, c.data_type AS dtype,
       c.comment AS description
ORDER BY c.name
LIMIT 500
```

查表关联键，`params={"table":"完整表名"}`。

```cypher
MATCH (a:Table)-[j:JOINS]->(b:Table)
WHERE a.name = $table
RETURN a.name AS left_table, b.name AS right_table,
       j.left_columns AS left_columns, j.right_columns AS right_columns,
       j.join_type AS join_type
ORDER BY b.name
```

### 6. 维度及其绑定字段与取值示例

在已确认域内核对维度，按来源查看绑定；不直接用 datasource_id 过滤 Dimension。工具 `limit=100`。

`params={"term":"维度名","domain_id":"已确认的业务域ID","datasource_id":"已确认的数据源ID","offset":0}`。

```cypher
MATCH (dim:Dimension)
WHERE dim.domain = $domain_id AND (dim.name = $term OR $term IN dim.synonyms)
OPTIONAL MATCH (dim)-[r:MAPS_TO_FIELD]->(f:DatasetField)
WHERE f.domain = $domain_id AND f.datasource_id = $datasource_id
RETURN dim.key AS dimension_key, dim.domain AS domain, dim.name AS dimension,
       dim.synonyms AS synonyms, dim.description AS description,
       f.domain AS field_domain, f.datasource_id AS datasource_id,
       f.dataset AS dataset, f.name AS field, f.description AS field_description,
       r.expr AS expr, r.value_example AS value_example,
       r.total_value_marker AS total_value_marker
ORDER BY dim.key, f.key
SKIP $offset
```

比较近义维度时可将名称/别名条件换成 `dim.name IN $names`，保留域和绑定来源约束。核对描述、字段表达式和所属数据集；合法绑定不证明两个维度的统计总体等价。`value_example` 是示例，不是完整枚举；total_value_marker 提供已声明的汇总值，缺失时不猜测。需要确认真实取值时使用同一 datasource_id 的只读 SQL 探针。

### 7. 某指标可用的维度（经共同数据集）

`params={"metric":"指标名"}`。

```cypher
MATCH (m:Metric)-[cm_edge_1:HAS]->(c:MetricCaliber)-[cm_edge_2:CALIBER_ON]->(d:Dataset)
WHERE m.name = $metric
MATCH (dim:Dimension)-[cm_edge_3:DIMENSION_BINDING]->(d:Dataset)
RETURN DISTINCT m.name AS metric, dim.name AS dimension, d.name AS dataset
ORDER BY dim.name
LIMIT 200
```

### 8. 已验证查询（金标 SQL，仅作参考，不能原样套用）

`params={"term":"问题关键词"}`。

```cypher
MATCH (v:VerifiedQuery)
WHERE toLower(v.question) CONTAINS toLower($term)
RETURN v.domain AS domain, v.question AS question, v.verified_sql AS verified_sql
ORDER BY v.question
LIMIT 10
```

### 9. 知识区事件（节假日、发布、促销等）

`params={"term":"事件关键词"}`。

```cypher
MATCH (e:Event)
WHERE toLower(e.name) CONTAINS toLower($term)
RETURN e.name AS name, e.type AS type, e.description AS description,
       e.date_from AS date_from, e.date_to AS date_to
ORDER BY e.date_from
LIMIT 50
```

### 10. 全文检索

以下为需要原生全文查询时的配方，`params.index` 取自 schema 中对应节点的 `fulltext_index`；`params.text` 为全文检索词。普通搜索参数见 [MCP 参考](mcp-tools.md#搜索节点)。

```cypher
CALL db.index.fulltext.queryNodes($index,$text) YIELD node,score
RETURN node.key AS key, node.name AS name, score AS score
ORDER BY score DESC
LIMIT 20
```

### 11. 向量排序

`params.vector` 放与目标 embedding 模型及维度一致的已有向量，`params.k` 为候选数；同步设置工具 limit。

```cypher
MATCH (m:Metric)
WHERE m.embedding IS NOT NULL
RETURN m.key AS key, m.name AS name,
       vector.similarity.cosine(m.embedding,$vector) AS score
ORDER BY score DESC
LIMIT $k
```

### 12. WITH 汇总数据集的维度

`params={"domain":"业务域名"}`。先限定业务域，再汇总每个数据集的维度名称；没有关联维度的数据集仍保留。

```cypher
MATCH (d:Dataset)
WHERE d.domain = $domain
OPTIONAL MATCH (dim:Dimension)-[r:DIMENSION_BINDING]->(d)
WITH d, collect(DISTINCT dim.name) AS dimensions
RETURN d.name AS dataset, d.description AS description, dimensions AS dimensions
ORDER BY d.name
LIMIT 100
```

### 13. UNWIND 批量查询并保留未命中输入

`params={"names":["GMV","订单量"]}`。仅需要命中记录时可直接用 `m.name IN $names`；需要逐个保留输入时使用：

```cypher
UNWIND $names AS requested
OPTIONAL MATCH (m:Metric {name: requested})
RETURN requested AS requested, m.key AS key, m.name AS name
ORDER BY requested, m.key
LIMIT 100
```

未命中的输入返回空节点字段。

### 14. UNION 合并指标和维度候选

`params={"term":"销售"}`。各分支分别匹配并限定范围，返回列名与顺序保持一致。

```cypher
MATCH (m:Metric)
WHERE toLower(m.name) CONTAINS toLower($term)
RETURN 'metric' AS kind, m.key AS key, m.name AS name
UNION
MATCH (d:Dimension)
WHERE toLower(d.name) CONTAINS toLower($term)
RETURN 'dimension' AS kind, d.key AS key, d.name AS name
```

UNION 去除重复行，UNION ALL 保留重复行；不保证合并后的顺序。工具 limit 控制返回行数，仍需检查 truncated。

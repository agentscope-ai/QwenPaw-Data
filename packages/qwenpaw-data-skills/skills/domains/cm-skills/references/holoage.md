# Holo AGE 查询规则

适用于 `get_graph_schema.backend=holoage`。本文件包含该后端的语法和业务配方；先加载语法，再按任务读取配方，无需另读独立配方文件。工具参数、预算和通用错误见 [MCP 参考](mcp-tools.md)。

- [语法](#语法)
- [业务配方](#业务配方)

## 语法

`query_graph.cypher` 传完整只读 SQL；服务端不补 SQL 包装、图名、身份 WHERE 或返回列。以下限制针对 Holo，不代表所有 Apache AGE 版本。

### 调用与参数

图名取自 `get_graph_schema.graphs`，替换模板中的 `authorized_graph`。个人图按 schema 隔离，无需重复 owner/workspace 条件。函数必须写 `ag_catalog.cypher`，无需 SET search_path。

有 Cypher 参数时，第三参绑定整个 JSON 字符串；下例工具参数为 `params={"values":"{\"term\":\"GMV\",\"domain_id\":\"已确认的业务域ID\"}"}`：

```sql
SELECT result.name
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (m:Metric)
  WHERE m.name = $term AND m.domain = $domain_id
  RETURN m.name
  ORDER BY m.name, m.key
  LIMIT 10
$$, %(values)s::ag_catalog.agtype) AS result(name ag_catalog.agtype)
```

无 Cypher 参数时省略第三参，即 `ag_catalog.cypher('authorized_graph', $$...$$)`，工具 `params` 省略或为 `{}`。第三参不能直接写 `'{}'` 或 `'{}'::ag_catalog.agtype` 常量；类型转换不能代替绑定。SQL 占位符放在 `$$...$$` 外，内部 `$term` 从第三参 JSON 取值，不能仅传 `params.term`。

### 返回列与准入

- Cypher 变量只在 `$$...$$` 内有效。RETURN 与 `AS result(...)` 的列数、顺序须一致；AS 列名唯一，每列为 `ag_catalog.agtype`。外层 SQL 引用 AS 列名，两层别名建议统一。
- 外层直接选择 agtype 列，不额外转换输出类型；其标量、列表和 map 保留数据库原生文本，结合 `property_types` 理解。搜索返回的 node 才是 JSON 属性对象；外层 SQL 计算的表达式按其 SQL 类型返回。
- 只允许授权图的只读结构及准入函数；拒绝写入、DDL、CALL、事务控制、EXPLAIN、系统目录和其他 schema。服务端不校验标签或表名是否属于公开 schema 清单。

### 进阶语法与 Holo 限制

| 用法 | Holo 中的写法 |
| --- | --- |
| MATCH 与关系 | 支持无标签或单标签、内联属性、WHERE、匿名节点/关系、有向/无向及固定多跳 |
| OPTIONAL MATCH / WITH / 聚合 | OPTIONAL 未命中为 NULL；WITH 只传递投影变量。支持 count/collect/min/max/sum/avg，非聚合项构成分组键，返回项仍声明为 agtype |
| 排序与分页 | 用源表达式 `ORDER BY m.name, m.key`，不能依赖 RETURN 别名；稳定排序后改变 SKIP |
| UNWIND | 可展开列表后 MATCH / OPTIONAL MATCH；大数据量下性能受限，只需命中记录时优先 `WHERE ... IN` |
| 路径 | 给 VLE 显式上界，如 `*1..2`；`*1..` 隐含上界为 10，不支持无界 `*`。下界 0 不能与其他边组合，改为 1 或拆 MATCH；可用 nodes(p)、relationships(p)、length(p) |
| 标签与日期 | 单顶点单标签；顶点用 label(n)，边用 type(r)，不用 labels(n)。不支持 datetime()；timestamp() 是当前 epoch 毫秒，不能解析日期字符串 |
| SQL 混合读取 | 支持 GROUP BY、JOIN、UNION、CTE、子查询；每个 cypher 独立声明返回列，内部变量不共享。JOIN 使用 SQL 列，UNION 列数和类型一致 |
| 原生标签表 | 表名限定为授权 schema，如 `"authorized_graph"."Metric"`，CTE 同样受范围检查。SQL 参数用 `%(name)s`，无需 JSON 第三参；`ag_catalog.agtype_object_field_text(n.properties, 'name')` 返回 SQL 文本 |

### Holo 原生全文与向量

普通召回用搜索工具。定制查询可用已准入的 `text_search` 和标签表 `content` 投影；`CONTAINS` / `IN` 不是分词全文检索。

已有与目标 embedding 模型及维度一致的向量时，可用 `approx_cosine_distance` 和 `plot_embedding` 投影；函数、物理列和索引以实例为准，ANN 召回由数据库决定。不套用 Neo4j 的全文 CALL / `vector.similarity`，不编造向量。

### Holo 诊断

| 报错 | 修正位置 |
| --- | --- |
| `Only ag_catalog.cypher is allowed` | 使用完整函数名，见 [调用与参数](#调用与参数) |
| `22023: third argument of cypher function must be a parameter` | 第三参传了常量；按 [调用与参数](#调用与参数) 绑定或省略 |
| `42P01` / `42703` / `42701`、返回结构不匹配 | 按 Message/Position 区分内外层；核对作用域、RETURN/AS 列数与顺序、AS 列名及重复列名；ORDER BY 用源表达式 |
| `42804` / `42846` / 其他 `22…` | 核对 schema 类型、绑定值、表达式和 AS 的 agtype 声明 |

## 业务配方

以下为查询模板，标签、关系和属性以当前 schema 为准，不代表当前实例已经执行验证。每个 SQL 代码块可直接作为 `query_graph.cypher`，先替换授权图名并绑定参数。查询 LIMIT 不提高工具上限，按配方同时设置工具 `limit`。


下方 domain_id 均指 Domain.domain_id。涉及业务节点的其余配方也须按已确认范围补齐对应属性过滤；选择业务 SQL 连接不会自动限定图查询。

来源尚未确定时，先用口径配方发现候选：将 `WHERE c.domain = $domain_id AND c.datasource_id = $datasource_id` 改为 `WHERE c.domain = $domain_id`，从 params.values 的 JSON 对象中移除 datasource_id，保留返回的 datasource_id 和 dataset。已有数据集名时也可用第 4 节查询来源。唯一候选可采用，多候选需消歧；确定后恢复来源过滤，再核对口径和字段。不要传 null/空串冒充 ID，也不能撤销已确认的域或来源约束。

下方带 offset 的配方取完整集合：首轮 offset=0，设置各节指定的工具 limit，不在 Cypher 内另加 LIMIT。truncated=true 时保留过滤和排序，将 offset 增加本页实际 row_count 后继续，直到 truncated=false；体积截断导致空页时先缩减投影或收窄范围。其他带 LIMIT 的示例仅返回候选页，不能凭 truncated=false 宣称全量。

### 从搜索结果继续查询

已确认 Metric 候选后，批量取得指定域和来源中的口径。工具 `limit=200`；Table / MetricCaliber 的 key 不能代作 Metric 的 key。

`params={"values":"{\"keys\":[\"搜索返回的Metric.key\"],\"domain_id\":\"已确认的业务域ID\",\"datasource_id\":\"已确认的数据源ID\",\"offset\":0}"}`。

```sql
SELECT result.metric_key AS metric_key, result.domain AS domain, result.metric AS metric,
       result.description AS description, result.caliber_key AS caliber_key, result.caliber_domain AS caliber_domain,
       result.datasource_id AS datasource_id, result.dataset AS dataset, result.expr AS expr,
       result.time_window AS time_window, result.additivity AS additivity, result.is_default AS is_default
FROM ag_catalog.cypher('authorized_graph', $$
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
$$, %(values)s::ag_catalog.agtype)
AS result(metric_key ag_catalog.agtype, domain ag_catalog.agtype, metric ag_catalog.agtype,
          description ag_catalog.agtype, caliber_key ag_catalog.agtype, caliber_domain ag_catalog.agtype,
          datasource_id ag_catalog.agtype, dataset ag_catalog.agtype, expr ag_catalog.agtype,
          time_window ag_catalog.agtype, additivity ag_catalog.agtype, is_default ag_catalog.agtype)
```

OPTIONAL MATCH 保留当前来源没有口径的指标；空口径字段只说明该范围内未匹配，不表示其他来源也没有。查询 Dataset / Table / Dimension 时也可将下方名称条件替换为 `d.key IN $keys` / `t.key IN $keys` / `dim.key IN $keys`，保留业务范围和返回列。

### 1. 业务域列表

`params={}`。

```sql
SELECT result.domain_id AS domain_id, result.name AS name, result.description AS description
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (d:Domain)
  RETURN d.domain_id AS domain_id, d.name AS name, d.description AS description
  ORDER BY d.domain_id
$$)
AS result(domain_id ag_catalog.agtype, name ag_catalog.agtype, description ag_catalog.agtype)
```

后续 `domain_id` 参数使用返回的 domain_id，name 仅作显示。

### 2. 按名称或同义词找指标，并带出全部口径

标准名称已知时，精确核对域内指标及当前来源的口径，工具 `limit=50`。

`params={"values":"{\"term\":\"GMV\",\"domain_id\":\"已确认的业务域ID\",\"datasource_id\":\"已确认的数据源ID\",\"offset\":0}"}`。

```sql
SELECT result.metric_key AS metric_key, result.domain AS domain, result.metric AS metric,
       result.synonyms AS synonyms, result.description AS description, result.unit AS unit, result.priority AS priority,
       result.caliber_key AS caliber_key, result.caliber_domain AS caliber_domain, result.datasource_id AS datasource_id,
       result.dataset AS dataset, result.expr AS expr, result.time_window AS time_window,
       result.additivity AS additivity, result.is_default AS is_default
FROM ag_catalog.cypher('authorized_graph', $$
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
$$, %(values)s::ag_catalog.agtype)
AS result(metric_key ag_catalog.agtype, domain ag_catalog.agtype, metric ag_catalog.agtype,
          synonyms ag_catalog.agtype, description ag_catalog.agtype, unit ag_catalog.agtype, priority ag_catalog.agtype,
          caliber_key ag_catalog.agtype, caliber_domain ag_catalog.agtype, datasource_id ag_catalog.agtype,
          dataset ag_catalog.agtype, expr ag_catalog.agtype, time_window ag_catalog.agtype,
          additivity ag_catalog.agtype, is_default ag_catalog.agtype)
```

OPTIONAL MATCH 保留没有匹配口径的指标；按上述分页规则取齐后，才是所选范围内的全部匹配口径。`$term IN m.synonyms` 匹配别名元素；批量精确名可将名称/别名条件换成 `m.name IN $names`。确需片段探索时使用全文搜索或 CONTAINS，并保留域约束；精确查询为空不等于业务数据不存在。

### 3. 数据集字段

工具 `limit=500`，`params={"values":"{\"dataset\":\"数据集名\",\"domain_id\":\"已确认的业务域ID\",\"datasource_id\":\"已确认的数据源ID\",\"offset\":0}"}`。

```sql
SELECT result.dataset AS dataset, result.domain AS domain, result.datasource_id AS datasource_id,
       result.physical_source AS physical_source, result.field AS field, result.field_datasource_id AS field_datasource_id,
       result.data_type AS data_type, result.description AS description, result.is_primary AS is_primary
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (d:Dataset)-[r:HAS]->(f:DatasetField)
  WHERE d.name = $dataset AND d.domain = $domain_id
    AND d.datasource_id = $datasource_id
    AND f.domain = $domain_id AND f.datasource_id = $datasource_id
  RETURN d.name AS dataset, d.domain AS domain, d.datasource_id AS datasource_id,
         d.source AS physical_source, f.name AS field, f.datasource_id AS field_datasource_id,
         f.data_type AS data_type, f.description AS description, f.is_primary AS is_primary
  ORDER BY d.key, f.key
  SKIP $offset
$$, %(values)s::ag_catalog.agtype)
AS result(dataset ag_catalog.agtype, domain ag_catalog.agtype, datasource_id ag_catalog.agtype,
          physical_source ag_catalog.agtype, field ag_catalog.agtype, field_datasource_id ag_catalog.agtype,
          data_type ag_catalog.agtype, description ag_catalog.agtype, is_primary ag_catalog.agtype)
```

### 4. 数据集所在数据源（取得 `datasource_id`）

`params={"values":"{\"dataset\":\"数据集名\"}"}`。

```sql
SELECT result.dataset AS dataset,
       result.datasource_id AS datasource_id,
       result.datasource_name AS datasource_name,
       result.datasource_type AS datasource_type
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (d:Dataset)-[cm_edge_1:AVAILABLE_IN]->(s:DataSource)
  WHERE d.name = $dataset
  RETURN d.name AS dataset, s.datasource_id AS datasource_id,
         s.datasource_name AS datasource_name, s.datasource_type AS datasource_type
  ORDER BY s.datasource_name
$$, %(values)s::ag_catalog.agtype)
AS result(dataset ag_catalog.agtype,
          datasource_id ag_catalog.agtype,
          datasource_name ag_catalog.agtype,
          datasource_type ag_catalog.agtype)
```

### 5. 表与列（物理区）

查表及其数据源，`params={"values":"{\"table\":\"表名片段\"}"}`。

```sql
SELECT datasource_id AS datasource_id, datasource_type AS datasource_type,
       table_name AS table_name, comment AS comment,
       partition_keys AS partition_keys
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (s:DataSource)-[:HAS]->(t:Table)
  WHERE toLower(t.name) CONTAINS toLower($table)
  RETURN s.datasource_id AS datasource_id, s.datasource_type AS datasource_type,
         t.name AS table_name, t.comment AS comment, t.partition_keys AS partition_keys
  ORDER BY t.name
  LIMIT 50
$$, %(values)s::ag_catalog.agtype)
AS result(datasource_id ag_catalog.agtype, datasource_type ag_catalog.agtype,
          table_name ag_catalog.agtype, comment ag_catalog.agtype, partition_keys ag_catalog.agtype)
```

查指定数据源中的表列，`params={"values":"{\"table\":\"完整表名\",\"datasource_id\":\"上一步返回的数据源ID\"}"}`。

```sql
SELECT datasource_id AS datasource_id, table_name AS table_name,
       column_name AS column_name, dtype AS dtype, description AS description
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (t:Table)-[:HAS]->(c:Column)
  WHERE t.name = $table AND t.datasource_id = $datasource_id
  RETURN t.datasource_id AS datasource_id, t.name AS table_name,
         c.name AS column_name, c.data_type AS dtype, c.comment AS description
  ORDER BY c.name
  LIMIT 500
$$, %(values)s::ag_catalog.agtype)
AS result(datasource_id ag_catalog.agtype, table_name ag_catalog.agtype,
          column_name ag_catalog.agtype, dtype ag_catalog.agtype, description ag_catalog.agtype)
```

查表关联键，`params={"values":"{\"table\":\"完整表名\"}"}`。

```sql
SELECT result.left_table AS left_table,
       result.right_table AS right_table,
       result.left_columns AS left_columns,
       result.right_columns AS right_columns,
       result.join_type AS join_type
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (a:Table)-[j:JOINS]->(b:Table)
  WHERE a.name = $table
  RETURN a.name AS left_table, b.name AS right_table,
         j.left_columns AS left_columns, j.right_columns AS right_columns,
         j.join_type AS join_type
  ORDER BY b.name
$$, %(values)s::ag_catalog.agtype)
AS result(left_table ag_catalog.agtype,
          right_table ag_catalog.agtype,
          left_columns ag_catalog.agtype,
          right_columns ag_catalog.agtype,
          join_type ag_catalog.agtype)
```

### 6. 维度及其绑定字段与取值示例

在已确认域内核对维度，按来源查看绑定；不直接用 datasource_id 过滤 Dimension。工具 `limit=100`。

`params={"values":"{\"term\":\"维度名\",\"domain_id\":\"已确认的业务域ID\",\"datasource_id\":\"已确认的数据源ID\",\"offset\":0}"}`。

```sql
SELECT result.dimension_key AS dimension_key, result.domain AS domain, result.dimension AS dimension,
       result.synonyms AS synonyms, result.description AS description, result.field_domain AS field_domain,
       result.datasource_id AS datasource_id, result.dataset AS dataset, result.field AS field,
       result.field_description AS field_description, result.expr AS expr, result.value_example AS value_example,
       result.total_value_marker AS total_value_marker
FROM ag_catalog.cypher('authorized_graph', $$
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
$$, %(values)s::ag_catalog.agtype)
AS result(dimension_key ag_catalog.agtype, domain ag_catalog.agtype, dimension ag_catalog.agtype,
          synonyms ag_catalog.agtype, description ag_catalog.agtype, field_domain ag_catalog.agtype,
          datasource_id ag_catalog.agtype, dataset ag_catalog.agtype, field ag_catalog.agtype,
          field_description ag_catalog.agtype, expr ag_catalog.agtype, value_example ag_catalog.agtype,
          total_value_marker ag_catalog.agtype)
```

比较近义维度时可将名称/别名条件换成 `dim.name IN $names`，保留域和绑定来源约束。核对描述、字段表达式和所属数据集；合法绑定不证明两个维度的统计总体等价。`value_example` 是示例，不是完整枚举；total_value_marker 提供已声明的汇总值，缺失时不猜测。需要确认真实取值时使用同一 datasource_id 的只读 SQL 探针。

### 7. 某指标可用的维度（经共同数据集）

`params={"values":"{\"metric\":\"指标名\"}"}`。

```sql
SELECT result.metric AS metric,
       result.dimension AS dimension,
       result.dataset AS dataset
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (m:Metric)-[cm_edge_1:HAS]->(c:MetricCaliber)-[cm_edge_2:CALIBER_ON]->(d:Dataset)
  WHERE m.name = $metric
  MATCH (dim:Dimension)-[cm_edge_3:DIMENSION_BINDING]->(d:Dataset)
  RETURN DISTINCT m.name AS metric, dim.name AS dimension, d.name AS dataset
  ORDER BY dim.name
  LIMIT 200
$$, %(values)s::ag_catalog.agtype)
AS result(metric ag_catalog.agtype,
          dimension ag_catalog.agtype,
          dataset ag_catalog.agtype)
```

### 8. 已验证查询（金标 SQL，仅作参考，不能原样套用）

`params={"values":"{\"term\":\"问题关键词\"}"}`。

```sql
SELECT result.domain AS domain,
       result.question AS question,
       result.verified_sql AS verified_sql
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (v:VerifiedQuery)
  WHERE toLower(v.question) CONTAINS toLower($term)
  RETURN v.domain AS domain, v.question AS question, v.verified_sql AS verified_sql
  ORDER BY v.question
  LIMIT 10
$$, %(values)s::ag_catalog.agtype)
AS result(domain ag_catalog.agtype,
          question ag_catalog.agtype,
          verified_sql ag_catalog.agtype)
```

### 9. 知识区事件（节假日、发布、促销等）

`params={"values":"{\"term\":\"事件关键词\"}"}`。

```sql
SELECT result.name AS name,
       result.type AS type,
       result.description AS description,
       result.date_from AS date_from,
       result.date_to AS date_to
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (e:Event)
  WHERE toLower(e.name) CONTAINS toLower($term)
  RETURN e.name AS name, e.type AS type, e.description AS description,
         e.date_from AS date_from, e.date_to AS date_to
  ORDER BY e.date_from
  LIMIT 50
$$, %(values)s::ag_catalog.agtype)
AS result(name ag_catalog.agtype,
          type ag_catalog.agtype,
          description ag_catalog.agtype,
          date_from ag_catalog.agtype,
          date_to ag_catalog.agtype)
```

### 10-11. 全文与向量检索

普通搜索见 [MCP 参考](mcp-tools.md#搜索节点)；定制原生查询的能力与限制见 [Holo 规则](#holo-原生全文与向量)。

### 12. WITH 汇总数据集的维度

`params={"values":"{\"domain\":\"业务域名\"}"}`。先限定业务域，再汇总每个数据集的维度名称；没有关联维度的数据集仍保留。

```sql
SELECT result.dataset AS dataset, result.description AS description,
       result.dimensions AS dimensions
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (d:Dataset)
  WHERE d.domain = $domain
  OPTIONAL MATCH (dim:Dimension)-[:DIMENSION_BINDING]->(d)
  WITH d, collect(DISTINCT dim.name) AS dimensions
  RETURN d.name AS dataset, d.description AS description, dimensions AS dimensions
  ORDER BY d.name
  LIMIT 100
$$, %(values)s::ag_catalog.agtype)
AS result(dataset ag_catalog.agtype, description ag_catalog.agtype,
          dimensions ag_catalog.agtype)
```

WITH 只向后传递列出的变量；聚合列表仍以 agtype 原生文本返回。

### 13. UNWIND 批量查询并保留未命中输入

`params={"values":"{\"names\":[\"GMV\",\"订单量\"]}"}`；未命中时 key 为 null。

```sql
SELECT result.requested AS requested, result.key AS key
FROM ag_catalog.cypher('authorized_graph', $$
  UNWIND $names AS requested
  OPTIONAL MATCH (m:Metric {name: requested})
  RETURN requested AS requested, m.key AS key
  ORDER BY requested
  LIMIT 50
$$, %(values)s::ag_catalog.agtype)
AS result(requested ag_catalog.agtype, key ag_catalog.agtype)
```

### 14. UNION 合并指标和维度候选

`params={"values":"{\"term\":\"销售\"}"}`。使用外层 SQL UNION 合并两段 Cypher，各段独立声明返回列，保持列数、顺序和类型一致。

```sql
SELECT metrics.kind AS kind, metrics.key AS key, metrics.name AS name
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (m:Metric)
  WHERE toLower(m.name) CONTAINS toLower($term)
  RETURN 'metric' AS kind, m.key AS key, m.name AS name
$$, %(values)s::ag_catalog.agtype)
AS metrics(kind ag_catalog.agtype, key ag_catalog.agtype, name ag_catalog.agtype)
UNION
SELECT dimensions.kind AS kind, dimensions.key AS key, dimensions.name AS name
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH (d:Dimension)
  WHERE toLower(d.name) CONTAINS toLower($term)
  RETURN 'dimension' AS kind, d.key AS key, d.name AS name
$$, %(values)s::ag_catalog.agtype)
AS dimensions(kind ag_catalog.agtype, key ag_catalog.agtype, name ag_catalog.agtype)
```

外层 metrics / dimensions 是 SQL 表别名，与各段内部节点变量独立。UNION 去重，UNION ALL 保留重复行；合并结果不保证顺序，仍需检查工具返回的 `truncated`。

### 15. 有界路径（Holo）

`params.values` 中绑定已查到的 key；按业务关系将 `[*1..2]` 收窄为指定关系类型。

```sql
SELECT result.key AS key, result.label AS label,
       result.hops AS hops
FROM ag_catalog.cypher('authorized_graph', $$
  MATCH p = (m:Metric)-[*1..2]->(n)
  WHERE m.key = $key
  RETURN n.key AS key, label(n) AS label, length(p) AS hops
  ORDER BY n.key
  LIMIT 50
$$, %(values)s::ag_catalog.agtype)
AS result(key ag_catalog.agtype, label ag_catalog.agtype, hops ag_catalog.agtype)
```

# Schema

Schema 统一声明逻辑节点、边、key、属性、检索字段和 CRUD 能力。`Zone` 定义 metadata、knowledge、user 和内部区域 `_internal`；节点归属与持久化 zone 使用同一枚举。

## 文件与关键类型

| 文件 | 类型或对象 | 职责 |
| --- | --- | --- |
| `model.py` | `PropertySpec` | 声明属性名称、`ValueKind`、`editable`、`DisplayPolicy`、可空性和所有者。 |
| `model.py` | `KeySpec` | 声明节点不可变的 `key` 属性、生成策略、前缀和 deterministic parts。 |
| `model.py` | `Zone` | 定义 metadata、knowledge、user 三个业务区域及内部区域 `_internal`。 |
| `model.py` | `NodeSpec` | 声明一个 canonical 节点类型、properties、全文检索字段、embedding 输入字段和操作。 |
| `model.py` | `EdgeSpec` | 直接引用 canonical source/target，声明 endpoint-closed 边类型。 |
| `model.py` | `GraphSchema` | 注册全部声明、建立索引、执行逻辑校验，并提供完整导出与拓扑概览。 |
| `graph.py` | `METRIC`、`HAS_CALIBER` 等 | 逻辑图事实。文件中没有构建函数或运行时类型转换。 |
| `graph.py` | `GRAPH_SCHEMA` | Store 和下游业务共同使用的 canonical Schema。 |
| `term.py` | `Term`、`TermError`、`UnknownTermError` | 定义节点属性的可选标签、错误类型和 graph label 校验。 |
| `glossary.py` | `Glossary`、`GlossaryRepository`、`DEFAULT_GLOSSARY_FILE` | Glossary 负责词条校验及标签规则，Repository 负责 Workspace 词典的 SQL 读写与事务。 |
| `relational.py` | `metadata`、`external_tables` | 声明 CM 的模型配置、词表、文档、checkpoint 和编织日志表，以及外部表的读取字段；由根目录 `CMDatabase` 统一初始化自有表、校验依赖。 |
| `default_glossary.json` | 默认标签数据 | 声明部分节点属性的可选标签、描述和 graph label 映射。 |

`GraphSchema` 从同一组 canonical 声明提供两种只读投影：`to_document()` 导出部署和 HTTP Schema 接口使用的完整结构，`get_overview()` 提供 Agent 读取图之前使用的精简拓扑概览。两种投影共同复用 Python 声明作为事实源。

关系表的 `metadata` 归 CM 所有，用于启动建表。`external_tables` 声明 Provider、Host history 和数据源表的必需列，保持独立于 DDL metadata；这些表由 Host、semantic_config 等所属模块维护，CM 在启动时检查其可用性。

## 声明方式

```python
from context_manager.schema import GRAPH_SCHEMA, METRIC, HAS_CALIBER

assert GRAPH_SCHEMA.node("Metric") is METRIC
assert GRAPH_SCHEMA.edge(("Metric", "HAS", "MetricCaliber")) is HAS_CALIBER
```

声明位于 `graph.py`。消费者使用注册对象，按值复制的同名 Spec 会被拒绝。

`NodeSpec.zone` 声明类型归属，`ZONE_PROPERTY` 声明对应持久化字段。Store 为节点注入 `spec.zone.value`，为关系注入起点 Spec 的 zone；`GraphSchema.zones` 由同一枚举导出。

`EdgeSpec.source` 和 `target` 直接引用 canonical `NodeSpec`。

Schema 19 将包含关系统一为 `HAS`：Domain → Dataset/Dimension/Metric、DataSource → Table、Table → Column、Dataset → DatasetField、Metric → MetricCaliber、User → UserMemory。八个 endpoint-qualified `EdgeSpec` 保留各自的方向、属性和操作，Python 常量名保持不变。查询具体包含关系时使用端点类型；仅筛选 `HAS` 会匹配全部包含关系。

从 Schema 18 升级时，须显式迁移存量 `HAS_DATASET`、`HAS_DIMENSION`、`HAS_METRIC`、`HAS_TABLE`、`HAS_COLUMN`、`HAS_CALIBER`、`HAS_MEMORY` 关系，保留端点和全部属性；启动时的 ensure 不会重命名旧边。Holo 需在各目标图中准备 `HAS` 关系标签。升级前用旧版本完成恢复栈的恢复或清理，因为 Undo 保存了旧关系三元组。

每个 `NodeSpec` 必须声明且只声明一个 `KeySpec`。`KeySpec` 固定为不可编辑、非空、始终展示的 TEXT 属性；deterministic parts 必须引用同一节点中不可空且不可编辑的属性。MG 节点的 deterministic parts 一律由 `owner_id` 段加该节点的自然标识列组成：`owner_id` 由写入策略决定（全局轨为 `__global__`，个人轨为用户 ID），因此全局与个人的同名对象 key 天然分离。`DataSource` 用 deterministic `("owner_id", "datasource_id")` key；`VerifiedQuery` 用 deterministic `("owner_id", "domain", "question_hash")` key，`question_hash` 是 question 的 sha256 短哈希，同一问题改了 SQL 会原地更新而不是新建节点。KG 的 `Entity`/`Event` 用 deterministic `("owner_id", "identity_hash")` key：`identity_hash` 由 Knowledge 按规范化身份事实（Entity 为 type+canonical_name，Event 为 type+name+date_from+scope）做 sha256 短哈希写入，与批内 exact_key 折叠同源，跨导入的精确重复直接命中同一节点。

Schema 定义 `ENTITY.type`、`EVENT.type` 等普通必填 TEXT property；`Glossary` 为选定 property 声明可选词条，并维护词条到 `Core` 等物理 graph label 的映射。统一通过 `context_manager.schema` 导入 `Term`、`Glossary` 及相关错误类型。

`Entity` 的业务字段为 `key`、`name`、`canonical_name`、`type`、`identity_hash`、`aliases`、`description`、`lifecycle_state`；`Event` 的业务字段为 `key`、`name`、`type`、`identity_hash`、`description`、`scope`、`date_from`、`date_to`。二者共同保存 `workspace_id`、`owner_id`、`zone`、`source_ids`、`embedding`、`embedding_hash`。KG 节点不保存 `datasource_id` 或 `valid_to`。

## 词汇表与词条

`Term` 表示指定节点属性的一个词条，以 `(node, property, value)` 定位，并保存面向抽取和展示的描述。例如 `Entity.type=product` 是一个词条，而 `graph_labels` 中的 `Core` 表示该属性值触发的 Neo4j 节点标签 `(:Entity:Core)`。

`Glossary` 在数据库中维护 Workspace 的词汇表及 graph label 映射。初始化 JSON 可以声明多个节点属性的词条集合：

```json
{
  "version": 1,
  "term_groups": [
    {
      "node": "Entity",
      "property": "type",
      "terms": [
        {"value": "product", "description": "可交付的软件产品或工具"}
      ],
      "graph_labels": {"Core": ["product"]}
    },
    {
      "node": "Event",
      "property": "type",
      "terms": [
        {"value": "launch", "description": "产品发布事件"}
      ],
      "graph_labels": {}
    }
  ]
}
```

`(node, property)` 必须对应 canonical `NodeSpec` 及其 TEXT property。JSON 中声明的属性进入词条管理范围，其余属性继续使用 Schema 的普通字段校验。当前 `Core` 映射属于 `Entity.type`，成员关系由 `(node, property, value, graph_label)` 确定。

默认文件 `schema/default_glossary.json` 维护 12 个 `Entity.type` 词条：`product`、`model_family`、`model_version`、`feature`、`platform`、`subscription_plan`、`company`、`team`、`customer`、`scenario`、`region`、`technology`。其中 `product`、`platform`、`model_family`、`model_version`、`subscription_plan` 会物化 `Core` graph label。

`Glossary` 绑定 canonical `GraphSchema`、SQL Engine 和 `(tenant_id, workspace_id)`。`WorkspaceRuntime` 持有该实例，并注入 Knowledge 与 Store。`QWENPAW_DATA_DB_URL` 中的 `cm_kg_glossary` 表每个 Workspace 保存一份词典文档，Workspace 内所有用户共享，不同 Workspace 和 tenant 隔离。

同文件中的 `GlossaryRepository` 借用共享 Engine，负责词典记录读取、首次写入和带行锁的事务更新。`Glossary` 负责默认 JSON 加载、Schema 校验、词条增删改查及 graph label 成员关系；表声明与初始化分别由 `schema/relational.py` 和 `CMDatabase` 管理。

内置 JSON 只用于首次初始化；已有记录（包括空词条集合）不被重启或新版 JSON 覆盖。每次查询读取数据库最新值，修改在同一事务内锁定 Workspace 记录、读取并更新，避免并行新增词条相互覆盖。运行期不写 JSON 文件。

```python
from context_manager.schema import ENTITY, GRAPH_SCHEMA, Term, Glossary

from context_manager.db import CMDatabase

CMDatabase(sql_engine).initialize()
glossary = Glossary(
    GRAPH_SCHEMA, sql_engine, tenant_id="tenant-1", workspace_id="workspace-1"
)
available: tuple[Term, ...] = glossary.list_terms(ENTITY, "type")
entity_types = tuple(term.value for term in available)

glossary.upsert_term(ENTITY, "type", "company", description="公司或法人主体")
glossary.set_graph_labels(ENTITY, "type", "company", frozenset({"Core"}))
glossary.remove_term(ENTITY, "type", "company")
```

Knowledge extraction 使用标签描述约束分类输出，并将经过校验的新标签写入当前 Workspace 的数据库记录。Store 在节点 create/patch 时通过 `resolve_term` 校验受管属性并物化 graph labels。

## Knowledge Graph

Knowledge Graph 的基础节点仍然只有 `Entity` 和 `Event`。KG 内部只声明五个 Neo4j relationship name，并以八个 endpoint-qualified `EdgeSpec` 覆盖精确方向：

- `PARTICIPATION`：Entity → Event；
- `HIERARCHY`：上位 Entity → 下位 Entity；
- `COMPOSITION`：整体 Entity → 部分 Entity；
- `INFLUENCE`：Entity/Event 的四种有向组合；
- `ASSOCIATION`：Entity → Entity。

旧 KG 内部的 `HAS_INSTANCE`、`RELATED_TO`、`ABOUT` 和 `CAUSES` 不再属于 Schema。跨图的 User → Entity/Event 关系继续保留。Trace 节点与关联边已退役，Schema 版本更新为 16；此声明更新不删除存量图数据或索引。

`DATA_SOURCE` 与其他节点一样使用 `key` 作为唯一主键；CM Auth-owned `datasource_id` 用于业务筛选和数据源 scope。`datasource_name`、`datasource_type` 对齐 `semantic_config.datasource` 的展示信息。Schema 不声明 `config`，连接配置仅从关系库读取，不写入图节点。

Metadata Graph 在 `graph.py` 中独立声明 `DataSource`、`Domain`、`Dataset`、`DatasetField`、`Dimension`、`Metric`、`MetricCaliber`、`VerifiedQuery`、`Table`、`Column` 及其关系。字段词汇与 MG 建模规范 v2.1 对齐：`aliases→synonyms`、`dimension_type→dimension_role`、`metric_key→metric_name`、`source_channel→source`，新增 `datasource_scope`、`datasource_id`、`DatasetField.dataset`、横切 `updated_at`，删除 `status`/`source_trust`/`valid_to`/`DatasetField.sample_values`。Schema 不 import `semantic_config` 的表、Pydantic model 或模板合同；两者只在 `metadata.contract` 的显式快照边界接合。



## 属性语义

- `editable=True`：调用者可在创建和更新时提交；这是默认值。
- `editable=False`：属性创建后不可由调用者更新；属性值的来源由 `owner` 决定。
- `DisplayPolicy.ALWAYS`：id、key、name、display name 等关键识别字段应始终进入通用展示。
- `DisplayPolicy.CONDITIONAL`：普通业务属性由具体视图根据上下文决定是否展示；这是默认值。
- `DisplayPolicy.NEVER`：加密的 datasource config、原始 SQL/DDL、用户原文、工具参数/结果、sample value、embedding/hash 等隐私或内部属性不得进入通用展示。
- `PropertyOwner.CALLER`：值来自业务调用者。
- `PropertyOwner.SCOPE`：值由 Store 从 `ScopeBinding` 注入，调用者不得提交。
- `PropertyOwner.STORE`：值由 Store 根据 Schema 声明生成，调用者不得提交；`KeySpec` 固定属于此类。
- `nullable=False`：完整创建结果中必须存在且不能为 `None`。
- `ValueKind`：定义数据库无关的逻辑类型，包括 text、boolean、integer、float、datetime、JSON、text list 和 vector。

`editable` 和 `display_policy` 彼此独立：前者由 `GraphSchema.validate_operation` 执行写校验，后者供通用 API/界面投影使用。Store 和内部 datasource resolver 仍能读取完整 record；`NodeResponse` 会无条件剔除 `NEVER` 属性，避免把凭据或原始内容交给通用 HTTP 客户端。

每个 `NodeSpec` 的唯一 `KeySpec` 固定为非空、不可编辑、始终展示且由 Store 管理。Store 在创建节点时先注入 Scope 属性，再按 `KeySpec` 生成 `key`；后续节点寻址、边端点和 Neo4j constraint 都直接使用该 `key`。

支持向量检索的节点声明可选的 `embedding: VECTOR` 和 `embedding_hash: TEXT`。`NodeSpec.fulltext_fields` 是后端无关的全文检索输入，`NodeSpec.embedding_fields` 是生成节点向量时按顺序组合的文本输入；两者只能引用当前节点已有的 `TEXT` 或 `TEXT_LIST` 属性。声明 `embedding_fields` 时，`NodeSpec` 同时校验两个 embedding 持久化属性的存在和值类型。

物理层 `Table`、`Column` 只保留全文检索，不声明 embedding 属性或输入字段，导入和向量重建跳过这两类节点。逻辑层 `Dataset`、`DatasetField` 保留全文检索与 embedding。已有数据库的旧向量属性、列和索引不由 Schema 声明变更自动删除。

物理图后端读取这两个字段集合声明检索能力。Neo4j 创建全文和范围索引，在 Scope 内用向量相似度函数精确排序，默认不创建 ANN 向量索引；Holo 按声明创建全文索引及 HGraph 向量索引。向量维度由各后端 catalog 声明，模型输出必须与其匹配；模型、批次及调用配置由运行时 Embedder 提供。

`GraphSchema.validate_operation` 验证操作、属性存在性和写权限；`validate_values` 验证逻辑类型、可空性和完整性。

## 导出与版本

`GraphSchema.to_document()` 使用布尔 `editable` 和字符串 `display_policy` 导出通用属性事实，并导出每个节点有序的 `fulltext_fields` 与 `embedding_fields`；`KeySpec` 还会导出 `strategy`、`prefix` 和 `parts`。这些逻辑规则共同进入 `fingerprint`、部署检查和 `GET /api/v1/graph/get_schema` 响应。Python 声明是唯一 authoring path。

`GraphSchema.get_overview()` 为 Agent 提供 version、zones、nodes 和 edges。节点包含名称、所属区域、可展示的 `properties: [属性名, ...]`、全文与 embedding 输入字段；边包含有向 `(source, relationship, target)` 和同样形式的属性名列表。属性投影保留 `ALWAYS` 与 `CONDITIONAL`，`key` 作为普通属性名展示。节点、边、属性和搜索字段沿用声明顺序，每次调用生成独立的可序列化结果。

MCP 的 `get_graph_schema` 在 overview 上补充 backend、属性类型和当前用户的 query_scope；Neo4j 节点声明全文索引名，Holo 返回已授权的个人图名。Agent 据此通过 `query_graph` 提交原生只读查询。Query Service 检查范围后原样执行，不注入过滤条件或复制临时图；Store 在执行时验证 Scope 与目标。

修改逻辑 Schema 时：

1. 修改 `graph.py` 中的 canonical 声明。
2. 在受影响的业务场景中验证新增字段或关系的实际结果；公开 Schema 通过 `tests/cm/acceptance/test_mcp_semantic.py` 的消费流程验证，不复制完整声明快照。
3. 检查 Neo4j 物理约束/索引是否需要迁移。
4. 若持久化兼容性发生变化，提升 `GraphSchema.version`，不要在 `ensure()` 中隐藏迁移。

## 内部节点与恢复记录

内部节点统一使用 `Zone.INTERNAL`（`_internal`）：MGSyncState、RecoveryStackHead、UndoBatch。公开 `get_overview()` 省略这个 zone 和内部节点；`to_document()` 保留完整部署声明，公开 HTTP 接口须另行过滤内部声明。

恢复图节点沿用 `key`、`workspace_id`、`owner_id`、`zone` 四个公共属性。RecoveryStackHead 声明 `rid`、`recovery_state`、`head_seq`、`result_status`、`message`、`updated_at`；UndoBatch 声明 `stack_key`、`sequence`、`payload`。两者使用 deterministic key、允许普通 CRUD，业务属性均不对外展示。

`cm_recovery_runs` 仅声明 8 列：复合主键 `tenant_id/workspace_id/task_type/task_id`，以及 `rid`、`recovery_state`、`result_status`、`message`。`recovery_state` 表示恢复流程阶段，`result_status` 表示业务结果，`message` 仅保存可选警告或失败原因（transaction 层截断至 2,048 字符），不保存统计结果。图与 SQL 使用同名结果字段。不声明 requester、执行令牌、租约或额外 SQL 索引。执行流程和状态推进由 transaction 层实现，不属于 Schema 职责。登记不保存整轮写入范围；Undo 条目记录具体 owner/datasource，节点或关系的 zone 由 canonical Spec 确定，恢复权限由业务入口重新签发。

`result_status` 回答“业务任务执行结果是什么”，`recovery_state` 回答“恢复栈处理到哪一步”。例如 `result_status=SUCCESS, recovery_state=CLEANING` 表示业务成功、Undo 尚在清理；清理完成后只将 recovery_state 改为 CLOSED。`result_status=FAILED, recovery_state=RECOVERING` 表示业务失败、已提交批次仍在撤销。

GraphSchema 18 需要显式迁移旧 MGSyncState 的 zone，保留 key、归属及 state；调用方也须适配 `_internal`。旧版恢复记录应先用原版本收尾，再迁移 SQL 声明。Store ensure 不隐式迁移现有数据。

Undo 不保留编码版本字段。以后修改 Undo 编码前，必须使用旧代码完成所有旧栈的恢复或清理，再升级。

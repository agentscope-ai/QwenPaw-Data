# Store

`GraphStore` 是业务与图数据库之间的统一边界。公共输入准备负责 Schema、属性和 Scope 校验，
Neo4j/Holo AGE 负责直接执行数据操作；连接、池和事务生命周期由各后端 Runtime 管理。
Store 不承担业务去重、查询准入或事务计划编排，计划与恢复见 [Transaction](../transaction/README.md)。

## 数据与授权

`NodeDraft`/`EdgeDraft` 表达未持久化事实，ref 是 Plan 内引用，不是数据库 key；transient 不落库。
Store 生成 key 并写入 Scope-owned 归属属性。返回的 NodeRecord/EdgeRecord 是 detached、递归冻结的快照。
`GraphStore.access` 是执行时验证的同一个 Authority，应用须使用它签发 Scope。
每个操作在执行前验证 canonical Spec、Scope 权限及其物理目标，事务内遵守同一边界。

## Primitive 契约

| 操作 | 输入与返回 |
| --- | --- |
| `get_nodes` / `get_edges` | key 或端点对的批次；按输入顺序返回 Record，缺失或不可见保留 `None` |
| `create_nodes` | property map 批次；按输入顺序返回 key |
| `create_edges` | `(source_key, target_key, properties)` 批次；按输入顺序返回 EdgeRecord |
| `patch_nodes` / `patch_edges` | key/端点与 changes 批次；成功返回 `None`，未命中为空操作 |
| `delete_nodes` / `delete_edges` | key 或端点对批次；成功返回 `None`，未命中为空操作 |
| `list_nodes` / `list_edges` | 等值 filters 和有界 limit/offset；返回 Record 页 |
| `search_fulltext` / `search_vector` | 普通文本或向量；返回有序 NodeRecord，不返回 score |
| `expand_neighbors` | 一个起点、方向和分页；返回 `(NodeRecord, EdgeRecord)`，保留真实边方向 |
| `execute_query` | 后端原生查询和绑定参数；返回查询指定的列和值 |
| `initialize_user_space` | sync 准备个人空间；Neo4j 仅校验授权，Holo 初始化个人 schema |

每批使用一个 Spec 和 Scope。读取允许重复目标，写入拒绝同批重复目标；空批次校验后不访问数据库。
`create_edges` 只插入，不自动更新已有边；缺少任一可见端点时整批失败。
`delete_nodes` 不隐式 DETACH，调用方先显式删边。需要目标存在的 patch/delete 由业务在事务内先读检查。

普通批次独立提交；`atomic(callback)` 内的操作共用事务。任一写 primitive 失败都会使该事务不可提交，
即使回调捕获异常也整体回滚。错误为 `transaction_failed`；提交结果未知为 `commit_unknown`，不自动重放。

## 属性、投影与搜索

- create 省略 nullable `None`，patch 的 `None` 清空属性，等值过滤的 `None` 匹配未设置属性。
  时间点统一为微秒精度的 UTC ISO 字符串，无时区时间拒绝；纯日期保持日期含义，不在读取时迁移旧数据。
- 输入 map 只浅拷贝并归一必要字段；返回快照递归冻结。向量写入与搜索共同校验维度、有限值和非零范数。
- `fields=None` 返回全部属性，空序列只返回身份，非空序列返回身份及指定属性，缺失字段为 `None`。
  `expand_neighbors` 分别接收 node_fields/edge_fields；投影不改变授权、过滤和分页。
- 搜索的 specs 为可选 NodeSpec 序列，省略时检索 Scope 内声明相应字段的类型；
  显式传入时只搜索指定类型，重复类型去重，空序列不查询；filters 仍须通过 Schema 校验。
  `filters_by_type` 在显式 specs 下按类型名补充等值条件，解决同一业务条件在不同类型上的属性名差异；
  与共同 filters 合并后逐类型校验，未选择的类型或冲突条件报错。Store 不解释业务域或绑定关系。
  全文输入是普通文本，不解释调用方的布尔、字段或通配符指令。
- 类型选择和 Scope、属性过滤均在取 Top-K 前生效。全文按原始分数降序合并，向量按相似度降序合并；
  分数相同按类型名、key 稳定排序，limit 为最终总数。不设类型配额，不做跨索引分数归一化。
  后端分词、停用词、ANN 召回和索引可见性允许不同，不保证填满 limit。
- Entity 的 Core 成员关系来自 Glossary，create/type patch 同步维护后端投影。
  Glossary 变更后的旧记录回填由显式迁移处理。

## Neo4j

[neo4j/store.py](neo4j/store.py) 使用批量 UNWIND，每个 primitive 直接执行对应 Cypher。
用户共享图，通过归属属性过滤；physical_schema 为每种节点建立 `key` 唯一约束，
同一物理数据库内同标签的 key 不允许跨 Workspace 重复。初始化不自动删除已有约束或迁移数据。
Core 使用附加 label，全文使用属性索引并在 limit 前过滤归属。
向量检索先限定 Scope，再用 `vector.similarity.cosine` 精确评分、排序；不截取全图 ANN 候选，
避免其他用户的数据挤占名额。代价是计算量随授权范围内的向量数增长；向量仍保存在节点属性中。
可向量检索的标签具备 `(workspace_id, owner_id)` 范围索引，跨 datasource 检索不依赖可空的 ID 列。
向量维度由 catalog 的 embed_dim 声明，默认 1024；不创建 ANN 向量索引，已有旧索引由显式迁移清理。
`neo4j/physical_schema.ensure_schema` 显式补齐约束与索引，并等待当前 catalog 声明的索引就绪。
两个后端的 Store 构造函数均不执行公共 DDL；初始化由 Workspace 开通任务调用。
同名旧结构的变更由迁移处理。Runtime 长期持有 driver，管理连接池、读写路由和关闭。

## Holo AGE

开发优先复用用户提供的 AGE SQL 参考文档。
[holoage/store.py](holoage/store.py) 的固定数据 primitive 直接写一条 SQL / AGE SQL：
读取使用 `cypher(graph, query, params) AS (...)`，批量 DML 使用带类型的 VALUES；
`patch_*` 合并/清空 agtype 属性，`delete_nodes` 使用 AGE DELETE 保留关联边约束。
搜索使用 Holo 函数，跨类型或跨图读取在同一数据 SQL 中 UNION ALL 后统一排序、分页。
固定读取在数据库输出处把属性 map 转为 JSON，不对整份属性额外深拷贝或反复编解码。

Workspace target 固定注册的 global 图；个人 schema 原样命名为 `<tenant_id>_<workspace_id>_<owner_id>`。
个人操作先查询数据库目录，核验 schema comment 中完整归属和初始化完成标记，再执行数据 SQL。
同名但归属不同的图不能复用，归属不缓存在进程内；全 Workspace 读取从目录发现已完成的个人图。
`sync` 调用 `initialize_user_space` 分配空间，普通 CRUD 不隐式建图。
缺少个人图、归属标记尚未写入或存在匹配的初始化标记时，读取抛出 `StoreError`，code 分别为
`user_space_uninitialized` / `user_space_initializing`，由 API 映射为未就绪状态；
空归属标记按未初始化处理，不允许读取数据或自动认领已有图；明确的归属不匹配仍抛出
`PermissionError`。初始化标记表示尚未完成，
不保证后台任务仍在运行。历史个人数据迁移尚未处理。

`physical_schema.py` 管理图、标签、列与索引的幂等 DDL；并发冲突仅在目录确认对象存在后接受，
中断后可补齐剩余对象。完成的个人图不重复建索引或 VACUUM，结构升级由显式迁移处理。

两类过渡投影均在建列及写入位置标注移除 TODO：

- `properties.embedding` 保留业务属性，`plot_embedding float4[]` 供 HGraph 索引；同一写入同步两份，
  未传 embedding 时保持原值，显式 null 时一起清除。
- Entity 的 `is_core boolean` 代替多标签，随 create/type patch 同步；不提供 Cypher `:Core` 语义。

全文使用 content 列与 jieba，向量使用 HGraph/Cosine。Holo 不保证业务 key 或边端点并发唯一性；
平行边按端点 get 选取一条，patch/delete 匹配全部；重复身份的选择和分页不保证与 Neo4j 一致。

Runtime 长期持有有界 psycopg 池，归还连接时恢复修改过的超时；个人路由不进入 Runtime。
写事务依赖目标实例的 mixed-DML 支持，单条写语句上限 30 秒，原生查询为 10 秒；
其他锁限制仍由数据库决定，连接池配置见 [Settings](../settings/README.md#holoage-connection-settings)。

## Workspace 开通与存量初始化

控制面 `workspaces` 表承担开通任务队列。worker 原子认领 `queued → provisioning`，
提交认领事务后分配或复用图连接，调用后端 `physical_schema.ensure_schema`，成功后才发布
`instance_status=ready`、`status=active`。异常由 worker 记录为 `failed`。
Neo4j 等待索引上线，Holo 保留完整 DDL 与 VACUUM 流程；sync 仅初始化个人空间并同步数据。
外部 provisioner 同样须在上报完成前调用 Store 初始化函数；Auth `/resume` 保留原有
“外部开通已完成”的上报契约，不执行图初始化。

本地和评测的 `scripts/start.sh` 使用同一个 worker。存量 Workspace 在上线新业务进程前停止业务流量，
通过以下显式入口补齐 schema（仓库根目录执行；控制面由 `QWENPAW_DATA_DB_URL` 指定）：

```bash
uv run python scripts/common/workspace_provisioner.py --initialize-all
# 或只处理一个已登记连接的 Workspace：--initialize-workspace <id>
uv run python scripts/common/workspace_provisioner.py
```

初始化命令仅入队；已有 worker 时无需重复启动。确认目标全部 ready 后恢复业务流量。
已登记的实例会复用连接与凭证，不重新分配实例；queued/provisioning 项不会重复入队。
失败项可通过同一入口重新入队。进程崩溃遗留的 provisioning 不自动抢占，须确认原 worker
已终止后人工恢复队列状态。固定连接的本地环境用 `scripts/common/initialize_local_graph.py`
在启动业务前显式准备图；该入口也调用上述 Store 初始化函数。

## 原生查询

`execute_query` 原样执行 Neo4j Cypher 或完整 Holo AGE SQL，不重写范围、不推断返回列。
调用方负责语句准入与数据范围；MCP 先经 [Query Service](../cypher/README.md) 校验只读及当前用户范围。
Neo4j 用 EXPLAIN 检查只读，Holo 使用只读事务；两端复用已有连接池，无需临时图或账号。
Holo 返回驱动原始列值，普通值或 JSON 转换由 SQL 显式声明，参数不能绑定在 dollar-quoted Cypher 内。
Explorer/Metadata 所需原始分数和 Core 读取暂留 [legacy_reads.py](legacy_reads.py)。

[legay_cypher.py](legay_cypher.py) 承接 Explorer 的固定批量读查询，返回列由调用方显式声明，
只返回标量及属性 map。执行前校验 Scope/target，Holo 通过现有归属检查路由授权图，
每图在 SQL 中排序、限量和 JSON 转换，再合并有界结果并应用总上限；
查询本身须约束每个节点及关系的授权范围。
不做任意 Cypher 解析、返回列推断或节点/边对象解码，不作为用户查询入口。

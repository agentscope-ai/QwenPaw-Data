# Compatibility

本模块向旧前端、Host 和尚未迁移的调用方提供协议投影与组合查询，复用当前 Schema、Scope 和 Store。
Runtime 负责装配和资源生命周期；兼容层不创建连接池、不拥有持久化模型。
HTTP 路由见 [API](../api/README.md#legacy)，当前工具集见 [MCP](../mcp/README.md)。

## Metadata

`metadata/reader.py` 承接 domains、search-metrics、dimensions 和 datasets 四个 HTTP 查询；
`projection.py` 转换旧字段，`queries.py` 保留尚有调用方的组合查询，不再作为 MCP 暴露。

- Domain 名称与稳定 ID 分开解析，Metric synonyms 映射为 aliases；Dimension 的 `OLAP维度`
  是展示分类。Dataset 类型和物理表来自 source，数据源类型从当前 Workspace 的 SQL 配置读取。
- datasource 和 Dataset 锚点按关联筛选业务对象，不扩大 Workspace/owner 授权范围。
  完整锚点唯一命中才收窄；详情的显式名称优先。缺数据源或无关联的详情返回 404。
- 指标候选保留原有软筛选和同域回退；全文在 top-k 前过滤过期内容，并保留原始分数和可信度排序。
  向量候选使用 primitive，再计算既有相关性并合并排名；兼容参数由 `metadata/relevance.py` 管理。
- 原始全文 score 经 `store/legacy_reads.py` 获取，因为公共 `search_fulltext` 不返回分数。
  仅明确的 `index_unavailable` 允许回退到文本匹配；连接、权限和查询错误正常上报。

## Explorer

`explorer/service.py` 提供画布、详情、搜索、Topics 和探索流程。首屏画布、Dataset 归属闭包和
schema 统计由 `queries.py` / `ds_closure.py` 执行固定批量查询，在数据库内筛选、限量和聚合；
边查询同时返回两端节点，复用既有响应投影规则。
详情、搜索、Topics 和探索流程仍由 `access.py` 组合 list/get/expand primitives，缓存仅限一次请求。
两条路径均使用当前 Scope；管理员可获准扫描所有用户图，个人 schema 的发现由 Store 完成。

画布保留 `{id,label,group,props}` 和 `{from,to,type,props}`。节点与边分别限量，补入边端点后
节点总数可能超过 `max_nodes`；达到边上限时不保证与旧查询选中同一子集。
Dataset 闭包只服务 datasource 筛选；默认域缺少 Domain 时回退到 DataSource。
内部字段和向量不展示，Domain ID 在页面边界转换为显示名。

全文锚点保留每类型 top-k、数据库 score 和跨类型排序；持久化 Core 统计使用 Neo4j 标签或
Holo 物理投影。两处固定读取暂留 `store/legacy_reads.py` 并标注过渡 TODO，不能用当前 Glossary
重算旧记录而改变结果。仅全部请求类型均缺索引时允许关键词回退，零命中不触发降级。
Topics 的临时向量不写回图，任务缓存按视角隔离。

固定查询经 `store/legay_cypher.py` 显式声明返回列，通过 `execute_query` 复用连接池；
Holo 按授权图分别在数据库内排序、限量，再合并有界结果并应用总上限。调用方对节点、关系和端点分别施加 Scope，
该入口不接受用户编写的 Cypher，也不恢复旧版临时图、返回列推断或图对象解码器。

只读 Cypher 控制台仅支持 Neo4j，借用 Workspace driver，保留原生图对象和表格结果。
AST 校验注入 Workspace/owner 范围，并排除内部恢复节点；成员读取本人及公共 owner，
管理员读取当前 Workspace，可用 `global_mg` 收窄。具体入口见 [API](../api/README.md#cypher)。

## 其他适配

`aligned_principal.py` 将可信 AuthorizationContext 投影为 Semantic Config/Profiling 暂需的 Principal。
`datasource_types.py` 从 canonical 配置模型生成旧表单目录，只补 UI 展示元数据。
这些适配在调用方迁移后整体删除，不在此复制认证或数据源配置逻辑。

## Raw Cypher

`explorer/raw_cypher/` 提供受限 Neo4j 读写。入口要求 `QWENPAW_DATA_RAW_CYPHER_ENABLED=1`、
当前 Workspace 的 Raw 权限及 `X-QwenPaw-Data-Breakglass-Token`。Auth 消费的一次性授权绑定用户、
tenant、Workspace 和原始语句/参数摘要；消费后即使执行失败也不可重放，审计不记录凭据和参数值。

读查询通过 AST 准入和范围注入；写查询只接受明确命名的 Entity/Event 及已声明关系，
服务端绑定归属参数。拒绝修改归属、key、标签、整个属性映射，以及 CALL、WITH、UNION、UNWIND、
子查询和 DETACH DELETE；函数仅允许白名单。管理员可明确选择 `__global__` 操作全局 KG。
适配器借用 Runtime driver，异常整笔回滚，默认语句超时 10 秒、上限 60 秒。

# API

API 负责 HTTP 参数、认证接入、Scope 获取和响应转换；业务流程由 Service 执行，资源由 Runtime 管理。
当前路由以 [Native router](native/router.py)、[Legacy router](legacy/router.py) 和运行实例的
`/openapi.json` 为准，MCP 工具契约见 [MCP](../mcp/README.md)。

`create_service_app()` 安装认证并挂载 CM、Semantic Config、Profiling 和 MCP；
`create_api_app()` 是供宿主装配的协议外壳，宿主须提供认证与 Runtime。
业务请求使用 Auth 返回的可信身份和当前 Workspace 权限，调用方提交的用户 ID 不授予访问权。
`/api/health` 和框架文档页不受业务认证中间件管理；MCP 可匿名发现，工具调用必须认证。

## Graph

`/api/v1/graph` 提供 Schema、节点列表、批量点查、节点/边修改和邻居展开。
成员读取本人图及允许的公共数据，管理员可获准跨用户读取；MG 权威图写入要求管理员，
KG/UG 编辑限定本人。节点缺失时批量读取保留对应的 `null`，输入与结果顺序一致。
批量修改先在同一事务中检查目标存在，再执行 Store patch；缺失返回 404，成功返回逐项完成结果。
底层批量契约见 [Store](../store/README.md)。

## RDB

`POST /api/v1/rdb/execute_sql` 在授权数据源执行只读 SQL，最多返回 1,000 行。
`GET /api/v1/cm/downloads/{result_id}.csv` 下载未过期的 SQL 产物，同 Workspace 成员共享。
数据源配置直接读取关系库，不要求先同步个人图。SQL 写入仅通过相应 MCP 工具提供，
管理员权限、语句类型和结果限制见 [SQL](../sql/README.md)。

## Config

`/api/v1/config` 管理当前用户的 LLM/embedding 配置并提供连接测试；响应隐藏凭据。
保存 embedding 配置只提示是否需要重建，实际模型解析和客户端生命周期见 [Model](../model/README.md)。

## Embedding

`POST /api/v1/embed/embed-all` 由管理员发起当前 Workspace 的全量向量重建，覆盖权威图和所有个人图。
成员可以读取同 Workspace 的任务状态；重试会重新校验请求者及原发起者权限和配置。
任务执行使用固定模型快照，分批提交与恢复契约见 [Embedding](../embedding/README.md)。

## Knowledge

`/api/v1/docs` 提供文档列表、上传、重新导入、原件下载和删除。
文档记录及原件仅对当前用户可见，管理员身份不扩大该范围。
上传和重导在保存原件及 processing 状态后返回，后台任务更新最终结果；同用户的并发文档操作返回 409。
状态、来源撤回及重试语义见 [Knowledge](../knowledge/README.md)。

## User

`/api/v1/ug/me` 维护本人资料和记忆；`/api/v1/ug/users` 为管理员提供跨用户只读视图。
记忆删除为撤销。`POST /api/cm/user/ingest` 处理当前用户、当前 Workspace 的待抽取消息。
类型、状态和手动编辑规则见 [User](../user/README.md)。

## Metadata

`/api/v1/mg/userspace` 提供本人 MG 同步、冲突决策和个人追加；管理员同步也只更新自己的图。
`POST /api/v1/semantic/feedback_card?mode=test|confirm` 预检或提交个人反馈。
织入任务、Excel 导入和编辑配置发布由 Metadata 协调 Semantic Config，相关入口在
[native/metadata](native/metadata/)；事务与失败语义见 [Metadata](../metadata/README.md)。

## Legacy

兼容路由继续服务旧前端与 Host，响应携带 `Deprecation: true`，有等价新入口时附 successor `Link`。
模型配置与重建路由转发 Config/Embedding；KG 编辑转为 Graph patch；数据源目录和 active selection
保留旧协议。四个 Metadata 查询及 Explorer 由 [Compatibility](../compatibility/README.md) 承接。
旧 Search、Trace/TG 和 Legacy/Curation MCP 不再挂载。

### Explorer

`/api/v1/admin/explorer` 提供画布、详情、搜索、邻居和 Topics；`/api/v3/cm/explore` 提供 SSE 探索。
结构化查询以签发的 Scope 为边界，管理员可查看所有用户图。数据源闭包是业务筛选，不代替授权。
接口前缀含 `admin` 不意味着所有操作只面向管理员，实际准入由路由与 Authority 决定。

### Cypher

`POST /api/v1/admin/cypher` 是 Neo4j 只读控制台：成员读取本人及公共 owner，管理员读取当前 Workspace；
`global_mg` 可进一步收窄范围。服务执行 AST 校验和范围注入，再借用 Runtime 的 driver。
该控制台不同于支持完整 AGE SQL 的 [MCP Query](../cypher/README.md)。
`POST /api/v1/admin/cypher/raw` 默认关闭，需开关、Workspace Raw 权限和一次性授权；
允许语法与审计要求见 [Raw Cypher](../compatibility/README.md#raw-cypher)。

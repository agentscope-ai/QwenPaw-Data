# Context Manager 1.0

CM 负责语义图的构建、维护和检索，为 Host 提供图查询与类型化 SQL 读写能力。
开发约定见 [架构说明](../../docs/architecture.md) 和 [测试规范](../../docs/test-instructions.md)。

## 模块导航

| 模块 | 职责 |
| --- | --- |
| [API](api/README.md) / [MCP](mcp/README.md) | HTTP 接口、MCP 工具、认证接入与协议会话。 |
| [Runtime](runtime/README.md) | 选择 Workspace，装配服务，管理共享资源与执行预算。 |
| [Metadata](metadata/README.md) | 配置导入、织入、个人 MG 同步与反馈；[Physical](metadata/README.md#physical) 负责源库探查和物理映射。 |
| [Knowledge](knowledge/README.md) | 文档解析、知识抽取、合并入图和来源撤回。 |
| [User](user/README.md) | 历史消息摄取、记忆协调、引用解析与手工编辑。 |
| [Embedding](embedding/README.md) | 工作区向量重建、任务状态与手动重试。 |
| [Cron](cron/README.md) | 持久化任务计划、领取执行权和定时触发。 |
| [Process](process/README.md) | 分块、抽取、候选解析和向量准备等共享算法。 |
| [Model](model/README.md) | 用户模型选择、Provider 凭据解析与模型调用。 |
| [Auth](auth/README.md) | 根据可信身份签发并验证图访问 Scope。 |
| [Store](store/README.md) | Draft/Record、图原语、atomic，以及 Neo4j/Holo AGE 实现。 |
| [Transaction](transaction/README.md) | 普通事务 Plan、原子执行与结果。 |
| [SQL](sql/README.md) | 数据源配置读取、只读查询、CSV 与后端连接。 |
| [Schema](schema/README.md) | canonical 节点、边、属性与 Workspace 词汇表。 |
| [Settings](settings/README.md) | 部署配置的读取、解析与校验。 |
| [Compatibility](compatibility/README.md) | 旧协议投影，以及 [Explorer](compatibility/README.md#explorer) 查询。 |
| [Migration](migration/README.md) | 历史数据预检查与显式迁移；启动时补齐已部署控制面表的心跳列。 |
| `heartbeat` | 在途行心跳刷新、事务内范围锁，以及心跳过期后将仍标记为运行中的业务行记为失败。 |
| [Utils](utils/README.md) | 仅依赖标准库的通用叶子工具。 |
| `executor.py` | work unit 的准入、排队、线程执行及取消收尾。 |
| `db.py` | CM 共享 SQL 连接池、自有表初始化和外部表依赖检查。 |
| `server.py` | 加载部署配置，启动独立 HTTP/MCP 服务。 |

## 调用与提交

```text
HTTP / MCP → Runtime 选择资源并签发 Scope → 业务 Service
                                          ├─ Model / Process
                                          ├─ Store → Neo4j / Holo AGE
                                          └─ SQL → 数据源
```

Runtime 决定业务使用的执行池，Service 组织业务流程和事务边界。
同一 Runtime 的 Workspace 共用执行预算；Scope 将数据访问限定到获准的 tenant、Workspace 和 owner。
Workspace 的逻辑隔离与物理数据库部署分别配置。

MG 导入、织入和 embedding rebuild 逐批准备与提交，失败处理见 [Recovery](transaction/README.md#recovery)。
KG 导入、UG 单条消息事实写入及个人 MG 同步保留各自事务边界。
SQL 配置、图数据和任务状态分别提交，具体失败与重试规则见业务模块。

## 服务入口

独立服务使用 `api.app.create_service_app()`，同时挂载 CM、Semantic Config 和 Profiling。
嵌入场景使用 `create_api_app()`，由宿主提供认证与 Runtime；启动和关闭方式见 [Runtime](runtime/README.md)。

`QWENPAW_DATA_HOME` 保存用户原件和查询产物，`QWENPAW_DATA_DB_URL` 指向共享 SQL。
从 develop 切换时，SQL CSV 与 Profiling 产物可复用原目录；历史向量任务记录需单独迁移。

# Runtime

Runtime 选择 Workspace、装配业务服务，并管理共享资源的启动、执行预算和关闭。

## 资源归属

```text
ContextRuntime
  ├─ CMDatabase / CronManager / EmbeddingJobManager / IngestAdapter
  ├─ RecoveryService：活动登记、后台恢复与持久回执
  ├─ ExecutionResources：graph_read / background / sql
  └─ WorkspaceResolver → WorkspaceRuntimePool
                           └─ WorkspaceRuntime
                               ├─ GraphStore / GraphScopeAuthority / Glossary
                               ├─ RdbConnector / SqlQueryService
                               └─ KnowledgeImportService 等共享业务服务
```

API 使用 `ContextRuntime` 选择资源。Resolver 校验目标并解析连接，Pool 按
`(tenant_id, workspace_id)` 缓存 WorkspaceRuntime。每个 Workspace 共享 Store、Glossary
和 KG 导入服务；ModelService、MG importer 和 UG CRUD Service 按调用装配。
Workspace 的逻辑范围由 Scope 保证，物理数据库资源可以共享。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `execution.py` | 装配和关闭三组执行预算。 |
| `context.py` | 定义 app 唯一可见的 `ContextRuntime` façade；内部完成 Workspace 选择。 |
| `resolver.py` | 统一 Workspace 解析，查询注册信息或使用显式连接，返回 Pool 中的 Runtime。 |
| `connection.py` | 定义公共 `GraphConnectionConfig` 及 Neo4j / HoloAge 连接配置；只保存路由与连接参数，不拥有资源。 |
| `workspace.py` | 组合一个 Workspace 的 Store、SQL、Glossary、LLM 和 application services，并创建、缓存和关闭 `WorkspaceRuntime`。 |
| `../settings/runtime.py` | 提供本地固定图入口的环境配置。 |
| `startup.py` | 装配 Store 与 WorkspaceRuntime，并组合唯一的 Resolver、Pool 和 ContextRuntime。 |

公共入口、Resolver 和 Pool 依赖 `GraphConnectionConfig`，WorkspaceRuntime 的目标使用
Store 层已有的 `ExecutionTarget` 接口。具体连接配置生成对应的后端 target；只有注册解析
和启动装配区分 Neo4j / HoloAge。依赖方向为 application runtime → store，
Store 不导入应用连接配置。

## 应用入口

API 不取得 `WorkspaceRuntime`。Scope 签发、非图 model config 和文档目录选择接收认证后的 `AuthorizationContext`；图资源 façade 都只接收已签发的 `ScopeBinding`：

```python
scope = runtime.issue_graph_read_scope(
    authorization,
    datasource_id=datasource_id,
    zone=zone,
)
store = runtime.graph_store(scope)
query_service = runtime.sql_query(scope)
glossary = runtime.glossary(scope)
importer = runtime.knowledge_importer(authorization)
documents = runtime.knowledge_documents(authorization)
models = runtime.models(authorization)
async with models.open() as (llm, embedder):
    answer = await llm.complete(messages)
```

图资源路由只消费已签发 Scope 的 tenant/Workspace，不重复校验其真实性或读写权限；图授权在签发处决策，Store 在实际执行前校验并落实隔离。派生 Scope 通过 Runtime 绑定的现有 Authority 接口签发，业务层保留用例范围一致性检查。

图资源选择调用 `WorkspaceResolver.resolve(tenant_id, workspace_id)`；Resolver 验证 tenant/Workspace，并通过内部 Pool 返回对应的 Workspace Runtime。模型配置由 `ContextRuntime.models()` 直接装配，使用认证身份与 Workspace 有效性校验，只依赖共享 SQL、Provider 和模型执行资源，不解析图凭据或要求图实例就绪。`ContextRuntime.fixed_target` 用于固定单 Workspace 模式下约束访问目标；动态模式返回 `None`。

## 服务装配

本地单 Workspace 模式：

```python
local = get_settings().runtime
connection = Neo4jConnectionConfig(**local.model_dump(exclude={"environment"}))
application = create_service_app(connection=connection)
```

动态模式：

```python
application = create_service_app()
```

两种模式都会在进程启动时先构造并绑定 `ContextRuntime`。两种模式使用同一个 `WorkspaceResolver`：

- 显式传入 connection 时，只接受其中指定的 tenant/Workspace；
- 未传入 connection 时，按认证后的 target 查询共享 SQL 中的 Workspace 注册信息，并在每次解析时检查其状态；
- `WorkspaceRuntime` 仍在首次需要图能力时懒创建，并在缓存前执行窄 health read；失败资源立即关闭，下次请求可重试。

Pool 按 `(tenant_id, workspace_id)` 共享正在进行的初始化：同进程的并发请求等待
同一次创建与 health 检查，成功后复用同一实例，失败时收到该次异常；后续请求可重新尝试。
不同 Workspace 的初始化可并行。创建线程记录带目标、阶段和原因链的失败日志。
关闭期间完成的初始化不会再发布资源，而是释放资源并通知等待者。此协调只作用于
当前进程，不协调不同 worker 或 Pod 的 schema DDL。

独立服务由 `api.app.create_service_app()` 挂载 `semantic_config`/`profiling` routers。Runtime 启动时先初始化 CM 自有表，再调用外部模块的建表入口并校验依赖。外部编辑器的异步 engine/session 由各自模块拥有。

动态路径只读取进程共享设置：

- `QWENPAW_DATA_PREFERENCES_MASTER_SECRET`：与 Host 一致的 64 位十六进制主密钥，用于模型凭据。开发环境未配置时使用 Host 默认值；生产环境必须显式配置。
- 内置 Glossary JSON 仅用于初始化 Workspace 标签；
- `QWENPAW_DATA_HOME`：用户文件根目录，默认 `~/.qwenpaw-data`；启动时解析并传入 Workspace，文档请求不重新读取环境变量。
- `QWENPAW_DATA_DB_URL`：与 Host 共享的数据库，也持久化 Workspace 词典和 KG 文档目录、状态。
- 可选 `EMBED_DIM`：用于图处理配置及 Holo catalog 的向量维度，Holo 未设置时使用 1024。当前 Neo4j 装配使用默认 catalog 的 1024 维，不随该环境变量调整。用户模型输出必须匹配 Store 的维度；修改配置不会迁移已有向量、列约束或索引。用户模型及凭据来自数据库中的模型选择与 Host Provider。

tenant、Workspace、Neo4j URI/user/password/database 来自认证结果及 control-plane 受信引用。`secret_ref` 支持 `file://`、`env://` 或 `env:` JSON 文档；Resolver 通过 `Settings.environment_value()` 读取环境引用。Neo4j 连接凭据在 `Neo4jConnectionConfig` 的 `repr` 中隐藏。

文件根目录与 embedding 维度由 Runtime 装配时从 `settings.paths`、`settings.embedding` 读取，连接对象只携带图连接信息。`settings.runtime` 提供本地固定图入口的配置；`create_context_runtime(connection)` 创建固定 Workspace 入口，不传 connection 时使用共享 SQL 注册信息。

## 服务与授权

`ContextRuntime` 使用目标 Store 的 `GraphScopeAuthority` 签发 Scope，Store 再校验其
issuer、tenant、Workspace、owner、zone 和 datasource。授权规则见 [Auth](../auth/README.md)。

- Workspace 的 `Glossary` 持久化在共享 SQL，首次装配时用内置 JSON 初始化缺失记录。
- ModelService 按身份读取配置，异步上下文持有一次操作的客户端，结束时关闭；配置变更影响后续操作。
- KG 导入服务由 Workspace 共享；文档查询、删除服务按认证用户装配，原件和目录限定本人。
- `sql_query(scope)` 返回目标 Workspace 的 SqlQueryService，查询与 CSV 契约见 [SQL](../sql/README.md)。

## Recovery 装配

`start()` 在 SQL 初始化后装配一个进程共享的 `RecoveryService`，通过 `runtime.recovery`
提供给调用方。它使用共享 SQL 的 `RecoveryRunRepository` 和 `background` 执行池。
启动后获取的 Metadata 导入、织入和 Embedding rebuild Service 注入同一 RecoveryService。
织入与 Embedding 将其作为必需构造依赖，未启动的 Runtime 不提供这两个 Service；操作时可通过 `recovery_session` 接收
上层持有的 RecoveryStackSession，复用该轮次而不另建栈。
正向执行和后台扫描共用该实例，活动登记覆盖所有 Workspace；本进程仍活跃的轮次不会被扫描接管。

每轮装配先由 Resolver 按登记中的 tenant/workspace 解析目标，再由
`WorkspaceRuntime.recovery_stack(rid)` 绑定该 Workspace 的 Store 与系统身份
`__system__`。系统身份仅由 Runtime 在进程内构造，不来自请求、原任务用户或 Undo。
它只授权已解析的 Workspace，复用现有 `issue_admin_scope()`：

- 控制权限限定到 `__global__` owner 和 `_internal` 区域，供持久栈头和 Undo 日志使用；
- 读取权限覆盖该 Workspace 的 owner，供旧值读取和关系覆盖检查使用；
- Undo 所需的 owner/datasource 写入权限通过绑定同一系统身份的回调签发；日志中的范围只选择数据，不能授予权限。

后台不加载原用户或模型，不按 `task_type` 构造业务服务。目标未注册、停用或资源不可用时，
沿用 Resolver 的拒绝行为，恢复记录保留，错误由 RecoveryService 记录。
用户正向写入继续使用现有 Runtime 签发入口及用户身份；Store 在执行前鉴权。
Metadata 导入、织入图写入、Embedding 和 semantic_config weave 任务均已接入；业务任务层持有恢复会话，任务 Repository 根据持久回执同步终态。
Embedding 的 Scope 工厂在 Runtime 绑定本次请求者的可信身份和目标 Workspace，新建和显式重试使用同一签发路径；
动态 owner 写入复用现有 ADMIN 签发接口，业务 Service 不自行调用 Authority。
协议、运行前提和 Schema 能力缺口见 [Recovery](../transaction/README.md#recovery)。

## 启动与关闭

`create_service_runtime()` 配置进程 SQL 初始化与释放；`create_context_runtime()` 借用宿主已初始化的 SQL，由宿主负责释放。两种入口的 `start()` 都启动 RecoveryService 和 Cron，不扫描或续跑 Embedding Jobs；通用后台只根据已登记的恢复状态弹栈或清理。`aclose()` 收尾自身任务和 Workspace。初始化期间收到取消时，等待初始化工作结束，再释放已取得的资源。

API lifespan 调用 Runtime 的 `start()` 和 `aclose()`，并管理 HTTP／MCP 会话。Runtime 的关闭顺序是：

1. 取消并等待 CronManager 管理的本进程任务；
2. 停止 Embedding Jobs 并等待其任务清理完成；
3. 停止 RecoveryService 的新准入和后台扫描，等待扫描中的数据库工作及当前活跃轮次退出；
4. Workspace Pool 调用各 Workspace 的 `stop_services()`，收尾其持有的 Topics 和 KG 导入服务，此时执行池、Store 和 SQL 仍然可用；
5. 关闭各执行池的准入，清退等待准入的单元，并等待在途工作结束；
6. Workspace Pool 调用各 Workspace 的 `close()`，释放 Store/Neo4j driver；
7. 停止模型用量记录器，再释放本 Runtime 启动的共享 SQL 连接池。

关闭步骤通过资源退出栈组织，一个模块收尾失败时，其他资源仍会执行关闭。启动过程中失败也进入同一清理路径。

同步 `close()` 只供尚未启动异步服务的直接嵌入场景使用。Runtime 启动恢复服务后拒绝同步关闭，避免在扫描或补偿尚未结束时释放数据库。嵌入已有 FastAPI app 时，宿主如果不使用 `create_api_app()` 的 lifespan，必须自行等待 `runtime.aclose()`。当前 `RdbConnector` 不持有全局 pool，因此无需额外关闭。

Workspace 缓存保留到进程结束；部署时需控制单进程访问的 Workspace 数量。

## 执行池

`executor.py` 提供 `BoundedExecutor`，`runtime/execution.py` 装配 `ExecutionResources`。
只有 Runtime 选择池；业务 Service 接收执行器，组织提交的函数与事务。
同一 Runtime 的所有 Workspace 共用三组预算，默认值和环境变量见 [Settings](../settings/README.md#资源预算)。

| 执行资源 | 主要调用 |
| --- | --- |
| `graph_read` | 图 HTTP/MCP 查询、UG 查询、个人 MG 状态、KG 目录与原件读取。 |
| `background` | MG 导入与织入、KG 导入/删除、UG 摄取/编辑、feedback、rebuild、个人 MG 同步、Autoexplore 及通用恢复的 SQL/图操作。 |
| `sql` | HTTP/MCP 用户 SQL，覆盖连接、查询、取数、结果转换与 CSV 保存。 |

work unit 指一次 `run()` 提交，从函数开始到结束，包含其中的同步步骤和异步等待。
`workers` 同时设置两个独立上限：已准入且尚未完成的 work unit 数、执行同步步骤的线程数。
异步模型等待占准入额度，线程可供其他同步步骤使用。

- `run()` 统一处理有界排队和准入；异步函数直接等待，同步函数交给 AnyIO 线程。
- 已准入单元通过 `run_sync()` 执行阻塞阶段，只申请线程额度。Service 在单元结束前等待自己的子任务。
- MG/rebuild 每批提交一个单元，KG 按文档、UG 按单次摄取、feedback 按操作提交。
  Autoexplore 的独立同步阶段、个人 MG 同步和直接图写也进入后台队列。
- `run_graph_read()`、`run_background()`、`run_sql()` 供应用入口提交完整同步操作，包含结果投影。
- 队列满、排队超时或关闭时记录池名和原因，并抛出 `BlockingIOError`；HTTP 映射为 503，MCP 返回工具错误。
- 排队取消会移除等待者；同步调用开始后，执行器等待实际结束再释放额度。
  SQL 的数据库超时由后端执行，排队超时仅限制准入等待。

FIFO 约束准入顺序，并发执行的完成顺序取决于实际耗时。预算按 Runtime 独立，
AnyIO 复用底层线程；同进程线程共享 GIL，数据库竞争和跨 Workspace 公平性仍需分别评估。
业务模块保存任务状态与租约，执行器管理调用期间的资源。

Embedding 的网络读取保持异步，响应解码、排序和向量校验合为一个同步步骤。
后台模型使用 `background.run_sync`，前台模型使用 `graph_read.run`。
KG/UG 租约心跳、释放和 checkpoint 等控制操作独立执行。
KG 服务获取时的 Workspace 注册查询与首次资源初始化仍沿用同步解析路径。

提交与异步关闭使用同一个事件循环。关闭先收尾业务，再关闭准入并等待在途工作，
最后释放 Store 和 SQL；`aclose()` 支持重复等待，调用者取消时继续释放资源。
底层空闲线程由 AnyIO 回收。Neo4j driver 设置 `notifications_min_severity="OFF"`，数据库错误照常传播。

# Settings

`context_manager.settings` 统一读取 CM 的部署环境变量，按职责提供配置对象。
目录采用扁平结构，字段名称、默认值及解析规则由各文件维护。

## 配置归属

Settings 将部署输入转换成经过校验的配置值，资源所有者负责执行预算。
连接、根路径、超时、并发、容量与初始化默认值放在这里；请求参数、用户配置和算法策略
由对应契约或业务模块管理。详细分层约定见 [架构说明](../../../docs/architecture.md)。

新增配置应说明运维用途、默认值、单位、有效范围、作用域与生效时机，区分可覆盖默认值和硬上限。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 聚合 `Settings`，通过 `get_settings()` 复用进程快照，通过 `clear_settings()` 显式刷新缓存。 |
| `db.py` | 读取共享数据库 URL、PostgreSQL 连接回收间隔和 SQLite 锁等待时间。 |
| `paths.py` | 解析 `QWENPAW_DATA_HOME`，默认 `~/.qwenpaw-data`，供业务模块组装文件路径。 |
| `model.py` | 读取模型 HTTP 超时、LLM SDK 重试次数、环境代理开关及模型凭据解密使用的部署主密钥。 |
| `embedding.py` | 读取模型调用、缓存、维度、重建任务租约。 |
| `execution.py` | 配置三组执行池的工作单元容量、等待数量和等待超时。 |
| `graph.py` | 图后端连接与部署声明，以及可分批图写入的单批行数。 |
| `knowledge.py` | 配置文档上传与解析上限、抽取并发和模型请求超时。 |
| `user.py` | 配置 User Graph 抽取并发与任务租期。 |
| `metadata.py` | 配置物理元数据缓存有效期和采样 SQL 超时。 |
| `sql.py` | 配置 SQL 下载结果的保留时间。 |
| `cron.py` | 配置执行并发、心跳期限及初始时、分、时区；调度对象由 `cron` 构造。 |
| `runtime.py` | 读取本地固定 Workspace 的图连接环境配置，通过 `settings.runtime` 按需加载。 |

## 加载与生效

服务入口调用 `Settings.load()`，模块通过 `get_settings()` 使用进程快照，部署变更需重启。
`clear_settings()` 仅清除配置缓存，已创建资源与已持有引用继续使用原值。
`settings.auth` 复用 AuthConfig；`settings.cron` 按需加载；
`settings.environment_value(name)` 读取 Workspace 注册信息引用的环境变量。

`QWENPAW_DATA_ENV` 与 `QWENPAW_DATA_PREFERENCES_MASTER_SECRET` 由 `settings.model` 读取。
生产环境要求显式配置与 Host 一致的 64 位十六进制主密钥；开发环境沿用 Host 默认值。
用户模型选择由 [Model](../model/README.md) 从数据库解析，Metadata 兼容查询参数见
[Metadata compatibility](../compatibility/README.md#metadata)。

## 资源预算

三组池均使用 `PoolSettings`，由同一 Runtime 的所有 Workspace 共用；多进程各持独立额度。

| 池 | workers | max_waiters | queue_timeout_seconds |
| --- | ---: | ---: | ---: |
| `graph_read` | 8 | 64 | 30 |
| `background` | 2 | 64 | 120 |
| `sql` | 4 | 16 | 5 |

- `workers` 为正整数，同时限制已准入 work unit 数和同步执行线程数。
  work unit 是一次 `run()` 提交的完整调用，包含同步步骤和异步等待。
- `max_waiters` 为非负整数，限制等待准入的单元数；0 表示满员即拒绝。
- `queue_timeout_seconds` 为有限正数，单位秒，仅限制准入等待。

环境变量格式为 `CM_EXECUTION_<GRAPH_READ|BACKGROUND|SQL>__<字段名>`，
例如 `CM_EXECUTION_BACKGROUND__WORKERS=2`。池在创建时读取配置；
排队、取消和释放语义见 [Runtime 执行池](../runtime/README.md#执行池)。

其他资源预算：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `KNOWLEDGE_MAX_UPLOAD_BYTES` | 52428800 | 上传原件字节上限，API 与 importer 使用同一预算。 |
| `KNOWLEDGE_MAX_DOCUMENT_PAGES` / `KNOWLEDGE_MAX_DOCUMENT_CHARS` | 100 / 500000 | 文档解析页数与字符上限。 |
| `KNOWLEDGE_MAX_CONCURRENCY` | 4 | KG 抽取并发。 |
| `CM_USER_MAX_CONCURRENCY` | 4 | UG 抽取并发。 |
| `CM_HEARTBEAT_INTERVAL_SECONDS` | 30 | 活着的 worker 刷新所认领行的间隔，单位秒，必须大于 0。进程启动时读取，修改后重启生效。 |
| `CM_HEARTBEAT_STALE_AFTER_SECONDS` | 300 | 心跳超过该秒数后，其他 worker 可以接手。必须大于刷新间隔。进程启动时读取，修改后重启生效。 |
| `CM_HEARTBEAT_SWEEP_INTERVAL_SECONDS` | 60 | 进程扫描并失败过期在途行的间隔，单位秒，必须大于 0。进程启动时读取，修改后重启生效。 |
| `CM_METADATA_CACHE_TTL_SECONDS` | 600 | 物理元数据缓存有效期，0 表示每次刷新。 |
| `CM_METADATA_SAMPLE_TIMEOUT_MS` | 3000 | 物理探查采样 SQL 超时。 |
| `LLM_HTTP_SDK_MAX_RETRIES` / `EMBED_SDK_MAX_RETRIES` | 2 / 1 | 两类模型客户端的 SDK 重试次数。 |
| `CM_SQL_RETENTION_SECONDS` | 86400 | SQL CSV 下载有效期与清理周期依据。 |
| `QWENPAW_DATA_DB_SQLITE_BUSY_TIMEOUT_MS` | 5000 | 共享 SQLite 连接锁等待时间。 |

Runtime 或独立导入服务在装配时读取 Settings，显式构造 `DocumentLimits` 与 `KnowledgeImportLimits`。
两个数据类保存本次操作的限制值。
分块、候选数量及单次图写入预算由 `KnowledgeImportLimits` 维护，支持显式传入单次操作参数。
Embedding 的 SDK 重试与 `EMBED_API_MAX_RETRIES` 控制的限流重试分别配置。

## 图写入批量预算

`GraphSettings.write_batch_max_rows` 对应 `CM_GRAPH_WRITE_BATCH_MAX_ROWS`，
默认 1000，有效范围 1～1000。它管理可拆分业务的一批写入量，不包含并发或排队参数，
不限制 Cypher 语句数、模型 HTTP 请求数或向量维度。1000 是当前支持上限，不是数据库容量。
恢复事务的业务输入加 Undo 另受 32 MiB 上限约束；较宽属性或较高向量维度可能需要缩小批次。

MG 导入和物理反射按当前事务的节点与边写入条目总数划批；rebuild 按节点数划批，
以 `min(page_size, write_batch_max_rows)` 读取、准备和提交。MG 在 Service 构造时、
rebuild 在执行开始时固定预算；请求参数只能缩小，不能放大。部署变更需重启。

新的可分批写图业务应消费这个预算，由 Service 保证依赖顺序、结果映射和失败语义。
KG/UG、个人 MG 同步和直接 patch 保留各自原子提交边界。
Store 的 `atomic()` 始终由调用方明确界定，批次划分由业务 Service 完成。

## HoloAge connection settings

`GraphSettings` also owns `GRAPH_BACKEND` (`neo4j` by default, or `holoage`) and
`HOLO_AGE_HOST` (`localhost`), `HOLO_AGE_PORT` (`5432`, range 1–65535),
`HOLO_AGE_USER` (`postgres`), `HOLO_AGE_PASSWORD` (empty, secret),
`HOLO_AGE_DATABASE` (`postgres`) and `HOLO_AGE_GRAPH_NAME` (`cm_graph`, reserved
shared graph name). Values are read when the process Settings snapshot is built;
changes require resource reconstruction or service restart. Existing Workspace
registrations remain authoritative for backend routing and physical graph names.
The legacy `holo_age` / `holo-age` deployment spellings normalize to `holoage`.
The following pool budgets are read when constructing each workspace-owned
Holo driver. They apply per workspace per process, so deployment totals multiply
with active workspaces and worker processes. Changes require resource recreation
or service restart; Settings never construct connections themselves.

| Environment variable | Default | Unit / range | Purpose |
| --- | --- | --- | --- |
| `CM_GRAPH_HOLO_POOL_MAX_SIZE` | 8 | connections, integer ≥ 1 | Maximum concurrent borrowed connections |
| `CM_GRAPH_HOLO_POOL_TIMEOUT` | 30 | seconds, finite > 0 | Maximum wait for a pool loan |
| `CM_GRAPH_HOLO_POOL_MAX_IDLE` | 300 | seconds, finite > 0 | Reclaim idle connections |

Pools start with zero connections and allow up to 64 queued borrowers. Runtime
owns pool closure, transaction binding and reset of its statement timeout on return.
Native graph queries borrow the same pool and run in a read-only transaction.
There is no Holo HTTP-proxy or read/write routing setting.

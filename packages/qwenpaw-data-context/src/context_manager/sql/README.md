# SQL

`SqlQueryService` 负责语句准入、数据源解析和有界结果，`artifact.py` 管理 CSV，
`connectors` 管理目标连接、原生事务与关闭。Runtime 提供共享 SQL 执行资源。

## 数据源与授权

`DatasourceRepository` 校验 Scope，按 tenant/workspace/datasource_id 读取关系库中的有效配置，
每次请求读取最新值；MG 只保存标识与展示属性，图中不存在连接凭据。
请求不依赖权威图或个人镜像已建立。Datasource 转为绑定目标的 RdbTarget 后不可隐式切换资源，
凭据不进入 repr、公开错误或日志。

ODPS 使用已保存的 AccessKey 或 STS 配置；与其他类型一样，执行不切换配置中的 project。

## 读取与 CSV

execute 只接受一条只读查询；通过多读一行判断截断。HTTP 上限 1,000 行，MCP 上限 10,000 行，
Profiling 使用其业务预算。MCP execute_read 默认 2,000 行，将有界结果写为 UTF-8 CSV，JSON 预览最多 20 行。
空结果不生成文件；结构化单元格为 JSON，空值为空单元格。

row_count 表示返回/预览行数，total_row_count 优先采用驱动计数；未知时使用读取行数，
截断时可能仅是 max_rows+1 的下界。其余输出包含 columns、rows、truncated、elapsed_ms 及 result_id。
查询超时由后端实现，不等于完整请求 deadline。

产物保存在 `QWENPAW_DATA_HOME/<tenant>/<workspace>/.cm/sql_downloads/<result_id>.csv`，
成功关闭查询与连接后原子发布，文件权限 0600。同 Workspace 成员可下载，默认有效期 24 小时。
下载逐次检查期限；非空导出最多每 60 秒触发一次清理，Runtime 的定时 Sweep 也会清理过期 CSV。
复用原 QWENPAW_DATA_HOME 可读取尚未过期的 develop 产物和链接，不自动迁移其他历史目录。

## 类型化写入

MCP execute_create 仅接受 CREATE TABLE/VIEW（含 CTAS），拒绝 OR REPLACE；
execute_modify 仅接受 INSERT/UPDATE，拒绝 WITH 前缀写、多语句和嵌套写。
写入要求 Workspace 管理权限及 WriteOwner.WORKSPACE Scope，个人图写权限不能用于业务库写入。

连接器提交一条已准入语句，异常回滚；RETURNING 读取有界，但不限制实际修改行数。
row_count 为驱动影响行数，未知为 -1；写工具不导出 CSV。
DDL 能否回滚及云作业行为依赖目标后端，工具不提供跨后端撤销。

## Connectors

支持 PostgreSQL/Hologres、MySQL 及兼容协议的 StarRocks/Doris/TiDB、SQLite、DuckDB、ODPS 和 BigQuery。
`RdbConnector.connect()` 创建请求期连接或 SDK client；`RdbConnection` 提供 query、execute、探活及幂等关闭。
生产调用在已授权 Service 内解析 Target，连接与游标在所属工作线程中开启和释放，返回结果不携带连接。
原生 DB-API 支持 cursor/transaction；native_client 只能在连接存活期间借用。

| 后端 | 读写与等待 |
| --- | --- |
| PostgreSQL/Hologres | 读取使用只读事务和 statement_timeout；写入使用独立写事务及语句期限 |
| MySQL/TiDB | 读取使用服务生成的 MAX_EXECUTION_TIME；MySQL 另用只读事务；写入使用驱动等待机制 |
| StarRocks/Doris | MySQL 协议，不开启事务；读取设置会话 query_timeout，SQL 准入保证只读 |
| SQLite | 打开既有文件，读取使用 query_only 和 progress handler；写入遵守 read_only 配置 |
| DuckDB | 以 read_only 打开既有文件并禁用外部访问；读取超时后 interrupt；不支持写入 |
| ODPS/BigQuery | 独立作业，使用 SDK 等待机制；结果有界读取并关闭 reader/client |

pyodps、Google SDK 和 duckdb 仅在使用对应后端时导入；BigQuery 保留计费上限。TLS 使用显式连接配置，
PostgreSQL 默认 prefer、MySQL 默认 preferred；需要校验服务器身份时配置 verify-full/verify-identity 与 CA。
接口见 [API](../api/README.md#rdb) 和 [MCP](../mcp/README.md)，下载保留期见 [Settings](../settings/README.md)。

## 超时分类

数据库报告的语句超时返回 `QUERY_TIMEOUT`，MySQL 客户端等待超时返回 `SQL_READ_TIMEOUT`；
两者都不代表数据量过大或数据缺失，不应原样重试。

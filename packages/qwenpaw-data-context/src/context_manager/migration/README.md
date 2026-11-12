# Migration

本包将历史持久化数据原地补齐为当前 Schema 和 Store 使用的格式。迁移在部署切换的维护窗口中显式执行，复用已配置的数据库连接，返回预检查问题和实际更新数量。后续迁移按对应的数据对象在本包增加模块。

## User Graph

`migrate_user_graph(runtime, dry_run=True)` 接收绑定目标 tenant、workspace 和明确 database 名称的 `Neo4jRuntime`。一个 Workspace 对应一个 Neo4j database；连接及其关闭由调用方负责。

本入口补齐字段，不重命名关系。旧版 `HAS_MEMORY` 应在运行本入口前显式迁移为 `HAS`。

迁移覆盖以下字段：

| 对象 | 补齐方式 |
| --- | --- |
| `User.key` | 使用当前 `USER` 的 `KeySpec`，由 `workspace_id`、`user_id` 生成。 |
| `UserMemory.key` | 使用当前 `USER_MEMORY` 的 `KeySpec`，由 `owner_id` 和已有业务 `id` 生成 `mem:<owner_id>:<id>`；同时转换旧 `mem:<id>`。 |
| `HAS`、`REFERS_TO` | 从起点补齐关系缺失的 `workspace_id`、`owner_id`、`zone`，让新版 scoped 邻居查询读取原关系。 |

更新补齐缺失字段，并原地转换已知的旧 `UserMemory.key=mem:<id>`；其他不符合预期的已有 key 仍报告冲突。原节点、业务 ID、Profile、记忆内容、向量、状态、时间戳和关系保持原样。重复执行时已迁移对象的 `pending`、`updated` 均为零。

预检查会报告缺少身份字段、重复 key、已有字段冲突、端点归属不一致，以及引用目标缺少 key 等问题。引用的 MG/KG 节点沿用各自迁移流程，现有引用的可见性由当前授权策略决定。目标 database 中缺少 workspace 的 UG 节点会作为归属不明问题报告。

## 调用流程

1. 停止该 Workspace 的 UG 写入，并完成数据库备份。
2. 以部署运维身份取得该 Workspace 的 Neo4j 连接，明确 tenant、workspace 和 database。
3. 默认执行预检查，核对统计及 `issues`；处理已报告的数据问题后重新检查。
4. 使用 `dry_run=False` 执行。该调用在一个数据库事务中重新读取、检查并更新；预检查问题会抛出携带 `report` 的 `UserGraphMigrationError`，数据库写入失败则回滚整次修改。
5. 再次预检查，确认 `pending=0`，并通过用户资料、记忆列表和长期记忆接口验收后恢复服务。

```python
from context_manager.migration import migrate_user_graph
from context_manager.store.neo4j import GraphTarget, Neo4jRuntime

# driver is the deployment-provided connection for this workspace.
runtime = Neo4jRuntime(
    driver,
    GraphTarget("tenant-1", "workspace-1", database="workspace_1"),
)
preview = migrate_user_graph(runtime)
if preview["ready"]:
    result = migrate_user_graph(runtime, dry_run=False)
```

结果包含 `tenant_id`、`workspace_id`、`database`、`dry_run`、`ready`、`applied`、`nodes`、`edges`、`issues`。`nodes` 和 `edges` 按类型返回 `scanned`（扫描数量）、`pending`（本次需补齐数量）、`updated`（本次实际更新数量）。`issues` 提供对象类型、数据库 element ID 和原因，方便运维定位。

迁移直接通过 `Neo4jRuntime` 读取原始属性，再按 Schema 计算 key，因此能够处理尚未满足 `NodeRecord` 契约的历史节点。正式执行重新读取 element ID，预检查结果只用于审阅。

## 已部署控制面表

`stateless.py` 在进程启动时为已经存在的 `weave_task`、`probe_task`、`explore_scope` 和 `cm_recovery_runs` 补上心跳列，并收拢重复的探查范围行。新库由建表语句直接带上这些列。这组语句可以重复执行；对应的旧库不再存在之后，可以连同调用点一起删除。

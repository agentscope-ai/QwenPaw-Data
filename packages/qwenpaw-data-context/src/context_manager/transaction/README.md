# Transaction

本模块维护普通图事务的 Plan、执行器与结果，以及多批图事务的补偿恢复。
业务准备计划，Store 提供 CRUD 和 atomic；连接、SQL 回执及后台生命周期由 Runtime 装配。

## 普通事务

`StoreOperation` 保存准备好的 primitive 调用。node_refs 命名 create_nodes/get_nodes 结果，
node_key_refs 将前序 key 填入后续参数；Operation/Plan 构造时校验结构与引用，必需节点缺失使事务失败。

`Transaction.execute(plan, scope)` 在一次 GraphStore.atomic 中顺序执行，Scope 可共用或按操作提供。
Store 执行权限与范围检查，validate(result) 在提交前运行，异常回滚整批。
`execute_transaction_plan()` 提供同一契约的便捷入口。

`TransactionResult` 保存原始结果、ref→key 映射和统计。创建/读取统计实际结果，
更新/删除统计 updates_submitted/deletes_submitted 输入数，不代表数据库命中数；patch/delete 结果为 None。

## Recovery

`recovery/` 为多批写入保存 Undo，失败时逆序补偿。RecoveryService 借用共享 SQL engine、executor
及 Runtime 绑定目标的系统恢复能力；不重新构造原用户身份、不签发 Scope、不持有业务服务。

```python
async with recovery.run(scope, task_type="import", task_id=task_id) as execution:
    for plan in plans:
        await executor.run(execution.execute, plan, scope)
```

上下文在 executor work unit 外进入和退出，调用方须等待全部提交批次结束。
已有工作单元使用 run_sync 执行同步部分，避免重复准入。外部异常、取消和已捕获的批次错误均阻止成功决定。
降级或显式失败可通过 set_result 记录；最终以持久回执为准。

每批业务写入、UndoBatch 和栈顶推进共用图事务。成功先持久化图 COMMITTED，再更新 SQL 结果并清理；
后续 SQL 故障仅续接同步/清理，不能撤销成功结果。补偿按逆序提交 Undo 与游标推进，清理同样有界分批。
缺失必要日志或状态不一致时保留未决记录，不伪造成功。

SQL 回执绑定 task_id/rid 和 executor_pod，心跳覆盖准备、执行与收尾。
同 Workspace 的未 CLOSED 轮次阻止新 run；新鲜心跳不被接管，过期后以条件更新认领。
Runtime 的 RecoveryService 扫描遗留轮次，关闭时停止准入并等待活跃工作，再释放 Store、executor 和 SQL。
重试前由业务 Repository 消费旧 CLOSED 回执并关联 previous_rid，旧 worker 结果须仍匹配当前 rid。

## 支持边界

- Store 保持最终权限校验；恢复额外约束各操作属于本轮 tenant/workspace/owner/datasource/zone 范围。
  Undo 不能扩大权限，后台系统 requester 可不同于原用户。
- Plan 只接受具备逆操作能力的写入，行数受 graph.write_batch_max_rows 限制，业务输入加 Undo 不超过 32 MiB。
  删除需完整旧值和可重建身份，建边不能覆盖已有关系，删节点不隐式 DETACH。
- 每栈串行且固定 Workspace/数据库；跨批可见中间状态，仍须避免其他冲突写入。
  恢复不撤销 SQL 配置、模型调用或外部副作用，也不正向续跑业务。

测试入口为 `tests/cm/infrastructure/transactions/recovery/`，使用仓库 [测试规范](../../../docs/test-instructions.md)。
未配置真实图数据库时相关用例会跳过，不代表后端或进程退出恢复已经验证。

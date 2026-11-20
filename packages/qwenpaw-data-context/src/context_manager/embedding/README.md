# Embedding

单批大小统一读取 `graph.write_batch_max_rows`（环境变量 `CM_GRAPH_WRITE_BATCH_MAX_ROWS`，默认 1000，范围 1～1000）。准备与提交使用同一预算，配置变更需重启；它与并发额度独立。[配置语义](../settings/README.md#图写入批量预算)。

`embedding/` 负责工作区节点向量重建及其任务生命周期。管理员显式发起，覆盖该工作区共享 MG 和所有用户的私有 KG/UG；模型凭据来自本轮请求者的 Host Provider。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `rebuild.py` | 每次读取最多 `max_rows` 个节点，准备向量并提交，再读取下一批。 |
| `service.py` | 拥有新建与手动重试流程；消费 Runtime 注入的工作区资源和执行器，校验当前模型配置，在共享 RecoveryService 会话中执行 rebuild。 |
| `repository.py` | `EmbeddingJobRepository` 借用 CMDatabase 读写任务表，原子登记任务与执行租约，消费精确轮次的恢复回执并更新任务状态。 |
| `manager.py` | `EmbeddingJobManager` 执行显式提交的任务，维护租约心跳，关闭时收尾本进程任务。 |
| `__init__.py` | 导出 `embed_all`、`EmbeddingJobManager` 及任务身份和互斥异常类型。 |

```python
from context_manager.embedding import EmbeddingJobManager, embed_all
```

## 分批重建

Runtime 注入工作区 Store、模型访问函数、共享 JobManager、RecoveryService 和后台 executor。
Runtime 绑定本次请求的可信身份，新建和显式重试都由 Auth 的 `issue_admin_scope()` 决策并签发。
Service 中的 AuthorizationContext 只用于选择当前 tenant/workspace 的 SQL 任务。
模型与用量归属本轮请求者，在受理时保存公开配置快照；
执行期间复用同一个 embedder，任务结束关闭。模型配置变更影响后续任务或手动重试。

```text
RecoveryService.run（整轮）
  读取一页 → 准备文本 → 请求向量 → TransactionPlan → recovery_session.execute
  每次提交：本页更新 + Undo + 栈顶推进，处于同一图事务
         ← 每页完成后释放后台额度，下一页重新排队 ←
```

扫描声明了 `embedding_fields` 的节点，页大小为 `min(page_size, max_rows)`。
每页的准备、模型调用和提交作为一个 work unit；同步阶段使用 `executor.run_sync()`。
当前页内按节点类型与 owner 聚合写回，通过 Runtime 绑定的窄回调按实际 owner 签发 admin Scope，保留发起管理员身份。
图鉴权在签发及 Store 执行入口完成，rebuild 不重复检查。
低层 `embed_all()` 可接收 `recovery_session`；未传时仍逐页使用普通 Transaction，支持调用方提供单一 owner 的写 Scope。

文本与 SHA-256 hash 复用 EmbeddingEnricher，每次重建都重新计算所选节点。
空文本清除旧向量及 hash；写回只更新这两个字段，返回实际更新的节点数。
内存仅保留当前页的准备和向量结果。

工作区 rebuild 的模型或数据库失败、取消均由共享恢复会话逆序撤销已提交批次。
同步事务已开始时先等待其结束，再撤销；完成后任务记为 failed。
整轮图成功后清理 Undo 并同步持久回执，随后任务记为 success。
低层未传恢复会话的 `embed_all()` 仍只回滚失败的当前页。分页与普通图写入并发进行，查询可读到新旧向量混合状态；
重建按扫描时读到的文本计算，提交时的文本变化未做版本核对。

## 任务与重试

| 项目 | 行为 |
| --- | --- |
| 任务记录 | `cm_embedding_jobs`，按 tenant/workspace 隔离，保存原发起者、进度、时间与公开模型快照。 |
| 状态 | `pending → running → success/failed`；进度阶段为 scanning、embedding、committing、done；遗留恢复期间为 recovering。 |
| 互斥 | 每个 Workspace 同时受理一个 rebuild；未关闭的遗留恢复（包括其他业务类型）阻止新任务和重试，其他 Workspace 独立。 |
| 心跳 | 任务行保存 `heartbeat_at` 和 `executor_pod`。刷新间隔与过期窗口见 `CM_HEARTBEAT_INTERVAL_SECONDS` / `CM_HEARTBEAT_STALE_AFTER_SECONDS`。进度和终态写回校验 `resume_count` 且状态仍是 pending 或 running。 |
| 查看与发起 | 工作区成员可查看，管理员可发起。 |
| 手动重试 | 当前 Workspace 的管理员可重试任意 failed 任务；沿用 job_id，使用本次请求者的权限和最新模型配置，从头扫描。原始 created_by 保留，重试不查询原发起者权限。 |

Manager 仅执行显式提交的任务。服务关闭时收尾本进程任务，并等待在途事务结束。
实例中断后，通用 RecoveryService 扫描未关闭的栈：未决定成功的栈撤销，已决定成功的栈继续清理。
恢复期间任务保持 running / recovering，关闭后才投影 success 或 failed。后台恢复只执行 Undo，不加载原用户或模型。
尚未登记恢复栈就中断的任务，心跳过期后记为 failed，由用户显式重试。
部署实例需要保持时钟同步，过期心跳不再挡住其他 worker。

恢复轮次 `rid = "embedding:" + job_id + ":" + attempted_at` 复用已有持久字段，无新增列或 Schema 迁移。
查询、worker 收尾和重新受理会消费匹配 tenant/workspace/job_id/rid 的 CLOSED 回执；
同 job_id 重试先消费旧回执，再把 previous_rid 交给 RecoveryService 替换。旧 worker 的进度和终态写回仍要求当时的 `resume_count`。
图提交与 SQL 分别写入。图已成功但 SQL 同步或日志清理失败时保留待恢复状态，不能标成失败或回滚成功图。
旧 JSON 任务历史需在部署切换时单独迁移。

HTTP 契约见 [Embedding API](../api/README.md#embedding)，
模型请求拆批与客户端管理见 [Model](../model/README.md)。

## 验证边界

`tests/cm/embedding/test_jobs.py` 验证共享任务互斥和过期 worker 的执行归属。
`tests/cm/embedding/test_http_recovery.py` 验证待恢复状态阻止新任务，并在 Runtime 重建后续接。
真实向量替换和失败后的重试由 `tests/cm/embedding/test_live_rebuild.py` 验证；
基础 Undo 与恢复中断由 `tests/cm/infrastructure/transactions/recovery/` 负责。
内存 Store、确定性模型和进程内重建不等同于真实进程强杀或数据库验收。

# User

`context_manager.user` 通过 `MemoryIngestService` 提供唯一的 User Graph ingest 入口：把可信 source adapter 筛出的用户消息转换为 canonical `User` / `UserMemory` Draft，完成批内去重、跨批记忆协调和 MG/KG 引用解析，然后在一个 Store transaction 中落图。`ingest.py` 统一装配各算法阶段。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `algo/contract.py` | 定义不暴露 provider 细节的抽取错误。 |
| `algo/extraction.py` | 实现 `process.ChunkExtractor`，维护抽取 prompt 与消息组装，校验消息和 LLM JSON、白名单化证据引用，并直接构造 `NodeDraft`。 |
| `algo/deduplicator.py` | 维护稳定 Memory 业务 ID；实现 `process.Deduplicator`，对整批记忆进行语义分组，选择已有表达并合并证据。 |
| `algo/reconciliation.py` | 跨类型召回已有记忆，由 LLM 判断 create / merge / replace；比较证据时间，按字段合并 Profile，协调冲突和需要停用的记忆。 |
| `algo/resolver.py` | 为整批记忆规划短查询、共享查询 embedding，以全文和向量召回五类 MG/KG 节点，再按候选 key 白名单解析引用。 |
| `loader/` | 从 session history 的终态 chats 加载逐消息增量，维护消息内容版本、抽取版本及同 session 的前文上下文。 |
| `loader/database.py` | 以只读事务分批读取 Host 的消息和数据源类型，关闭每轮连接。 |
| `loader/repository.py` | `ChatLoaderCheckpointRepository` 读写共享 SQL 中已处理的消息版本与抽取版本。 |
| `ingest.py` | 单次导入与算法装配；MemoryIngestPlan 保存算法结果、Profile 增量和引用；Service 在 Store 事务内读取当前事实、编译并执行写入操作，提交后补齐向量。 |
| `adapter.py` | `IngestAdapter` 加载增量历史、重查用户身份、领取用户级执行租约、逐消息抽取并确认成功版本，供定时与手动入口共用。 |
| `repository.py` | `UserIngestRepository` 原子领取、续期和释放共享 SQL 中的用户级执行租约。 |
| `crud.py` | `MemoryCrudService`：读取资料、记忆与本人长期上下文，校验记忆业务规则，将资料编辑、记忆创建/修改/撤销和引用集合替换写入 Store transaction。 |
| `__init__.py` | 暴露 `MemoryIngestService`、`MemoryCrudService`、`IngestAdapter` 和低层算法角色。 |

各算法文件就地维护自己的 prompt、响应 Schema 与处理逻辑；通用 JSON 输出解析复用 `process/output.py`，文本归一化复用 `utils/values.py`，是否忽略大小写由各业务明确选择。

## 摄取流程

```text
IngestAdapter 加载待处理消息 → 重新确认身份与 checkpoint
  → MemoryIngestService.ingest
      → 校验证据 → 抽取 → 批内去重 → 已有记忆协调 → MG/KG 引用解析
      → 一个图事务：重读当前事实、合并证据、编译并执行操作
      → 补齐记忆向量
  → 确认该条消息 checkpoint
```

每个 TextChunk 包含已验证的用户消息 JSON：

```json
{"messages":[{"id":"msg-1","text":"以后修改数据前必须先征得我确认。"}]}
```

```python
service = MemoryIngestService(store, models, executor=executor)
await service.ingest(messages, read_scope, write_scope, occurred_at=cutoff_at)
```

Runtime 注入 Store/模型选择函数和后台 executor。Adapter 负责历史、身份、租约与 checkpoint；
Service 负责摄取业务。每次 ingest 从准备到事实提交及后置向量维护占一个 work unit，
阻塞阶段使用 `run_sync()`，模型异步等待。租约与 checkpoint 控制操作独立执行。
资源规则见 [Runtime](../runtime/README.md#执行池)。

`prepare_plan()` 校验证据并在事务外完成模型决策；`execute_plan()` 在事务内重读当前事实、
复核证据并编译写入。完整 ingest 复用一组客户端至结束；单独准备返回纯数据计划并关闭客户端，
单独执行时按需创建 embedder。空输入生成空增量计划，执行时确保 User 节点存在。

## 记忆与证据规则

- User Graph 的抽取范围是 Profile 与 `principle`、`preference`、`anchored_rule`、`trigger` 四类持久记忆。
- `profile` 在抽取 Draft 中是 Schema 声明的 JSON 字符串。Plan 保留有序 `profile_delta`、证据时间与 source refs，执行时按当前字段时钟合并。Deduplicator 的处理对象是 UserMemory。
- 模型响应中的 `source_ref` 和 `evidence_refs` 必须来自当前 Chunk 的消息 ID。目标消息模式要求 `source_ref` 引用目标，Profile 和 Memory 的证据集合都包含目标；验证后持久化 `source_ref`、`source_refs` 与目标的 `evidence_at`。
- source adapter 负责提供用户消息。算法规范化消息 ID、裁剪文本首尾空白，保留正文换行和字符供模型判断持久含义；取得模型配置前验证输入结构。所有传入的证据时间在输入边界校验并统一为 UTC；逐消息模式要求目标及上下文均有时间，整段快照允许省略时间。
- Profile 保存用户证据支持的字段，在 JSON 内以 `_evidence_at` 记录各字段的证据时间；较新字段值和用户主动清空均受该时钟保护。缺少资料的用户以空 Profile 初始化。
- LLM 抽取的 `user_implicit` 用于至少两条消息支持的 preference / trigger；status 根据解释确定程度选择 active 或 needs_review。
- prompt 规定 trigger 使用非空 `triggers`，并要求 principle / anchored_rule 使用直接用户证据。模型输出使用统一 JSON Schema；证据 ID、目标覆盖和隐式证据条数在响应通过 schema 后单独校验。
- 抽取、批内分组、跨批协调、引用规划和选择共用 `process/output.py` 的 `complete_json`，提供各阶段的 JSON Schema，最多尝试 2 次 JSON 格式/schema 解析；证据校验失败直接终止本条消息。网络重试由模型访问层管理，provider 错误交由对应阶段处理。
- UserMemory Deduplicator 用一次整批 LLM 分组判断同类型记忆的持久含义。合并保留选中的原文、汇总证据及 trigger；模型分组失败时保留全部输入。歧义和冲突保持独立，批内去重本身使用文本判断。
- Reconciler 从本人 HAS 连接的 active / needs_review 记忆召回候选，批量嵌入去重后的描述，跨类型取最近 5 条；相同业务 ID 或重合 trigger 优先召回。LLM 在候选白名单中决定新建、合并或替换，较新证据优先。
- merge 保留已有含义、类型和 key，追加证据与 trigger；replace 沿用 key、替换事实和旧引用。superseded 记忆在同一次事务中转为 revoked，保留节点、历史边并记录此次退休的证据时间。多个输入指向同一记忆时合并证据；同一批保留与停用同一个 ID 会报冲突。
- Resolver 处理 anchored_rule / trigger，包括 needs_review。整批规划每条记忆 1–4 个短查询，共享一次去重查询 embedding；五种目标的全文和向量各取 25 条，按 RRF 合并排名。一个节点保留最佳查询得分；候选摘要保留当前 NodeSpec 的 fulltext_fields 及 domain、datasource_id 上下文。每条记忆最多向 LLM 提供 80 个候选，选择最多 5 个白名单 key。缺失计划回退原描述，缺失选择视为空引用；模型给出的未知 key 被忽略。
- 整段快照与逐消息输入共用失败规则：跨批协调、引用规划、候选 embedding、Store 检索或选择失败均终止本次准备；history loader 保留失败消息的重试资格。
- UG 的 `KeySpec` 声明业务唯一身份：User 使用 `(workspace_id, user_id)`，生成 `usr:<workspace_id>:<user_id>`；UserMemory 使用 `(owner_id, id)`，生成 `mem:<owner_id>:<id>`。新记忆的 `id` 仍由规范化后的 workspace、user、type、description 计算 SHA-256。记忆合并或替换保留既有 id 和 key；旧 `mem:<id>` 在维护窗口通过 `migrate_user_graph` 原地转换，不重算业务 ID 或向量。
- 同一计划、同一 Scope 的独立阶段按 Spec 和 create/patch 聚合，保留节点引用和端点映射。节点先于关系，UG 额外写入保持在两者之间；Store 异常整笔回滚。
- User 与 UserMemory 的业务查找显式限定当前 `owner_id`；即使 read Scope 允许管理员查看其他用户，也只合并当前用户的事实。同 ID Memory 在事务内合并证据，保留既有 created_at；带证据时钟的旧事实受到时间顺序保护。
- 含 Memory 的 ingest 在事实提交后，分页扫描当前用户的未撤销记忆，覆盖四类 Memory 及 needs_review。仅为向量缺失或 `embedding_fields` 文本 SHA-256 hash 已变化的节点复用 `EmbeddingEnricher` 批量生成向量及 hash，内容未变且已有向量的节点跳过。
- 后置 embedding 失败记录警告，已提交的事实保留，该消息继续确认 checkpoint，下次含 Memory 的 ingest 再尝试补齐。模型切换仍通过 `embedding/` 的显式重建入口处理。抽取、协调及引用解析所需的模型失败使源消息保持 pending。
- read Scope 用于跨 User/MG/KG 的候选读取；write Scope 必须限定 `owner=user`、`zone=user`。两者是调用级 capability，不保存在 service 构造状态中。
- `MemoryIngestPlan` 绑定 tenant、Workspace 和 User，执行 Scope 与三者精确一致。`__post_init__()` 集中校验业务身份、每条 Memory 唯一的 HAS 归属和已解析引用；Plan 保存待合并的记忆证据与算法决策，Store 写操作在事务内编译。
- 执行在 `GraphStore.atomic()` 内重新读取当前资料、目标记忆和待停用记忆。较新证据使对应记忆操作跳过；LLM 判断后含义发生变化或待停用目标消失时，整条消息回滚并保留重试资格。merge 基于最新来源集合追加证据，精确修改未命中同样回滚。
- 相同消息重新准备或顺序重放同一个 Plan 时，执行阶段按业务 ID 匹配已有节点和边，复用已有 key。模型抽取、协调和引用选择在事务外完成，事务内只进行证据复核与图读写。
- 并发首次创建由 Store 的 `key` 唯一约束拦截重复节点；冲突事务回滚，源消息保留重试资格，下一次执行按业务身份复用已提交节点。存量随机 key 节点也按业务身份匹配并继续复用原 key。
- chat checkpoint 逐条记录成功处理的消息版本。一条目标消息的图事务成功后确认该消息；同一用户的其他消息继续独立处理，失败消息保持 pending。`messages_processed` / `messages_failed` 分别统计成功确认和失败的消息数量；`users_failed` 统计本轮有失败消息的用户数。
- `created_at` / `updated_at` 记录图事实的处理时间，`evidence_at` 保留 Host 消息的证据时间。图写入失败记录 `user_graph_update_failed`，图已提交但 checkpoint 确认失败记录 `checkpoint_write_failed`，便于区分重试阶段。

## 历史与 checkpoint

loader checkpoint 保存在 `QWENPAW_DATA_DB_URL` 对应数据库的 `cm_update_checkpoints`
表中，与 Host session history 共用数据库。表沿用 `graph_name`、`source_id`、
`source_version`、`updater_version` 和 `processed_at` 字段：`graph_name` 由
`user:` 前缀和 URL 编码后的 tenant、workspace、user 组成；`source_id` 为
`message:<tenant>:<workspace>:<user>:<session>:<chat>`，各段使用 URL 编码。
`source_version` 是当前抽取输入的 canonical JSON SHA-256，包含原始 `{id, user_input}`，
以及存在时的数据源 ID、当前有效类型和成功 ODPS 执行的 `tool_observations`；
`updater_version` 沿用 develop 的 `7`，认可旧抽取成果继续有效。
只有内容版本和抽取版本均匹配才跳过；内容编辑、补录消息和抽取版本升级都可进入下次处理。
表由 `schema/relational.py` 声明，服务启动时由 `CMDatabase` 统一初始化，多个服务实例共享同一份逐消息处理记录。Host history 的必需列也在该声明中维护，由启动入口统一检查；loader 通过只读连接读取消息和工具事件。

loader 接纳 `completed`、`failed`、`canceled`、`cancelled` 且有完成时间的消息，
按照 session/chat 创建时间、完成时间、更新时间和删除时间应用本轮 cutoff。
同 session 的完整前文作为上下文传入，每个目标及其原始完整文本组成一个抽取 Chunk。
`target_message_ids` 标明本次目标，每项抽取结果须包含目标证据；
前文可参与多轮佐证。内容版本仅覆盖目标输入，追加或修改前文时只重新处理发生变化的目标。
消息证据 ID 为 `host-chat:<chat_id>:user`。scope、时间或 sequence 异常按 session 隔离，
无效用户文本按消息跳过；两类结果均返回诊断。`sources` 汇总 checked、succeeded、skipped、
failed，`details` 给出消息或 session 的处理结果。
Loader 同时提供各用户全部合格 session 的证据时间映射，包括已经处理的消息，供旧图数据补齐缺失时钟。

逐消息记录沿用 develop 的身份、输入摘要和版本匹配方式。继续使用同一共享库时，
`cm_update_checkpoints` 无需迁表或改写；版本为 `7` 且输入未变化的历史消息直接跳过。
切换前须保留或迁好对应 UG 图成果及引用；checkpoint 本身不包含记忆内容。
如更换共享库，原样复制消费记录及对应 Host 历史，保留身份和内容；
其他抽取版本仍按不匹配处理，不批量改写历史版本号。已有游标记录保留在原处。
图提交和 checkpoint 确认是先后两次写入，二者之间中断会在下次重放该消息。

## 用户主动管理

`get_global_memories(scope)` 为 Host 提供当前用户的长期上下文：读取有效 User
的 Profile，分页遍历出向 `HAS`，筛选本人 active 的 principle／preference，
按创建时间、业务 id 排序后返回全部结果，以及三类长期上下文的说明。记忆投影包含
id、type、description；HTTP 契约见 [API](../api/README.md#user)。

`MemoryCrudService` 接收 Store，由 Runtime 按调用构造。手工编辑为完整同步方法，
HTTP 一次提交后台池，覆盖准备、事务和结果投影。HTTP 入口在 `api/native/user`，具体路径与响应见
[API](../api/README.md#user)。手工写入直接保存用户明确输入的事实、
操作来源和引用边，模型服务独立于这条编辑链路。后续含 Memory 的 ingest 或显式 embedding 重建会更新手工编辑后的向量。

HTTP 请求模型位于 `api/native/user/requests.py`，负责字段格式与输入限制。
Service 接收属性或 patch mapping；引用目标独立传入 `(label, key)` 序列。
PATCH 的 `references=None` 表示保留原集合，空序列表示清空。
记忆类型、触发词和引用的一致性由 Service 校验；图读写复用 `NodeDraft`、`NodeRecord` 和边类型。

手工写入要求已存在 active User；Memory 读写通过 `HAS` 确认归属。
Profile 的显式 null 删除对应字段，已有字段证据时钟随编辑更新。
手工创建复用抽取的确定性业务 id 规则；Store key 由 Store 生成，编辑保留两者。
同一用户 active / needs_review 记忆中，type 与归一化 description 相同即合并；
按更新时间降序、业务 id 升序选择已有项，保留其表达，追加来源、trigger 与引用。
相同确定性 ID 的 revoked 记忆也可由手工创建重新激活。
PATCH 保留当前审核状态，对显式提交的引用集合做替换；改为 principle / preference 时
清空默认引用，从 trigger 改为其他类型时清空默认触发词。修改后与其他有效记忆重复时返回 conflict。
DELETE 将状态改为 revoked，保留节点和边，将 `updated_at` 与 `evidence_at` 更新为撤销时间，
并追加用户操作来源；该证据时间用于阻止旧消息重放恢复已撤销记忆。
列表默认筛选 active / needs_review，按更新时间降序、业务 id 升序分页；显式 status 可查询 revoked。

手工输入准备和引用读取在事务外完成，事务执行确定的 Store 操作。
读取快照变化或精确修改失败时返回 conflict 并回滚；这不替代跨事务并发控制。

当前认证用户可直接调用 `POST /api/cm/user/ingest`，无需配置开关。
入口使用 Auth 上下文中的
`(tenant_id, active_workspace_id, user_id)` 作为 `target_scope`，处理该用户的 pending
history，与 CronManager 调用同一个 `IngestAdapter.ingest()`，共用用户级认领。
CronManager 内部绑定 `user_ingest` 任务，按共享 SQL 中的计划触发扫描待处理用户；
手动 API 传入认证用户范围，仅处理该用户的历史。

`cm_user_ingest_claim` 按 `(tenant_id, workspace_id, user_id)` 保存心跳和 `run_epoch`，
由 `CMDatabase` 在启动时初始化。每个用户批次原子领取这一行，已被领取的用户返回
`overlap`，其余用户继续处理；成功领取后重新检查每条消息的 checkpoint。
执行期间按 `CM_HEARTBEAT_INTERVAL_SECONDS` 刷新该用户的认领 token，结束时按 token
释放。刷新失败取消当前批次，未确认消息保留重试资格；心跳超过 `CM_HEARTBEAT_STALE_AFTER_SECONDS` 后，下次触发可以重新领取。
正常结束和取消都会停止刷新任务，空闲期间没有后台扫描。

图事务与 checkpoint 分别提交，两者之间中断会重放该消息。
取消协程时已提交给 Neo4j 的操作仍可能完成，认领主要保护执行归属和状态写回。

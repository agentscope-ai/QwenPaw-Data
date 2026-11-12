# Metadata

本模块维护配置发布、物理映射和个人 MG 同步。`weaving` 将已提交的 Semantic Config 发布到
Workspace 权威图，`physical` 读取源库结构，`sync` 将权威图拉取到本人图，`feedback` 接收个人反馈。
Service 在事务外准备操作，通过 Store 和 Transaction 提交；资源由 Runtime 装配。

## 映射与身份

输入快照契约在 `contract.py`，投影在 `weaving/projection.py`，Schema 定义节点身份与属性。
Domain 使用稳定 domain_id，name 仅用于展示；DataSource 使用 owner_id/datasource_id；
VerifiedQuery 使用 owner_id/domain/question_hash，因此修改同一问题的 SQL 原地更新。
Dataset 保留 source/datasource_id，经 AVAILABLE_IN 关联 DataSource；字段、维度绑定和指标口径
按 canonical 关系连接。连接凭据只保存在关系库 datasource 配置，不进入图或 Plan。

Domain、Metric、Dimension、VerifiedQuery 可跨数据源复用；Dataset、字段、口径及物理节点保留 datasource 归属。
准备阶段合并同一身份的投影，已有占位对象保留原事实；改名按 Schema 重建受影响身份和关联，
普通属性修改保持 key。删除绑定或字段时保留其他配置仍引用的对象。

## 配置发布与织入

编辑 API 先提交 SQL，再等待图发布。失败返回 503 并保留已保存配置和失败记录，可通过 FULL 重新发布。
SQL 与图之间不提供跨库事务，提交窗口及跨实例发布顺序不由本模块保证。
Profiling 候选确认仍属 staging，阶段完成、正式表提交后才发布图。

CRUD publish_changes 逐批执行普通 Transaction，失败仅回滚当前批次。
导入及 FULL/INCREMENTAL 使用 RecoveryService，或借用上层同一恢复会话；失败/取消时撤销本轮已提交批次，
成功先持久化图决定再清理日志。恢复不撤销 SQL 配置；协议与能力边界见 [Recovery](../transaction/README.md#recovery)。

- FULL 清理当前数据源的过时语义对象，保留物理节点、其他数据源及仍被引用的共享字典对象；
  INCREMENTAL 更新快照内对象，CRUD 删除即时发布。
- 保持先删边再删节点、节点先于新边的依赖顺序。向量按批次最终属性生成，一轮使用固定模型快照。
- `MetadataWriter` 统一消费准备迭代器，每批准备、模型调用和提交占一个后台 work unit；
  行数使用 [图写入预算](../settings/README.md#图写入批量预算)，请求不能放大部署上限。
- before_commit 检查当前租约；恢复上下文在 executor work unit 外进入和退出，避免重复准入。
- 查询可看到分批提交或补偿的中间结果，分页没有全图快照保证；调用方须避免同范围冲突写入。

Excel 导入提交 FULL 任务；任务与恢复轮次关联，查询只消费 CLOSED 回执，恢复未结束时保留 RUNNING。
重新提交创建新任务；图已成功决定后的取消或 SQL 收尾错误不改变持久结果。
首次成功或降级完成的正向 weave worker 触发物理探查，后台恢复不重放该外部动作。

## Physical

`PhysicalMappingService` 从当前 Workspace 的 datasource 配置读取连接，复用 autoexplore 的范围筛选、
结构指纹、有限采样和人工列优先规则，将 DataSource/Table/Column 及归属边直接写入权威 MG。
数据源尚未入图也可探查；不写编辑器正式表，不生成模型向量。

首次 weave 完成后异步触发；手动 `/api/semantic-config/datasources/{datasource_id}/explore` 强制刷新，
GET/PUT explore-scope 仅管理范围。后端目录和 SDK 读取由 `physical/source.py` 完成，
配置读取、连接开启/关闭、采样及图写入在共享后台执行资源中运行。

默认排除临时表和日期分片，未变结构跳过采样与写图，不覆盖人工列；ODPS/BigQuery 默认不采样。
缓存按用户、数据源、配置和 schema 区分，TTL 见 Settings。
节点先于依赖边提交，每笔受图写入预算约束；失败保留此前批次，重试从当前图补齐。
探查失败独立记录，不改变已完成的 weave 状态，后续 FULL 保留这些物理对象。

## 个人图同步

Service 不保存请求用户，每次接收本人 Scope；管理员同步也只更新自己的个人图。
源为当前 Workspace 的 `__global__` 权威 MG。sync 通过 Store 初始化个人空间，
Holo schema 路由见 [Store](../store/README.md#holo-age)；status 返回个人存储的
`space_status`（`uninitialized` / `initializing` / `ready`），首次同步前 `needs_sync=true`。

快照按去除 owner 后的业务身份匹配并重映射端点：镜像随权威图更新，权威删除撤回镜像及其关联边；
个人新增与权威相同则接纳，不同则形成冲突。global 决策采用权威内容和关联，user 决策保留个人版本，
权威版本不变时不重复询问；权威 digest 变化后拒绝旧决策，冲突节点关联边延后处理。

节点、边和 MGSyncState 在一次 atomic 中提交，无剩余冲突才推进水位；MGSyncState 不承担锁。
同步不调用模型、不复制向量或凭据，个人 DataSource 仍通过 SQL 配置解析连接。
个人追加使用 Schema 生成身份、校验属性和完整边三元组，不能任意指定 key。

sync、冲突决策和个人追加借用共享 SQL engine，按 tenant/workspace/user 锁住读取到图提交的完整过程。
PostgreSQL 使用事务 advisory lock，SQLite 使用写锁；多进程须连接同一协调库，最长等待 60 秒。
这是协调 SQL 库的能力要求，不代表 Holo 图后端提供同一锁机制。

## Settlement feedback

`POST /api/v1/semantic/feedback_card?mode=test|confirm` 接受指标口径、维度定义、列含义和数据集用法。
用户须已同步个人 MG；test 只读并准备，confirm 重读当前状态、生成向量后一次事务写本人图。
筛选、合并与模型判断都在个人范围内，无进程级筛查缓存。

card_content_hash 持久化以识别重复提交，来源信息不参与同步内容冲突判断。
提交前发现参与合并的记录已改变时返回 409 STALE_FEEDBACK。
明确的模型/配置错误可降级并返回告警，失效向量清空后重建；Store 错误与内部契约错误直接失败。

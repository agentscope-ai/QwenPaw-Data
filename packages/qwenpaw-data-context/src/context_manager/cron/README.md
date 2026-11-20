# Cron

`CronManager` 从共享 SQL 加载任务计划，按预定时间触发，领取执行权并调用业务入口，管理心跳与关闭清理。

## 组成

- `ScheduleSpec`：五段 cron 表达式、IANA 时区和单次 `run_at` 时间。星期数字使用 `0/7=Sunday`，也支持 `mon-fri` 等名称。
- `CronJobRepository`：任务计划 CRUD、立即执行请求、原子领取、心跳续期与执行权释放。
- `CronManager`：持有本地定时器，限制本进程并发数，执行已绑定的 handler。内置 `user_ingest` 调用 `IngestAdapter.ingest()`。`CronSettings` 只保存初始时、分和时区，由 Manager 构造 `ScheduleSpec`。

## 持久化与生命周期

`schema/relational.py` 声明 `cm_cron_jobs`，启动时由 `CMDatabase` 创建和校验。
Repository 通过 `get_database()` 使用共享连接池。

启动入口创建 manager，服务生命周期调用 `await start()` 和 `await shutdown()`。
Manager 首次启动写入内置任务；后续启动保留数据库内已经修改的计划与启停状态。
`jobs.create()` 可以添加同一 handler 的多个计划；`update()` 修改计划或暂停，
`run_now()` 请求一次执行，`delete()` 删除后续计划。
启动时加载计划，执行完成后读取该任务的下一次时间并重新设置定时器。
通过 Repository 修改计划后，调用 `await manager.refresh()` 让本实例重新加载；
多实例部署时需要逐实例刷新，或在重启时加载变更。
运行期间的立即执行请求通过 `run_requested` 保留，待当前执行释放后领取；
多次请求合并为一次待执行任务，正常结束与心跳过期清理都会保留该请求。

多个实例通过条件更新竞争同一任务，执行 token 标识本次领取，心跳维持执行权。
失效心跳在加载计划或预定任务触发时清理；周期任务跳过已错过的时间点，继续下一个周期。
其他实例仍持有执行权时，本实例等待下一个计划时间；租约过期后的清理时机由下次触发或显式刷新决定。
单次任务领取后即消耗其时间点，执行失败或中断后可通过 `run_now()` 再次触发。
业务异常记录日志并释放执行权。进程内存保存可由 SQL 重建的定时器和正在运行的协程。

手动 ingest API 直接调用同一个 adapter，与自动入口共用用户级 SQL 租约；
领取后重查业务 checkpoint。执行权与图事务独立，业务需要允许中断和重试。

## 配置

`get_settings().cron` 提供配置：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CM_SCHEDULER_ENABLED` | `1` | 本实例是否运行 cron worker |
| `CM_SCHEDULER_HOUR` | `23` | 内置任务首次创建时的小时 |
| `CM_SCHEDULER_MINUTE` | `0` | 内置任务首次创建时的分钟 |
| `CM_SCHEDULER_TIMEZONE` | `Asia/Shanghai` | 初始计划时区 |
| `CM_CRON_MAX_THREADS` | `8` | 本进程并发任务上限 |
| `CM_CRON_RUNNING_TTL_SECONDS` | `90` | 执行心跳失效期限秒数 |

心跳间隔为失效期限的三分之一。部署时可以启用多个 worker，由共享表协调领取。

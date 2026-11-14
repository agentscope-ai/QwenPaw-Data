"""语义探查工作台（data-profiling）子系统。

在 semantic_config 实体层之上叠加「有状态的探查流程编排」：状态机 + 异步探查任务 +
staging 草稿表 + 完成后 promote 到正式实体。作为独立包交付，与 semantic_config 共用
同一 SQLite 库；权限模型完全沿用语义配置的多租户权限（读工作空间可见、写管理员、
数据源为归属真源、异步任务身份提交时捕获）。
"""

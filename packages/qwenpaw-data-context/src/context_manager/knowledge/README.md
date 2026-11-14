# Knowledge

本模块负责文档导入、知识合并和来源撤回。`importer.py` 编排文件、SQL 记录和 algo 算法，
`deletion.py` 负责撤回，`query.py` 提供目录与原件读取；资源由 Runtime 注入。
文档和 Chunk 不进入图，所有模型调用及文件操作在图事务外完成。

## 导入与重导

```text
保存原件和 processing 记录 → 解析/分块 → 抽取 Draft → 去重/候选解析
  → 合并属性与来源 → 生成向量 → TransactionPlan → 图事务 → ready/degraded
```

支持 PDF/DOCX/TXT/Markdown，上传与解析预算见 [Settings](../settings/README.md)。
`import_document()` 等待完整流程；HTTP 的 start_import/start_reingest 保存原件和状态后返回，
Service 持有后台任务至结束。准备与执行占一个后台 work unit，阻塞步骤借用其线程额度。

`prepare_plan()` 在事务外完成模型和业务决策；`execute_plan()` 校验 Plan 归属后原子写图。
异常或取消记录 failed 并保留原件；图、SQL 和文件分别提交，图提交后的状态写入失败不会撤销图。
重新导入读取原件并校验哈希，沿用文档身份，重新准备计划，不直接重放旧 create Plan。

## 抽取与合并

- 外部模型 JSON 在转为 canonical Draft 时完成结构、KG 规则和 Schema 校验；请求期信息放 transient。
  Entity.type 优先复用 Glossary，新类型须满足词典要求，Store 据此维护 Core 成员关系。
- Entity/Event key 来自 owner 与业务身份哈希。日期 `YYYY-MM-DD` 保持日期粒度并参与事件身份，
  时间戳必须带时区；混合精度的日期区间按日比较，不为纯日期补零点或默认时区。
- 精确身份命中已有节点，近似身份在同类型、当前用户可读的 KG 中召回，再由模型判断
  match/new/ambiguous；歧义候选跳过并给出 warning。
- aliases/source_ids 去重追加，其他节点与边属性只补空值，保留已有名称、类型、事件时间和已确认关系。
  根据最终属性生成或复用向量，仅提交真实差量。
- Plan 保证批内身份及拓扑一致；节点先于关系，新节点以 ref 连接后续操作。
  图事务只执行准备好的 Store 操作，任一失败整笔回滚。

一次准备固定模型快照。抽取和身份判断使用 Knowledge 配置的单次请求超时，embedding 使用基础模型超时；
完整导入可能包含多次请求。模型客户端与执行资源见 [Model](../model/README.md) 和 [Runtime](../runtime/README.md)。

## 文档与并发

原件位于 `QWENPAW_DATA_HOME/{tenant}/{workspace}/{user}/kg-docs/{filename}`，SQL 记录位于 cm_kg_documents。
目录、状态、原件下载和删除均限定本人，管理员不扩大文档访问范围。
同用户 filename 唯一；新文档 ID 组合内容和文件名哈希，同内容不同名是独立文档，重导沿用原 ID。
同名上传返回 409。迁移须同时保留文档 ID、原件路径及图中 source_ids。

导入、重导和删除通过共享 SQL 的 cm_kg_user_claim 领取用户 graph 资源；租约覆盖准备、图提交和文件清理。
同用户并发操作返回 409，其他用户独立。执行期间刷新心跳，状态写回核验 token 与有效期，结束时释放租约。
取消等待已开始的同步操作完成；租约失效后，在途图或文件操作仍可能完成。

读取状态时，过期 processing 显示 failed 并允许重导；deleting 保留删除意图并允许重试。
恢复由用户操作触发，没有空闲文档扫描。心跳与过期预算由 Settings 管理。

## 来源撤回

删除依次执行：标记 deleting → 图事务 → 删除原件 → 删除记录。
从本人 Entity/Event 的 source_ids 撤回该文档；仍有来源的节点保留，无来源节点及其关联边按计划删除，
跨 MG/UG 的另一端节点不删除。两个保留节点间的关系保持原样。
图内顺序为 patch_nodes → delete_edges → delete_nodes，一次事务提交。

图或文件清理失败保留 deleting 和错误，可再次 DELETE；有效租约内不接受并发删除。
当前证据归属为节点级，属性与边的逐文档撤回不在此契约内。
HTTP 入口见 [API](../api/README.md#knowledge)。

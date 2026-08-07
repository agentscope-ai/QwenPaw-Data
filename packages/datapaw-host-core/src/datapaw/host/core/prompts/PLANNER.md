# 任务图管理规则（PLANNER）

以下规则是硬约束，**所有创建、修改、归档 TaskGraph 的操作都必须遵守**。

## 1. 新需求到来时

当 `current_plan` 仍在 `in_progress` 且用户提出新需求时：

1. 判断新需求是否与当前图相关：
   - **相关**（如"灵敏度改成 2.0"、"再算一下 D7 留存"）→ 调用
     `revise_current_plan(changes=[...])`，不要 `create_plan`；
   - **不相关**（如"换个方向，分析用户留存"）→ 先
     `finish_plan("abandoned", "用户切换主题")` 再 `create_plan`。
2. `create_plan` 时必须填写 `name`、`description`、`expected_outcome` 和
   `nodes`。每个 node 至少包含 `name`、`description`、`expected_outcome`。

## 2. 节点 deps 设计

- `deps` 必须是**真实的数据依赖**（B 需要 A 的产出才能开始）
- `deps` 只能填写上游节点的 `node_id`，不要填写节点 `name`。如果某节点会被
  其他节点依赖，应显式给它设置稳定、可读的 `node_id`。
- **不要**用 `deps` 强行表达"顺序偏好"。DAG 可以表达多个节点同时
  ready，但当前 MasterAgent 的执行策略是**单节点串行执行**：每次只从
  ready 节点中选择一个执行。
- 如果两个节点都依赖 A，且彼此没有真实数据依赖，不要为了让它们看起来
  串行而互相加 deps
- 叶子节点（无上游）的 `deps = []`

## 3. 产物路径记录

- 在 `update_subtask(..., state="done", files=...)` 的 `files` 参数中记录每个文件产物，字段为
  `name` / `path` / `mime_type`。不要填写文件大小，后端会自动计算。
- `files[*].path` 必须使用真实路径：
  `<graph_id>/<node_id>/<filename>`。
- `<graph_id>` 和 `<node_id>` 必须来自当前 TaskGraph，不要臆造。
- 不要给 `files[*].path` 添加 `artifacts/<session_id>/` 前缀；后端会按当前
  session artifacts 根解析。
- 如果节点没有文件产出，可以省略 `files` 或传空列表。

## 4. 中断恢复的自检清单

当你在续跑时看到 `<system-hint>` 提到某节点处于 `in_progress`
（可能是中断留下的）：

1. 翻阅对话历史，定位该节点对应的工具调用
2. 如果工具已经返回完整结果 → 直接 `update_subtask(..., state="done", ...)` 记录
3. 如果结果不完整/不存在 → 重新执行该节点
4. 如果用户中间要求了调整 → `revise_current_plan(changes=[...])` 再重跑

## 5. 归档纪律

- 图整体完成时必须调 `finish_plan("done", outcome=<完整报告摘要>)`
- `outcome` 应当是 30–200 字的自然语言，概括本次分析的主要结论
  （不是节点清单！）
- 用户主动放弃时调 `finish_plan("abandoned", <原因>)`

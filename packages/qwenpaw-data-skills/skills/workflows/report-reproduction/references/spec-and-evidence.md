# 口径、验收与证据

`sections/{id}/spec.json` 由 Agent 写成。会话里可以比下面多字段；`export-reference` 只保留下次运行要读的部分。

## spec.json

| 字段 | 导出后 | 怎么写 |
| --- | --- | --- |
| `metric_definitions` | `{text, provenance}` | 一条口径配一个来源标签 |
| `required_data` | 原样 | 要探测的表或分区，导出时去重成 `probe_tables` |
| `datasource` | 收成配方里的一个字符串 | 不写就在定时指令里说「沿用当前会话数据源」 |
| `success_criteria` | `[{id, text}]` | 纯字符串会编成 `S1.1` 这种编号。定时 `check-run` 要求每个 id 都有一条同名 `--business-check` |
| `markdown_outline` | 原样 | 小节标题顺序 |
| `run_guards` | `[{id, rule, why, provenance}]` | 最多 5 条。只写换一个期次后会静默算错的规则，例如错用上一月分区 |

`inferred_questions`、`context`、`provenance_basis` 只服务基线提取，不进入参考目录。导出的 spec 打进参考包，由 `install-reference` 解到任务会话的 `report-reproduction-cron/reference/<recipe_hash>/sections/{id}/spec.json`。本期数据在 `report-reproduction-cron/sections/{id}/runs/<期次>/`。`finish-run` 把 HTML 复制到任务会话产物根目录的 `report.html`。

## 证据门禁（R1）

`compare-section` 和 `apply-comparison` 不相信草稿自己的声明，只看落盘文件：

- `queries/` 里至少一个非空、含 `SELECT` 的 `.sql`
- `baseline/data/` 里至少一个非空数据文件
- 数据文件不是 `source.md` 或 `draft.md` 的拷贝
- 草稿没有声明 `【无法复现】`

任一不满足，`evidence_gate.provenance` 为 `原报告`，`passed` 强制为 false。这种章节不能 `--approved-by-user`。补上真实取数、重写草稿后再比对。

定时运行的对应检查是：渲染后的 SQL、本期 `data/`、验收项和「没有残留 `〔数〕`」。

## 四个来源标签（R3）

按这个顺序判定，写上第一个成立的：

1. **重算**：SQL 和数据都在，数字是这次算出来的。
2. **用户确认**：用户在 `ask_user_question` 里明确选定了口径。导出后进入定时指令的「必守规则」，写成不得回退。
3. **推测**：数据和用户都没给，只能根据字段名或上下文推断。必须显式标出。
4. **原报告**：数字或口径直接来自原文，本次没有重算。

不能同时贴两个标签，也不能为了好看把 `重算` 写成 `推测`。本期若偏离冻结口径，在 `check-run` 里另传「口径来源披露」，`details` 写 `推测` 或 `用户确认`。与冻结口径一致时不用传，CLI 按 spec 自动生成这条检查。

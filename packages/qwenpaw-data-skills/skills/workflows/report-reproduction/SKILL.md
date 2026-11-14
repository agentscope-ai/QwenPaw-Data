---
name: report-reproduction
description: 复现已有业务报告的数值逻辑与视觉样式，并将已验证流程固化为可周期执行的任务。
---

# Report Reproduction

本 Skill 是**报告复现任务**的工作流：当会话产物目录已存在
`report-reproduction/report_manifest.json` 时，Host 会注入 `<workflow-detected>`
提示，要求第一动作加载本 Skill、以其 SKILL.md 为权威流程与铁律的唯一事实源，
**无需再走 `data-intent-router`**，并继承上一个非空 Plan。首轮（无论用户上传源
报告还是仅提供 URL）由 default 会话经 `data-intent-router` 按**用户指令**判定 2f，
仅运行 `rr parse` 建出 manifest 并生成 Plan，随后停止；从下一轮起 Host 依据该
manifest 注入上述提示。
**Host 不再注册任何 report 工具**：解析、比对、状态机与产物落盘全部由本 Skill
自带的 `scripts/report_cli.py` 负责。
是否进入报告复现取决于用户指令，而非“会话里是否上传了文件”——default 会话也会为普通
分析上传文档，故不得把“存在报告样式的文件”当作派发信号。

## 命令入口

本流程的全部动作都通过本 Skill 自带的一个脚本完成：

```bash
python {skill-dir}/scripts/report_cli.py \
  --artifact-dir <当前 Session 产物目录> \
  --chat-id <当前 chat_id> \
  <子命令> [参数]
```

| 位置 | 取值 |
| --- | --- |
| `{skill-dir}` | 本 Skill 目录，取技能清单里本条目的 `<dir>` |
| `--artifact-dir` | 环境说明中的“当前 Session 产物目录”；脚本的所有写入都被限制在其中 |
| `--chat-id` | 环境说明中的“当前 chat_id”；只允许字母、数字、下划线、短横线，最长 128 字符 |

- **每条命令都必须带 `--chat-id`**：脚本靠它落 `turns/{chat_id}.json`，并执行“一个
  chat 只推进一个 Section（或一个目标周期）”的约束；换 Section 必须换 chat。
- **stdout 恰好一个 JSON 对象**，日志一律走 stderr。失败时是
  `{"ok": false, "error": "..."}` 且退出码 1：按 `error` 修正后重试，**不得绕过脚本
  手工改产物或手工改 manifest 状态**。
- 下文用 `rr` 指代
  `python {skill-dir}/scripts/report_cli.py --artifact-dir <产物目录> --chat-id <chat_id>`。

| 子命令 | 作用 | 关键产物 |
| --- | --- | --- |
| `parse` | 解析 DOCX/HTML/Markdown/TXT 原报告并切分为稳定 Section | `source/original.*`、`sections/{id}/source.md`、`report_manifest.json` |
| `update-section` | 写入基线或目标周期复现稿并推进状态机 | `baseline/draft.md` 或 `runs/{period}/draft.md` |
| `compare-section` | 先过**证据门禁**（按落盘证据判定数值来源），再与原 Section 严格比对数值与结构，**只算不决**；门禁未过时 `passed` 强制为 `false` 并回写可反思的 `evidence_gate.violations` | `baseline/comparison.json` |
| `apply-comparison` | 落定该 Section 状态，**人工字段的唯一写入方**；证据门禁在此重算，未过时只能补证据重算、**不能人工放行** | `baseline/comparison.json` + manifest 状态 |
| `freeze-recipe` | 冻结参数化 SQL 与 Section 依赖，并顺带生成定时任务描述符 | `recipe.json`、`baseline_frozen_at`、`schedule_descriptor.json` |
| `prepare-run` | 为目标周期建立隔离运行 | `runs/{period}/run_manifest.json` |
| `check-run` | 目标周期质量检查（不做 GT 对照） | `runs/{period}/run_checks.json` |
| `compile-report` | 合并各 Section 为一份 Markdown | `report.md` 或 `runs/{period}/report.md` |
| `schedule-descriptor` | （可选）只按现有 recipe 重算定时任务预填参数，不重新冻结基线；freeze-recipe 已顺带生成 | `schedule_descriptor.json` |

Host 另提供一个**通用**文档比对 CLI（`python -m qwenpaw_data.host.core.agent.tools.compare
--target <目标文档> --reference <参考文档> --output <结果 json>`，见环境说明），它只写
一份中性的比对结果、不认识报告语义。**报告复现的 GT 对照一律走 `rr compare-section`**，
不得改用通用 CLI 再自行拼 `comparison.json`——那样会丢掉串号/陈旧两道防伪与人工放行
留痕。通用 CLI 只用于本流程之外的临时文档比对。

## 证据门禁（数值来源判定）

复现的铁律是**数值必须由本次真实取数重算得到，不得把 `source.md` 的数字直接搬进
`draft.md`**。为此 `rr compare-section` 与 `rr apply-comparison` 在内核的数值/结构
比对之外，先按**落盘证据**判定本节数值的来源（provenance），结论写进
`comparison.json` 的 `evidence_gate`（`{passed, provenance, violations[], checked_at}`），
**不采信 draft 的自述**。

门禁通过（`provenance=重算`）的硬条件——与 `check-run` 对目标周期的两条证据检查同源：

- `sections/{id}/queries/` 下至少一个**含 `SELECT` 的非空 `.sql`**（本次取数的参数化模板）；
- `sections/{id}/baseline/data/` 下至少一个**非空数据文件**（该模板取回的真实结果）。

以下任一命中即判 `provenance=原报告`、`evidence_gate.passed=false`，并**强制
`comparison.passed=false`**：

- 缺上述任一证据（`queries/` 无有效 SQL，或 `baseline/data/` 为空）；
- 证据文件是 `source.md` 或 `draft.md` 的拷贝（按去空白摘要判定内容一致）——用原文
  或复现稿冒充取数结果；
- 证据文件是空文件 / 占位文件；
- `draft.md` 出现 `【无法复现】` 标签（等于自认数值未独立重算）。

**门禁未过时的唯一出路是补证据重算，不能人工放行**：

1. `rr compare-section` 在 stdout 的 `next` 与 `evidence_gate.violations` 里逐条说明
   缺什么、哪里像伪造；Agent 必须**据此反思**——回到 `fetch-data` 真实取数、把参数化
   SQL 落到 `queries/`、把结果落到 `baseline/data/`，重写 `draft.md`（数值来自本次取数，
   不得搬运 `source.md`），再重新 `rr compare-section`；
2. 这种情形下 `rr apply-comparison --approved-by-user` **会被直接拒绝**（`ok:false`）：
   没有真实重算就没有“残差”可供用户接受；
3. **只有门禁通过（真证据、非拷贝）后才进入数值验收**：此时才走“通过 → `approved`”
   或“仍有会改变结果的残差 → `ask_user_question` → 用户 `answered` 接受 →
   `apply-comparison --approved-by-user`”的正常逻辑。

禁止为过门禁而伪造证据（空文件、把 `source.md`/`draft.md` 拷进 `baseline/data/`、写
一个不含 `SELECT` 的 `.sql` 占位）：这类做法会被门禁判为 `原报告` 并拦下，且属于
严重违规。

## 触发与边界

- 必须有原始报告文件或可访问 URL；缺失时用 `ask_user_question` 补充，不得臆造原报告。
- 只询问会改变计算结果的关键歧义；能通过数据、语义层和现有上下文解决的问题应自主完成。
- 先完成数值与逻辑复现，再处理样式，最后才固化定时任务。
- **恢复规则**：若当前 Session 产物目录已存在 `report_manifest.json`，以它为恢复
  事实源，从未完成的 Section 继续，不得无故重建整个任务或重跑已 `approved` 的
  Section（`rr parse` 不加 `--force-reinitialize` 时是幂等的，不会丢弃已有产物）。

## 强制流程

1. 阅读本 Skill 的 references（产物目录与路径以 `references/artifact-contract.md` 为准）。
2. 检查原始报告、目标周期和数据源。缺失关键输入时调用 `ask_user_question`。
3. 运行 `rr parse --file <原报告路径>` 或 `rr parse --url <报告 URL>`（URL 必须是
   公网 http/https，脚本拒绝内网地址与重定向），读取 stdout 的 `sections` 与落盘的
   `report-reproduction/report_manifest.json`；用户给出周期时带上
   `--source-period` / `--target-period`。
4. 在首次解析 chat 中一次性确认会影响复现结果的关键口径，再调用 `PlanCreate`：
   每个 Section 至少对应一个任务，使用稳定的 `section_id`。
   Section 粒度应对齐原报告的一级业务章节，例如“一、整体收入分布...”、
   “二、客户活跃分布...”；不得把 A/B 小节、子表、明细说明继续拆成独立 Section。
   Section 任务按 manifest 顺序设置依赖；若用户给出 `target_period`，Plan 最后
   追加一个完整目标周期测试报告任务，并依赖最后一个基线 Section。
5. 首次解析 chat 到此结束，不得运行 `rr update-section` 或
   `rr compare-section`。最终回复必须包含：
   - 报告解析和 Section 拆解已完成；
   - 关键口径小窗已确认完毕，并逐条标注口径来源（`原报告` / `语义层` /
     `用户确认` / `推测`，见「口径来源披露」）；
   - Plan 已生成，并概述 Section 顺序和最后的目标周期测试；
   - 已准备开始，请用户确认执行第一个 Section。
6. 用户确认后，每个基线 chat 只处理一个可运行 Section：
   - 读取 `report_manifest.json` 并选择下一个 `pending`、`failed` 的 Section；
     当轮输入里若带有针对某个已 `approved` Section 的评论（见下方评论条），
     优先重新打开那个 Section；
   - 在运行命令前，先说明已经明确的 Section 内容范围、指标、拆解维度、时间和
     业务口径，让用户知道本轮将复现什么；口径必须逐条标注来源（`原报告` /
     `语义层` / `用户确认` / `推测`），`推测` 项当场写明依据与影响面；
   - 用户评论只存在于**当轮输入**里：Host 把用户在成果文件上选中的行与评论原文
     渲染成「用户对成果文件的评论：- 文件 `<路径>` 第 N 行（或第 N-M 行）：<评论>」段落
     注入本轮输入；评论**不落盘，manifest 也不记录**。把它当新增 evidence，
     重新打开被点名的那个 Section 修订；不得去找 `comments.jsonl` 之类的评论文件，
     也不得因为 manifest 里没有评论记录就忽略用户评论；
   - 根据 `source.md` 反推本 Section 的 `spec.json`，写入
     `sections/{section_id}/spec.json` 并重新读取核对；
   - 仍存在会改变计算结果的真实歧义时，通过 `ask_user_question` 小窗一次性确认；
   - **取数默认走 `fetch-data`（按需触发，非每 Section 强制）**：当本 Section 需从
     数仓获取新数据（`spec.json` 的 `required_data` 有缺口、或 `baseline/data/` 无对应
     数据）时，加载 `fetch-data` 走完整取数编排——问题结构化 → 语义层口径补全
     （Step 2.1 指标口径 `calibers[]`，逐条取 `table` / `expr` / `window`：
     `expr` 就是语义层的口径表达式原文，退款剔除、清洗过滤等 WHERE 条件本应
     写在里面；若某条 `expr` 只有列名或聚合式、未覆盖原报告写明的过滤条件，
     即为语义层缺口径——按 Step 2.2 的表/列元数据补齐，或标 `推测` 并向用户确认，
     不得自行编造，也不得去找 `formula_evidence` 之类已退役字段（恒空））
     → NL2SQL → 执行；不得跳过 `fetch-data` 直接手写 SQL 从数仓取新数据。数据已存在
     （续跑复用 `baseline/data/`）、目标周期 Run 复跑冻结 recipe、或纯结构/样式复现时
     无需取数，可不加载；
   - **`query-odps` 是写 ODPS SQL 的规则集，不替代 `fetch-data` 的口径补全**：在
     `fetch-data` Step 5 生成 SQL、或确需手写 ODPS SELECT 时加载并遵循（方言/分区/
     性能/字面量取值）；“算什么口径”来自 `fetch-data` Step 2.1 的语义层结果，
     `query-odps` 只解决“SQL 怎么写对”；
   - 按问题加载适用的 `bi-*` 分析 Skill；
   - 将参数化 SQL 保存到 `sections/{section_id}/queries/`，将基线数据证据保存到
     `sections/{section_id}/baseline/data/`；两者是 `rr compare-section` **证据门禁**
     的硬前置（据此判定 `provenance=重算`），不得用空文件、占位文件或 `source.md` /
     `draft.md` 的拷贝充数（见「证据门禁」）；
   - 存在客观无法复现的缺口（数仓缺字段/缺表、语义层缺口径）时，在草稿**顶部**写
     「复现差异与数据缺口说明」块（写法见「草稿排版规范」第 6 条）；可经问答消解的
     数值偏差、可修订消除的差异一律不入块，仍走下面的比对/放行；
   - 生成基线 Markdown，运行
     `rr update-section --section-id <id> --content-file <草稿路径>`（短稿可用
     `--content`），**不得传 `--run-period`**；仍需补信息时加
     `--status needs_input`；
   - 先按本 Skill 的 Section 校验规则自检证据链，再运行
     `rr compare-section --section-id <id>`；它**只写
     `baseline/comparison.json`、不改状态**，并先过**证据门禁**：门禁未过时
     `passed` 强制为 `false`、`evidence_gate.violations` 逐条说明缺什么，Agent 必须
     据此补真实取数证据、重写 `draft.md` 后重跑（见「证据门禁」）；
   - 接着必须运行 `rr apply-comparison --section-id <id>` 才会把状态落定为
     `approved`（passed）或 `failed`；同一轮里可带上 `--notes` / `--criteria`
     写入口径来源与缺口说明（见「口径来源披露」）；**证据门禁未过时不能人工放行**，
     `--approved-by-user` 会被拒绝；
   - 判为未通过时 Section 状态为 `failed`（前端显示“待修订”），先按差距自行修订复现
     （SQL、口径、数据、拆解），能通过修订消除的差异一律不得放行；
   - 修订后仍存在会改变结果、且归因明确的残差（如原报告口径含额外补采数据）时，
     **必须调用 `ask_user_question`** 让用户在“接受该残差并放行 / 继续修订”之间明确
     选择；**禁止自行判定用户已接受**，也不得只在正文解释差异就当通过；
   - 仅当拿到用户 `answered` 且选择接受后，才可放行，且**放行只能由
     `apply-comparison` 落盘**（Agent 不得手工编辑 `comparison.json`）：

     ```bash
     rr apply-comparison --section-id <id> --approved-by-user \
       --reason "<用户接受该残差的口径理由>" \
       --notes "<口径来源逐条 + 数据缺口/替代方式/偏差归因>" \
       --criteria '<逐指标口径判定 JSON 数组，或其文件路径>'
     ```

     脚本会保留差异明细、把 `passed` 与 `passed_with_review` 置为 `true`、把
     `--reason` 写进每条 `numeric_differences[].note`（标注“经用户确认：…”）、把
     `--notes` 写进顶层 `notes`、把 `--criteria` 写进 `criteria_analysis`，并落一条
     `review` 记录后转 `approved`。**`--approved-by-user` 缺 `--reason` 直接失败**；
     用户超时未答或选择继续修订时不得加这两个参数，保持 `failed`；不得只修改文件或
     Plan 状态便宣称通过；**放行之后不得再跑 `rr compare-section`**——输入未变时
     脚本会直接拒绝，以免抹掉放行理由与口径披露；确需重算先用 `rr update-section`
     修订草稿（旧放行记录随草稿过期），再重新走一遍比对与放行；
   - 通过后调用 `TaskStateUpdate` 标记对应任务完成。
   - 本轮完成该 Section 后必须停止，不得继续处理下一个基线 Section；
   - 最终回复必须说明该 Section 已成功复现、`draft.md` 已显示在右侧“复现产物”，
     给出本 Section 的口径来源清单（哪些复用原报告、哪些来自语义层、哪些经用户
     确认、哪些仍是推测）——draft.md 正文不带来源标签，来源以本回复、对照弹窗
     （顶部差异块与口径判定）为准——并请求用户确认执行下一个 Section。
7. 最后一个基线 Section 通过后，不得在本 chat 编译或开始 Run；请求用户确认执行
   Plan 最后的目标周期完整测试任务。
8. 用户确认目标周期完整测试后，在一个新 chat 中：
   - 确认基线已全部通过；
   - 加载 `report-schedule-builder` 并运行 `rr freeze-recipe`，确保参数化 SQL
     已保存到 recipe；
   - 运行 `rr prepare-run --period <period>` 创建隔离 Run（它要求基线已全部
     `approved` 且 recipe 含查询模板；绝不覆盖基线产物）；
   - 在同一个 chat 中按 recipe 的 Section 顺序执行全部冻结 SQL，并把本期数据保存
     到 `sections/{section_id}/runs/{period}/data/`；
   - 对每个 Section 运行
     `rr update-section --section-id <id> --run-period <period> --content-file <本期草稿>`
     生成本期 `draft.md`，再运行
     `rr check-run --section-id <id> --period <period>`（口径来源汇总按
     「口径来源披露」第 5 条用 `--business-check` 传入）；
   - 全部 Run Section 通过后加载 `report-format-extractor`，由该 Skill 直接读取
     原报告并生成完整、安全、可追溯的 `format_spec.json`；
   - 随后加载 `bi-report-generation`，读取 `format_spec.json`、本期各 Section
     `draft.md` 和数据证据，直接生成 `runs/{period}/report.html`；
   - 不得运行 `rr compile-report`，也不得在 HTML 生成后再套通用模板二次渲染；
   - 更新 Plan 最后的测试任务并告知用户 HTML 产物路径。
9. 如果用户未要求目标周期测试，全部基线 Section 通过后，风格提取、调用
   `bi-report-generation` 生成基线 `report.html` 与运行 `rr freeze-recipe` 冻结
   recipe 也必须在独立的新 chat 中完成（`rr freeze-recipe` 与
   `rr schedule-descriptor` 都不得在已绑定 Section 的 chat 中运行）。

## 基线与跨周期

- 首次还原原报告逻辑属于基线复现：运行 `rr compare-section` +
  `rr apply-comparison` 严格比较 GT，目标是重算值与原报告一致。
- 下月/月报等跨周期运行不得运行 `rr compare-section`，也不得产生
  `comparison.json`；应运行 `rr check-run` 生成
  `sections/{section_id}/runs/{period}/run_checks.json`。
- Run 不得覆盖 `baseline/draft.md` 或 `baseline/comparison.json`。
- 基线严格执行“一 chat 一个 Section”；Plan 最后的完整目标周期测试允许一个 chat
  依次执行全部 Run Section 并生成 HTML。
- `rr freeze-recipe` 后基线进入冻结状态（`baseline_frozen_at` 已记录，且已准备的
  周期运行会因基线漂移而拒绝复跑）；确需修订时必须显式加上 `--reopen-baseline`
  运行 `rr update-section`，重新严格比对、重新 `rr apply-comparison` 落定，并再次
  冻结 recipe；已存在的周期运行需 `rr prepare-run --force-reinitialize` 重建。

## 定时任务固化

用户在对话中要求“提炼为定时任务”“每月/每周自动生成同逻辑报告”时：

1. 必须先保证全部基线 Section 已 `approved`，并在未绑定 Section 的新 chat 中运行
   `rr freeze-recipe` 冻结参数化 SQL 与 Section 依赖。freeze-recipe 会**顺带写出**
   `report-reproduction/schedule_descriptor.json`（`report_id` / `title` /
   `recipe_hash` / 推断出的 `period` 周期形态与 `parameters` 参数模板），正常无需
   再单独跑 `rr schedule-descriptor`。
2. 通过 `ask_user_question` 确认执行时间、时区、周报/月报、周期偏移、数据窗口和
   参数绑定；默认周期偏移为 `-1`（上一个完整周期）。
3. （可选）若 descriptor 缺失或需按最新 recipe 重算，运行 `rr schedule-descriptor`：
   它只读现有 `recipe.json` 重新派生 descriptor，不重新冻结基线、不改
   `baseline_frozen_at`，因此已排期报告的 `recipe_hash` 保持不变。
4. **本 Skill 不创建定时任务**：Host 已移除对话内建 job 的能力。必须明确告知用户
   到会话工作区的定时任务入口创建，并说明该入口会读上面这份
   `schedule_descriptor.json` 预填周期参数（`recipe_hash` 用于执行时校验 recipe
   未变）；不得只给文字建议就结束，也不得声称自己已经建好任务。

## Section Spec 内置阶段

每个基线 Section 开始后，Agent 必须先读取该 Section 的 `source.md`，直接生成
`sections/{section_id}/spec.json`。该文件是后续取数、分析、Markdown 草稿和复跑
recipe 的局部契约，由本 workflow 直接完成，无需加载其他 Skill。

`spec.json` 至少包含：

- `report_id`、`section_id`、`source_title`；
- `inferred_questions`：换一个报告周期仍成立的问题，不得把原报告答案当作新周期答案；
- `metric_definitions`：指标口径、时间窗口、过滤条件、维度、实体粒度和单位；每条
  口径必须带 `provenance`（`原报告` / `语义层` / `用户确认` / `推测`），`推测` 项
  另附 `provenance_basis` 写明依据与影响面；
- `required_data`：需要的数据表、字段、语义实体和不可获取的数据缺口；
- `success_criteria`：可验证的数值、结构、证据链和结论标准；
- `markdown_outline`：草稿的三级标题树（`#`/`##`/`###`），逐块对齐 `source.md` 的
  章节、表格与图表占位位置；必须遵循「草稿排版规范（draft.md）」，不得规划 `####`
  及更深标题，也不得规划 GT 没有的「本节结论」「摘要」等块；唯一例外是 draft.md
  **顶部**的「复现差异与数据缺口说明」块（见「草稿排版规范」第 6 条），它不属于
  `source.md` 的章节结构，仅在存在客观无法复现的缺口时才规划；
- `context`：解决本 Section 必需的原文上下文，避免保存整份报告或无关正文。

生成后必须重新读取 `spec.json`，确认时间参数可复跑、字段口径明确、每条口径都有
`provenance`、不可复现项有证据说明，再进入取数。

## 口径来源披露

任何会影响数值的口径（指标定义、时间窗、过滤条件、维度枚举、单位、实体粒度）都
必须标注来源。**对得上的和对不上的都要披露**：与 GT 一致的数字要说明口径从哪来，
不一致的数字更要说明口径从哪来——用户只有看到来源，才能判断差异是口径问题还是
计算问题。

来源标签固定为四种，不得自造、不得省略：

| 标签 | 含义 | 判据 |
| --- | --- | --- |
| `原报告` | 源报告已写明，本轮直接复用 | `source.md` 正文、表格或脚注里有明确表述 |
| `语义层` | 语义层已定义的指标口径 | `fetch-data` Step 2.1 取到 `calibers[]`（`expr` 口径表达式 + `window` + `table`） |
| `用户确认` | 用户明确给出或在小窗里选定 | 对话原文，或 `ask_user_question` 的 `answered` 结果 |
| `推测` | 以上三者都缺，由模型按业务常识或数据分布推断 | 必须同时写出推测依据，否则不得落笔 |

规则：

1. **一条口径有多个来源时，按 `原报告` > `用户确认` > `语义层` > `推测` 取最可信的
   一个作为标签**；来源之间冲突时以 `原报告` 为准，并在披露里写明冲突内容。
2. **`推测` 口径不得伪装成既定事实**：必须写清推测依据（业务常识 / 数据分布 /
   同名指标类比）、影响哪些数字、以及若推测错误数值会往哪个方向变。
3. **会改变计算结果的 `推测` 口径必须先走 `ask_user_question`**，拿到 `answered` 后
   改标为 `用户确认`；用户超时未答或选择继续修订时保留 `推测` 标签，并在差异说明
   中列出，不得因为已经算出数字就当口径成立。
4. **标签出现在四个地方**，缺一不可：
   - `spec.json` 的 `metric_definitions[].provenance`；
   - 运行命令前的过程叙述，口径清单逐条带标签；
   - `comparison.json`：由 `rr apply-comparison --notes` 逐条写「口径来源：…」写进
     顶层 `notes`，`--criteria` 写进 `criteria_analysis`，`--reason` 会同时落进每个
     数值差异行的 `numeric_differences[].note`——这几处会直接显示在对照弹窗底部的
     差异清单里；
   - 本轮最终回复（chat）的口径来源汇总，**这是用户读到来源标签的主渠道**。
   `draft.md` 正文**不带来源标签**：它的口径说明只镜像 GT（见「草稿排版规范」第 3
   条），GT 没披露的部分不在 draft.md 里补，来源只在 chat 回复与 `comparison.json`
   中披露。唯一例外是 draft.md **顶部**的「复现差异与数据缺口说明」块（见「草稿
   排版规范」第 6 条）：它对每个客观缺口标注来源，是面向读者的缺口披露，不属于正文
   口径。
5. **跨周期 Run 不做 GT 对照，口径随冻结 recipe 继承**：
   `sections/{section_id}/runs/{period}/draft.md` 的口径说明与基线 draft 同形状
   （镜像 GT），不带来源标签；若本期为了跑通而临时改过口径（换表、放宽过滤、
   改时间窗），必须在 chat 回复与 Run 检查清单里单独标为 `推测` 或 `用户确认`
   并说明为何偏离基线，不得默认沿用。运行 `rr check-run` 时把口径来源汇总作为
   一条业务检查传入（`name` 固定为 `口径来源披露`），`details` 里逐条列出，
   使其出现在对照弹窗的 Run 检查清单中：

   ```bash
   rr check-run --section-id <id> --period <period> \
     --business-check '{"name":"口径来源披露","passed":true,"details":"[原报告] …\n[推测] …"}'
   ```

chat 回复与 `rr apply-comparison --notes` 的写法示例（`--notes` 按行书写，前端原样
换行显示）：

```text
- [原报告] 新客口径 = 首次支付 / 注册 / 在 W33 内（source.md 2.1 节脚注）
- [语义层] 退款订单剔除 is_refund_order = false（Step 2.1 calibers：
  table=dwd_trd_ord，expr=COUNT(DISTINCT order_id) WHERE is_refund_order = false，
  window=1d）
- [用户确认] 收入按 GAAP 口径、单位万元（本轮小窗第 1 问）
- [推测] 活跃用户按自然周去重 —— 原报告未写去重粒度，依据是同表其他指标均按
  自然周统计；影响「客户活跃分布」全部人数列，若实际应按自然月去重人数会系统性
  偏低，需用小窗向用户确认
```

## 草稿排版规范（draft.md）

`draft.md` 是给用户直接阅读的**复现报告**，不是工作底稿：它的章节顺序、表格、图
和口径说明都镜像 `source.md`，读起来应当和原报告同一形状。必须满足：

1. **层级只用三级**：`#` = Section 标题（与 source.md 一级章节同名）；`##` = 块（对应
   原报告的一个可独立阅读的小节）；`###` = 块内的表/图/说明单元。禁止使用 `####` 及
   更深标题；需要再分层时改用**加粗行**或列表。
2. **不新增 GT 没有的块**：原报告该章节有结论/摘要段，复现稿在对应位置写**复现**
   结论；原报告没有，就不得自造「本节结论」「摘要」等前置块。**正文**（含结论，即
   顶部差异块以外的全部内容）只写复现值与结论，**不得出现 GT 数值、偏差、偏差率或
   差异归因**——机器数值/结构差异仍进 `comparison.json`（对照弹窗据此高亮与判定），
   客观无法复现的缺口只进 draft.md **顶部**的「复现差异与数据缺口说明」块（见第 6 条）。
3. **口径说明镜像 GT**：原报告在某处披露了口径（表下注、脚注、括号说明），复现稿在
   同一位置用同样形式写**本次实际采用的口径**；原报告该部分没披露，复现稿也不写。
   超过 2 句的口径说明拆成 `-` 列表一行一条，禁止单行长段落。**口径的来源标签
   （`原报告` / `语义层` / `用户确认` / `推测`）不进 draft.md**，只出现在 chat 回复
   与 `comparison.json` 的 `notes` 里（见「口径来源披露」）。
4. **图表位置显式标注**：草稿阶段无法内嵌图表时，用一行引用块占位并写明数据来源与
   图形类型，例如：`> 【图】分段次周留存率 · 人数分组柱状图（系列=m1 cohort）——
   数据见上表，最终由 bi-report-generation 渲染`。禁止留空标题。
5. **编号体系统一**：全文只用一套编号（`## 1.` / `### 1.1`），不得混用「块N」与
   「b-1)/c-1a)」两套；同一编号不得在不同口径分组里重复出现。
6. **draft.md 顶部可写、且仅可写一个「复现差异与数据缺口说明」块**：这是 draft.md
   里唯一允许承载差异/缺口叙事的位置，且**只写「因数据仓库缺字段/缺表、语义层缺口
   径而客观无法复现」的项**——可经问答消解的数值偏差、可修订消除的差异一律不得入块
   （那些沿用「强制流程」第 6 步的问答/放行机制，落定在 `comparison.json`）。写法：

   - 位置固定在 draft.md **最顶部**（首个 `#` Section 标题之前），以哨兵标题
     `## 复现差异与数据缺口说明` 起始，到下一个 `##`/`#` 标题前结束；前端据此标题把
     整块切出、渲染为**默认折叠**的卡片，其余正文正常渲染。
   - 可在标题下用一对 `<dl-diff>` / `</dl-diff>` 作为可选边界哨兵（前端切分时忽略、
     不依赖它渲染，也不得为此放开 HTML）；块内一律用 `-` 列表逐条书写。
   - 每条以 `【数仓缺口】` / `【语义缺口】` / `【无法复现】` 标签起始，写清：缺什么
     （哪个字段/表/口径）、为什么客观拿不到、本次用了什么替代或占位（如 `—`）、对哪
     个结论/数字有多大影响、来源标签（`原报告` / `语义层` / `用户确认` / `推测`）。
   - 块内**不得出现 GT 数值、偏差率**，也不得把可问答消解的残差写进来；面向读者叙事
     （说明缺什么、为什么、用了什么替代/来源），不是工作底稿。
   - 无任何客观缺口时**不写该块**（不得留空标题）。该块会被 `rr compile-report` 合并、
     `bi-report-generation` 带进最终 HTML——这正是「客观做不出的项要透明出现在报告里」
     的意图，故准入必须严格。

   机器数值/结构差异仍由 `rr compare-section` 写入 `comparison.json`，缺口/替代/归因的
   人工放行留痕仍由 `rr apply-comparison --notes` / `--reason` 落进 `comparison.json`；
   draft.md 顶部块是**面向读者的缺口披露**，与 `comparison.json` 各司其职、不互相替代。
   draft.md 正文（顶部块以外）出现 GT 数值或偏差即视为污染产物。
7. **表格可读性**：列数 ≤ 7；数字右对齐语义（千分位保留）；缺数据统一用 `—`，
   并在表下口径里解释为什么是 `—`；同一表内单位统一，单位写进列名（如「GAAP（万元）」）。
8. **不得为了排版改数字**：以上全部是排版约束，任何数值、口径、来源都不得因排版
   需要而被改写、四舍五入或省略（受「每个数字均可追溯」约束）。

## Section Validation 内置阶段

每个 Section 草稿生成后，Agent 先自检：

- `draft.md` 中每个数字均可追溯到 SQL、数据文件或确定性计算；
- `draft.md` **正文**（顶部差异块以外）的章节、表格、图与口径说明覆盖 `source.md`
  的业务结构，且**不含 GT 数值、偏差、偏差率、差异归因或来源标签**；
- 客观无法复现的缺口（数仓缺字段/缺表、语义层缺口径）已写进 draft.md **顶部**的
  「复现差异与数据缺口说明」块，逐条带 `【数仓缺口】/【语义缺口】/【无法复现】`
  标签、替代方式与对结论影响；可经问答消解的数值偏差**不在**该块内；
- 机器数值/结构差异与人工放行留痕仍由 `rr compare-section` / `rr apply-comparison`
  写进 `comparison.json`（`--notes` 落顶层 `notes`、`--reason` 落对应
  `numeric_differences[].note`），与顶部块各司其职、不互相替代；
- 每条口径都有来源标签（在 `spec.json` 与 chat 回复中），`推测` 项写明了依据与
  影响面，会改变计算结果的推测项已走过 `ask_user_question`；
- 参数化 SQL 已保存到 `queries/`，基线数据保存到 `baseline/data/`；
- 新周期只读取 `sections/{section_id}/runs/{period}/draft.md` 与本期 `data/`，
  不得回退到基线数据。

基线复现运行 `rr compare-section` 生成 `baseline/comparison.json`（它只算不决），
再运行 `rr apply-comparison` 落定状态并严格对照原报告 GT。未通过时 Section 保持
`failed`，先按差距修订；修订后仍有会改变结果的残差时，**必须调用
`ask_user_question` 并拿到用户 `answered` 明确接受**，才可运行
`rr apply-comparison --approved-by-user --reason … --notes … --criteria …` 保留差异
明细、把 `passed` 与 `passed_with_review` 落为 `true` 并转 `approved`；禁止自行判定
用户已接受、禁止手工编辑 `comparison.json`、也禁止只改 Plan 状态便宣称通过。

跨周期复跑不得运行 `rr compare-section`，只能运行 `rr check-run` 生成
`sections/{section_id}/runs/{period}/run_checks.json`，检查目标周期参数、口径一致性、
数据完整性和结论是否由当前周期数据支持。

## 禁止事项

- 不得在数值验收完成前生成最终 HTML。
- 不得凭模型补写数字。
- 不得把 `source.md` 的数值直接搬进 `draft.md` 冒充复现；复现值必须来自本次真实取数
  重算（`queries/*.sql` + `baseline/data/*`），由**证据门禁**按落盘证据判定，不采信自述。
- 不得为过证据门禁而伪造证据：空文件、不含 `SELECT` 的占位 `.sql`、把 `source.md` /
  `draft.md` 拷进 `baseline/data/` 一律判为 `原报告` 并拦下。
- 不得在 `draft.md` **正文**里写 GT 数值、偏差、差异归因或口径来源标签；这些只进
  `comparison.json` 与 chat 回复。draft.md **顶部**只允许一个「复现差异与数据缺口说明」
  块，且只承载客观无法复现的缺口（见「草稿排版规范」第 6 条）；可问答消解的数值偏差、
  GT 数值与偏差率一律不得入块。
- 不得把 `推测` 口径写成既定事实，也不得省略口径来源标签；与 GT 一致的口径同样
  必须披露来源。
- 不得一次重写整份长报告来替代 Section 更新。
- 不得在同一个 chat 中推进两个不同的基线 Section；目标周期完整测试按第 8 步执行。
- 不得手工创建或编辑 `report_manifest.json`、`comparison.json`、`run_manifest.json`、
  `recipe.json`、`run_checks.json` 或 `turns/*.json`；状态与产物只能由
  `report_cli.py` 写入，否则串号/陈旧两道防伪与人工放行留痕均失效。
- 不得用 Host 的通用比对 CLI（`python -m qwenpaw_data.host.core.agent.tools.compare`）代替
  `rr compare-section` 做 GT 对照，也不得据其输出自行拼 `comparison.json`。
- 不得声称已经创建定时任务；对话内只能产出 `schedule_descriptor.json` 并引导用户
  去会话工作区创建。
- 不得运行 `rr compile-report` 绕过 `report-format-extractor` 和
  `bi-report-generation`。
- 不得加载 `report-section-spec-builder` 或 `report-section-validator`；对应逻辑已
  内置在本 workflow。
- 最终 HTML 只写协议路径：基线 `report.html` 或
  `runs/{period}/report.html`。不得再拷贝到 Session 产物根目录，也不得另存
  `report_{period}.html`。报告复现产物以本 Skill 的 artifact-contract 为准，
  系统提示 `<qwenpaw-data-analysis-environment>` 的通用目录不覆盖该协议。
- 只运行本 Skill 的 `rr` 子命令与被 recipe 引用的分析 Skill，不得把其他工作流的
  流程带入用户报告任务。

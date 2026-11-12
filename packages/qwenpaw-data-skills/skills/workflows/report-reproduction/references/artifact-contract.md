# 报告复现产物协议

所有路径相对于当前 Session artifact 目录：

```text
report-reproduction/
├── source/original.*
├── report_manifest.json
├── evidence/global.json
├── turns/{chat_id}.json
├── sections/{section_id}/
│   ├── source.md
│   ├── spec.json
│   ├── queries/*.sql
│   ├── baseline/
│   │   ├── data/*
│   │   ├── charts/*.json
│   │   ├── draft.md
│   │   └── comparison.json
│   └── runs/{period}/
│       ├── data/*
│       ├── draft.md
│       └── run_checks.json
├── runs/{period}/
│   ├── run_manifest.json
│   ├── report.md
│   ├── sections_*.json
│   └── report.html
├── format_spec.json
├── sections_*.json
├── recipe.json
├── schedule_descriptor.json
├── report.md
└── report.html
```

`rr` 指 SKILL.md「命令入口」里的
`python {skill-dir}/scripts/report_cli.py --artifact-dir <产物目录> --chat-id <chat_id>`。

- `report_manifest.json` 是基线复现的恢复事实源；`turns/{chat_id}.json` 记录该 chat
  绑定的阶段（`planning` / `baseline_section` / `target_run`）。两者与下列所有结构化
  产物**只能由 `report_cli.py` 写入**，Agent 不得手工创建或编辑。
- `runs/{period}/run_manifest.json` 是对应目标周期运行的恢复事实源；它记录
  `baseline_revision`，基线一旦漂移，已准备的运行会被拒绝复跑，需
  `rr prepare-run --force-reinitialize` 重建。
- `source.md` 不可由 Agent 改写。
- `baseline/draft.md` 只能通过不带 `--run-period` 的 `rr update-section` 更新；基线
  已冻结时必须显式加 `--reopen-baseline`。
- `draft.md` **顶部可携带一个「复现差异与数据缺口说明」块**（哨兵标题
  `## 复现差异与数据缺口说明`，可选 `<dl-diff>` 边界），随整段草稿由
  `rr update-section --content-file` 原子落盘，CLI 无需感知也不校验其内容。该块
  **只承载客观无法复现的缺口**（数仓缺字段/缺表、语义层缺口径），逐条带
  `【数仓缺口】/【语义缺口】/【无法复现】` 标签 + 替代方式 + 对结论影响 + 来源；
  可经问答消解的数值偏差、GT 数值与偏差率一律不得入块（写法见 SKILL.md
  「草稿排版规范」第 6 条）。前端据哨兵标题把整块切出、渲染为默认折叠卡片，
  并把 `comparison.json` 的 `target_start`/`target_end` 偏移按切出后的正文重新对齐。
- `sections/{section_id}/queries/*.sql` 与 `sections/{section_id}/baseline/data/*` 是
  **证据门禁的输入**：`rr compare-section` / `rr apply-comparison` 据此判定本节数值的
  `provenance`（`重算` / `原报告`）。要求 `queries/` 下至少一个含 `SELECT` 的非空
  `.sql`、`baseline/data/` 下至少一个非空数据文件；数据文件若是 `source.md` 或
  `draft.md` 的拷贝则判为无效证据。两者只能由 Agent 真实取数时落盘，不得为过门禁而
  伪造（见 SKILL.md「证据门禁」）。
- `baseline/comparison.json` 由 `rr compare-section` 生成，**只能由
  `rr compare-section` 与 `rr apply-comparison` 写入**。仅当修订后仍有会改变结果的
  残差、且**已通过 `ask_user_question` 拿到用户 `answered` 明确接受**时，才可运行
  `rr apply-comparison --approved-by-user --reason … --notes … --criteria …`：脚本在
  保留差异明细的前提下把 `passed` 与 `passed_with_review` 落为 `true`、写入一条
  `review` 记录并把 manifest 转 `approved`（`--approved-by-user` 缺 `--reason` 直接
  失败）；禁止自行判定用户已接受，也禁止仅改文件/Plan 状态便宣称通过。
  **已带 `review` 且未过期的 `comparison.json` 不得再跑 `rr compare-section`**：
  输入未变时脚本直接拒绝，以免抹掉放行理由与口径披露；修订草稿后旧记录随之
  过期，拒绝自动解除。
- `comparison.json` 的 `evidence_gate`（`{passed, provenance, violations[], checked_at}`）
  由上述两个子命令按落盘证据写入：`provenance=原报告`（缺 SQL/数据、证据是原文或
  复现稿拷贝、草稿声明 `【无法复现】`）时 `passed` 被强制为 `false`，且
  `rr apply-comparison --approved-by-user` 会被拒绝——这种情形只能补真实取数证据、
  重写 `draft.md` 后重跑，不能人工放行。
- `comparison.json` 的 `notes`、`criteria_analysis` 与 `numeric_differences[].note` 是
  **机器差异与人工放行留痕**的落点：偏差归因、口径来源、放行理由都写在这里，供
  前端对照弹窗高亮与「口径判定」使用（`notes` 按行换行显示）；面向读者的**客观缺口
  披露**则写在 `draft.md` 顶部的「复现差异与数据缺口说明」块，两者各司其职、不互相
  替代。`draft.md` **正文**（顶部块以外）只镜像原报告形状，不得承载 GT 数值、偏差或
  差异说明。
- `numeric_differences[].target_start` / `target_end` 是 `rr compare-section` 写入的
  `draft.md` 字符区间，前端据此把标红精确落在被比较的那个数字上（与 GT 数值一一
  对应，不靠字符串搜索猜位置）。补充差异说明只能通过 `rr apply-comparison` 的
  `--notes` / `--reason` / `--criteria`，不得改动、重排或删除这两个偏移字段，也不得
  手工增删差异行；一旦 `draft.md` 被修订，必须重新运行 `rr compare-section` 由脚本
  重建偏移，偏移与正文对不上时前端会跳过该处标红。脚本另有两道防伪：`target_path`
  / `reference_path` 必须指向本 Section 的 `baseline/draft.md` 与 `source.md`（串号），
  且 `comparison.json` 的 mtime 不早于这两个文件（陈旧），任一不满足即拒绝落定。
- `sections/{section_id}/runs/{period}/draft.md` 必须通过带 `--run-period` 的
  `rr update-section` 更新。
- `sections/{section_id}/runs/{period}/run_checks.json` 只能由 `rr check-run` 生成，
  不包含 GT 数值比较；口径来源汇总通过 `--business-check`（`name` 固定为
  `口径来源披露`）追加。
- `report.md` 与 `runs/{period}/report.md` 只能由 `rr compile-report` 生成（后者带
  `--run-period`），stdout 给出每个 Section 在合并文档中的偏移索引。
- `recipe.json` 只能由 `rr freeze-recipe` 生成；`schedule_descriptor.json`（`report_id`
  / `title` / `recipe_hash` / 周期 `parameters` 模板）由 `rr freeze-recipe` 冻结时
  **顺带写出**，`rr schedule-descriptor` 是只按现有 recipe 重算它的可选入口，供前端
  创建定时任务时预填，`recipe_hash` 用于执行时校验 recipe 未变。**Host 已移除对话内
  创建 job 的能力**，对话内只能产出这份 descriptor。
- 新周期 Run 不得覆盖基线产物。
- SQL、数据和计算证据必须先落盘，再在 Markdown 中引用。
- `format_spec.json` 由 `report-format-extractor` 读取原报告后直接生成，必须包含
  页面、标题、导航、字体、色板、Section、表格、图表、交互和来源证据。
- `sections_*.json` 和最终 `report.html` 由 `bi-report-generation` 生成；新周期
  只能使用对应 `sections/{section_id}/runs/{period}/draft.md` 和数据，禁止回退到基线。
- 每个周期只保留一份最终 HTML：基线为根目录 `report.html`，目标周期为
  `runs/{period}/report.html`。不得再拷贝到 Session 产物根目录，也不得另存
  `report_{period}.html`。
- 最终 HTML 不得由 `rr compile-report` 或其他通用模板再次渲染。

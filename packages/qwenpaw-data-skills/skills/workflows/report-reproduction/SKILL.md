---
name: report-reproduction
description: 复现已有业务报告的数值逻辑与视觉样式，并将已验证流程固化为可周期执行的任务。
---

# 报告复现

把一份已有报告拆成章节，逐章用真实取数重算并请用户放行，再导出一份只含下次定时运行所需内容的参考包。会话里已有 `report-reproduction/report_manifest.json` 时，Host 会提示先加载本 Skill。

下文用 `rr` 表示：

```bash
python {skill-dir}/scripts/report_cli.py --artifact-dir <产物目录> --chat-id <当前 chat_id>
```

定时运行先 `install-reference`，之后的命令再同时带上 `--reference-dir report-reproduction-cron/reference/<recipe_hash>` 和 `--work-dir report-reproduction-cron`。相对路径相对于产物目录。`--chat-id` 用当前 chat，只含字母、数字、下划线和短横线。

## 阶段地图

| 阶段 | 这一次 chat 做什么 | 命令 | 停下时怎么说 |
| --- | --- | --- | --- |
| 解析与规划 | 只解析、澄清、写 Plan，不写章节 | `rr parse` | 列出章节和待确认口径，等用户确认 |
| 基线章节 | 只推进一个章节到 approved | `update-section` → `compare-section` → `apply-comparison` | 说明本节结论；有残差先问用户 |
| 目标周期测试 | 只跑一个期次的全部章节 | `prepare-run` → `update-section --run-period` → `check-run` | 说明该期次是否可固化 |
| 设为定时任务 | 冻结并导出参考包 | `freeze-recipe` → `export-reference` | 用导出的 `cron_job` 创建任务 |
| 定时运行 | 按参考包跑新期次 | `install-reference` → `plan-run` → `prepare-run` → `check-run` → `finish-run` | `STOP` 时只回复 `message` |
| 模板修订 | 在维护会话里改模板并试跑 | `--reopen-baseline` → 再冻结 → `diff-run` | 把差异交给用户确认后再替换任务 |

随时可跑 `rr status` 查看每节状态、证据来源和放行理由。它不写任何文件。

## 铁律

**R1 数值只能来自本次取数。** 基线必须有含 `SELECT` 的 `queries/*.sql` 和真实的 `baseline/data/*`。拷贝 `source.md` 或草稿、空文件、草稿里写【无法复现】，都过不了证据门禁。门禁未过不能人工放行。

**R2 放行只能由用户决定。** 机器比对通过才可 `apply-comparison`。仍有会改变结论的残差时，先 `ask_user_question`，用户明确接受后才加 `--approved-by-user --reason "<用户原话>"`。

**R3 口径必须标来源。** 每条口径用且只用一个标签：`重算`、`用户确认`、`推测`、`原报告`。有真实取数就写 `重算`，不得降级，也不得把推测写成重算。

**R4 一个 chat 只推进一个章节或一个周期。** 解析、基线章节、冻结、某一个目标周期，各用各的 chat。

**R5 产物只能由 `rr` 写入。** 不要手改 manifest、comparison、run_checks、recipe、STATE.json。参考目录只读。

**R6 草稿正文镜像原报告。** 标题、顺序、表格和句子按原文。定时运行时 `exemplar.md` 里的 `〔数〕` 全部换成本期数值，结构不动。

**R7 客观缺口只写进顶部缺口块。** 数仓缺字段、缺表、缺口径，写在草稿最上方的 `## 复现差异与数据缺口说明`。能问用户解决的数值差不进这个块。

**R8 HTML 只经 `bi-report-generation` 生成。** 输入是 `format_spec.json` 和各章草稿。定时交付前要有 `runs/<期次>/report.html`，然后 `finish-run`。

## 命令速查

| 命令 | 阶段 | 写入 | 常见失败 |
| --- | --- | --- | --- |
| `parse --file/--url` | 规划 | 源文件、manifest、章节原文 | 缺源、路径越出工作区 |
| `update-section --section-id` | 基线或周期 | 草稿 | 基线已冻结却没加 `--reopen-baseline` |
| `compare-section` | 基线 | `comparison.json`，不改状态 | 【R1】证据门禁未过 |
| `apply-comparison` | 基线 | 状态、放行记录 | 【R2】未给用户理由却要求放行 |
| `freeze-recipe` | 固化 | `recipe.json`、`schedule_descriptor.json` | 有章节未 approved，或没有 SQL |
| `prepare-run --period` | 周期 | `runs/<期次>/` | 基线变了；定时模式下校验和不符 |
| `check-run --period` | 周期 | `run_checks.json` | 缺数据、验收项未覆盖、草稿仍有 `〔数〕` |
| `status` | 任何 | 不写 | 任务还没 parse |
| `export-reference --out --note` | 固化 | `reference/`、`seed/`、`rr-bundle-*.zip`、`cron_message.md` | `source_css` 为空，本节有人在改，或包超过 20MB |
| `install-reference --recipe-hash --work-dir` | 定时 | 工作目录里的参考和种子 | 上传目录没有这个 hash，或压缩包路径越界 |
| `plan-run --run-date` | 定时 | 不写 | `STOP`：已交付、缺口、状态超前 |
| `finish-run --period` | 定时 | 报告副本、`archive/`、`STATE.json` | 有章节未通过，或没有 HTML |
| `diff-run --period --base-work-dir --candidate-work-dir` | 修订 | 候选目录的 `diff.json` | 目录不在工作区内 |

`check-run` 的每条 `--business-check` 都是 `{"name","passed","details"}`。定时模式下 `name` 用验收项编号。口径与冻结 spec 一致时不必再传「口径来源披露」，CLI 会按 `metric_definitions[].provenance` 自动检查；只有本期偏离时才另传一条，`details` 标明 `推测` 或 `用户确认`。

## 设为定时任务

在单独的 chat 中：

1. `rr freeze-recipe`。它会扫 `queries/*.sql` 和 `compute/*.py`，并写出前端要读的 `schedule_descriptor.json`。
2. 确认每节 `spec.json` 的 `success_criteria` 是 `[{id, text}]`（纯字符串会在导出时编号），`run_guards` 最多 5 条，只写换期次后会静默失败的规则。
3. `rr export-reference --note "<这一版改了什么>" --baseline-period <基线期次>`。`--out` 可省略，默认写到产物目录的 `report-reproduction-export/`，且必须留在产物目录内。
4. `CronJob create`。`message` 用 `cron_message.md` 的全文，`session_policy` 用 `reuse`，`attachment_filenames` 用返回的 `bundle_path`，`channel` 用 `console`。调度与配方周期一致：月报用每月运行日，周报用每周运行日。不要用 `new_each_run`，否则每期都会丢掉 `STATE.json`。

参考包里没有原报告、没有基线数字、没有验收过程。种子只有 `STATE.json` 和上一期的 `data/`。任务跑起来之后，不要在该任务的会话里同时聊天：会话正忙时，这一次触发会被跳过。

## 定时运行

`install-reference` 把参考包解到 `report-reproduction-cron/reference/<recipe_hash>/`，并在还没有 `STATE.json` 时写入种子。参考目录只读，`report-reproduction-cron/` 可写。期次以 `plan-run` 为准。`STOP` 就用它的中文 `message` 结束，不要补跑。`exemplar.md` 只提供结构和措辞。每个验收项都要有一条 `--business-check`。HTML 生成后最后才 `finish-run`；没成功就还没交付。`finish-run` 会把报告复制到本会话产物根目录的 `report.html`。步骤正文在 `cron_message.md` 里，不在这里展开。

## 模板修订

只在创建该任务的维护会话里改模板。`update-section --reopen-baseline` 解开基线，改完再 `freeze-recipe` 和 `export-reference`。用新参考包试跑一个期次，再 `rr diff-run` 比较任务会话的 `report-reproduction-cron/` 和试跑目录。把 `diff.json` 交给用户。用户确认后才 `CronJob replace`：`message` 用新的 `cron_message.md`，`attachment_filenames` 带上新的 `bundle_path`。新 hash 会得到新文件名，旧包留在任务上但不会被选中，因为运行按 `recipe_hash` 取包。`replace` 不改已经绑定的会话。

## 参考文档

- `references/artifacts.md`：维护会话、参考包、任务工作目录，以及谁可以写。
- `references/lifecycle.md`：章节和期次怎么迁移，一个 chat 管多大范围。
- `references/spec-and-evidence.md`：`spec.json`、证据门禁、四个口径标签。
- `references/drafting.md`：草稿怎么排、缺口块怎么写、口径怎么披露。

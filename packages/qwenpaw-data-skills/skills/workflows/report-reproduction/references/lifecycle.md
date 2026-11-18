# 状态与 chat 范围

状态只由 `rr` 迁移。Plan 里的进度不能代替 manifest。

## 基线章节

```text
pending → running → validating → approved
             ↓           ↓
        needs_input ←─────┘
```

任意执行态都可以到 `failed`，`failed` 可以再回 `running`。

| 状态 | 含义 | 谁把它送进去 |
| --- | --- | --- |
| `pending` | 还没开始，依赖可能未完成 | `parse` |
| `running` | 正在取数或改稿 | `update-section` 的第一步 |
| `needs_input` | 有会改变结果、必须问用户的决定 | `update-section --status needs_input` |
| `validating` | 草稿已落盘，等比对 | `update-section` |
| `approved` | 证据门禁是 `重算`，且比对通过或用户已放行 | 只有 `apply-comparison` |
| `failed` | 比对未过，或工具失败 | `apply-comparison` |

`compare-section` 只写 `baseline/comparison.json`，不改状态。已有未过期的用户放行记录时，它会拒绝重算，避免把理由冲掉；草稿或原文变了，记录过期，拒绝解除。

基线冻结之后，再改草稿必须 `update-section --reopen-baseline`。这会清掉 `baseline_frozen_at`，已准备的周期运行随之失效，要 `prepare-run --force-reinitialize`。

## 一个 chat 的范围

`turns/{chat_id}.json` 记下这个 chat 的阶段。

- 解析 chat 只能 `parse`，不能写章节。
- 基线 chat 只能推进它 claim 的那一个 `section_id`。
- 冻结、导出用没有 claim 章节的另一个 chat。
- 目标周期 chat 在 `prepare-run --period` 之后可以推进该期次的每一节，但不能再改基线，也不能换期次。

## 目标周期

```text
pending → running → checking → approved
             ↓          ↓
        needs_input ←────┘
```

`update-section --run-period` 写运行态草稿。`check-run` 做数据、SQL、占位符和验收项检查，通过则 `approved`。这一路不和原报告比数字。

定时模式下还要过：验收项编号都出现在 `--business-check` 里、草稿没有残留 `〔数〕`、渲染后的 SQL 还在、口径来源已披露。全部 `approved` 且 `runs/<期次>/report.html` 存在后，`finish-run` 才把该期次写入 `STATE.json`。

`plan-run` 不改状态。它只根据 `recipe.schedule` 和 `STATE.json` 回答本期能不能跑：

| `status` | `reason` | 含义 |
| --- | --- | --- |
| `OK` | 空 | 本期紧挨着上一已交付期次 |
| `STOP` | `already_delivered` | 这一期已经交付 |
| `STOP` | `gap` | 中间缺了期次，先补再跑 |
| `STOP` | `state_ahead` | 状态比运行日算出的期次更晚 |

## 模板修订

维护会话里 `--reopen-baseline` 回到基线章节流程。重新冻结并导出后，试跑目录和任务会话的 `report-reproduction-cron/` 用 `diff-run` 比较。用户确认之前不要 `CronJob replace`。

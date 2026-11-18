# 产物与归属

`rr` 是 SKILL.md 里的 `report_cli.py`。下表的「Agent 可改」只指 Agent 用自己的写文件工具；凡是标了 `rr` 的，都不要手改。

## 会话产物

路径相对于当前 Session 的 artifact 目录。前端读的就是这一棵树。

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
│   ├── compute/*.py
│   └── baseline/
│       ├── data/*
│       ├── charts/*.json
│       ├── draft.md
│       └── comparison.json
├── runs/{period}/
│   ├── run_manifest.json
│   └── report.html
├── sections/{section_id}/runs/{period}/
│   ├── data/*
│   ├── queries/*.sql
│   ├── draft.md
│   └── run_checks.json
├── format_spec.json
├── recipe.json
└── schedule_descriptor.json
```

`comparison.json` 与草稿顶部可以有哨兵标题 `## 复现差异与数据缺口说明`。前端靠这个标题把缺口块折叠，并按切掉之后的正文重算差异偏移。

`export-reference` 在同一产物目录下另写 `report-reproduction-export/`（可用 `--out` 改名，但必须仍在产物目录内）：

```text
report-reproduction-export/
├── reference/          下面的只读参考
├── seed/               STATE.json 和上一期 data/
├── rr-bundle-<hash 前 12 位>.zip
└── cron_message.md     CronJob 的 message
```

zip 里只有 `reference/` 和 `seed/` 两个顶层目录，不超过 20MB。`CronJob create` 的 `attachment_filenames` 指向这个 zip。

## 参考包（任务附件，解出后只读）

定时任务保存这份 zip。每次触发都会把它拷进任务会话的 `uploads/<session_id>/`；同名内容有变化时，文件名会变成 `cron-<摘要>-<序号>.zip`。`install-reference` 不看文件名，只按 `REFERENCE.json` 的 `recipe_hash` 选择，并解到：

```text
report-reproduction-cron/reference/<recipe_hash>/
├── REFERENCE.json          校验和、recipe_hash、contract_version、中文 prompt
├── recipe.json             章节顺序、查询、计算脚本、schedule、数据源、探测表
├── format_spec.json        source_css、dom_contract、视觉字段；没有原报告路径
└── sections/{id}/
    ├── spec.json           口径、验收项、大纲、run_guards
    ├── queries/*.sql
    ├── compute/*.py
    └── exemplar.md         结构保留，业务数字换成〔数〕
```

不包含原报告、`source.md`、基线草稿、comparison、证据、轮次、HTML 和已接受的残差。那些只解释基线那一期，下一次运行不读。目录已存在且校验通过时，`install-reference` 不再解包。

## 任务会话的工作目录（跨期次可写）

`session_policy=reuse` 让任务一直用同一个会话，所以 `report-reproduction-cron/` 能跨期保留。种子只在还没有 `STATE.json` 时从参考包写入：`STATE.json`，以及 `sections/{id}/runs/{基线期次}/data/*`。之后只有 CLI 改 `STATE.json`。

```text
report-reproduction-cron/
├── reference/<recipe_hash>/   上一节，只读
├── STATE.json
├── turns/{chat_id}.json
├── runs/{period}/params.json
├── runs/{period}/report.html
├── archive/{period}/report.html
└── sections/{id}/runs/{period}/
    ├── queries/*.sql     prepare-run 渲染后的 SQL
    ├── data/*            本期取数
    ├── draft.md
    └── run_checks.json
```

`finish-run` 再把 HTML 复制到任务会话产物根目录的 `report.html`。

## 谁写、谁读

| 文件 | 谁写入 | Agent 可改 | 谁读取 |
| --- | --- | --- | --- |
| `report_manifest.json`、`turns/` | `rr` | 否 | 会话恢复、`status` |
| `source.md`、`source/original.*` | `parse` | 否 | 基线比对 |
| `spec.json`、`queries/*.sql`、`compute/*.py`、`baseline/data/*` | Agent 落盘真实取数和口径 | 是 | 证据门禁、冻结、导出 |
| `baseline/draft.md` | `update-section` | 否 | 比对、导出 exemplar |
| `comparison.json` | `compare-section`、`apply-comparison` | 否 | 前端、`status` |
| `recipe.json`、`schedule_descriptor.json` | `freeze-recipe` | 否 | 前端创建任务、导出 |
| `format_spec.json` | `report-format-extractor` | 否 | `bi-report-generation`、导出 |
| `report-reproduction-export/` | `export-reference` | 否 | 维护会话创建定时任务 |
| `report-reproduction-cron/reference/` | `install-reference` | 否 | 定时运行 |
| `STATE.json` | `install-reference`、`finish-run` | 否 | `plan-run` |
| `report-reproduction-cron/` 的 `runs/`、`archive/` | `prepare-run`、`update-section`、`check-run`、`finish-run` | 数据文件可以 | 下一期对比、交付 |
| `diff.json` | `diff-run` | 否 | 模板修订时给用户看 |

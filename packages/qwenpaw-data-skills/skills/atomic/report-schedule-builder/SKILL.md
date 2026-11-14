---
name: report-schedule-builder
description: 将已验证报告的 SQL、参数和 Section 依赖固化为周期复跑配置。
---

# Report Schedule Builder

本 Skill 依附于 `report-reproduction` 工作流：下文 `rr` 指其 SKILL.md「命令入口」里的
`python {skill-dir}/scripts/report_cli.py --artifact-dir <产物目录> --chat-id <chat_id>`，
`{skill-dir}` 取 `report-reproduction` 的目录。

1. 确认所有可复用 SQL 已保存到各 Section 的 `queries/`。
2. 时间范围必须改成显式占位符，例如 `${start_date}`、`${end_date}` 或 `${month}`。
3. 运行 `rr freeze-recipe` 生成 `recipe.json`；它默认拒绝不含任何查询模板的 recipe
   （确需空 recipe 才加 `--allow-empty-queries`），记录 `baseline_frozen_at`，并
   **顺带写出** `report-reproduction/schedule_descriptor.json`（定时任务预填参数）。
4. 检查每个参数、查询路径、Section 顺序和最终输出路径。
5. 用户要求“提炼为定时任务”时：上一步 `rr freeze-recipe` 已顺带写出
   `report-reproduction/schedule_descriptor.json`，保存源 Session 的 `report_id`、
   recipe 哈希与推断出的周期参数模板，正常无需再单独跑命令。参数模板可引用
   `${period}`、`${period_start}`、`${period_end}`、`${window_start}`，并可在冒号后
   使用 `strftime` 格式，例如 `${period:%Y%m}`。若 descriptor 缺失或要按最新 recipe
   重算，可选运行 `rr schedule-descriptor`（只重算、不重新冻结、不改 `baseline_frozen_at`）。
   **Host 已移除对话内创建 job 的能力**：不得声称任务已创建，必须引导用户到会话
   工作区的定时任务入口，由它读这份 descriptor 预填周期粒度、偏移、窗口大小与参数绑定。
6. 每次触发由 Host 创建新 Session、复制冻结的模板（含 `report_manifest.json`），并据此
   自动派发报告复现子代理；子代理先运行 `rr prepare-run --period <period>` 创建周期隔离
   目录。不得修改源 Session，也不得要求 Agent 每次重新自由生成 SQL。Host 侧会用
   descriptor 里的 `recipe_hash` 校验 recipe 未被改动。
7. 周期任务完成取数和草稿生成后，运行
   `rr check-run --section-id <id> --period <period>`，而不是
   使用基线 GT 比对（`rr compare-section` / `rr apply-comparison`）。

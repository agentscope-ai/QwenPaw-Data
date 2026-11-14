<qwenpaw-data-analysis-environment>
当前 QwenPawData 分析环境：{workspace_mode}。
- 当前 session_id：`{session_id}`。
- workspace 根目录：`{workspace_dir}`。
- 当前 Session 产物目录：`{artifact_dir}`。
- 当前没有持久 IPython 内核；需要运行 Python 时，用 `Write` 保存脚本后再用 `Bash` 执行。
- MCP 工具若返回 `file_path`，按该路径读取完整数据。`rows` 只是预览。
- 用户要求创建、导出或生成的最终文件必须保存在当前 Session 产物目录中，并在最终回复里按链接格式给出路径。
- 未获用户明确指示，禁止读取或改写其他 Session 的文件。临时文件可写在 workspace 其他目录。

所有需落盘的分析产物一律写入当前 Session 产物目录，绝对路径以上面给出的真实路径为准。结构：

```
<Session 产物目录>/
├── plan.yaml                 # 原始 plan（来自 planner，不修改）
├── plan_v1.yaml              # 第一次修改后的 plan（如有）
├── steps/                    # 步骤结果（每步完成后写入）
├── data/
│   ├── raw/                  # 从数据源拉取的原始结果
│   └── processed/            # 衍生指标、清洗结果等
└── result.yaml               # 最终结果
```

- `plan.yaml` 始终保留不修改；调整计划时按顺序写 `plan_v1.yaml`、`plan_v2.yaml`…，执行以最新版为准。
- 原始数据放 `data/raw/<数据描述>.<ext>`；计算结果放 `data/processed/<分析内容>_<结果描述>.<ext>`。文件名自描述；列名须有业务含义，不用 `col1`。每个关键计算结果都要落盘，确保可溯源。
- 每步完成后写 `steps/step_01_<步骤描述>.yaml`，含：做了什么、产出文件路径、关键代码（如有）、本步结论。
- 执行完成后写 `result.yaml`：完成状态、核心结论、支撑数据、未解决问题、后续建议。结论中区分数据事实、分析解读、不确定性标注。
- 报告、看板、最终数据包（CSV/Excel/zip）禁止覆盖已有文件。首次 `<交付物名>_v1.<ext>`；再改先 glob `<交付物名>_v*.<ext>`，按最高版本新写：结构/口径/结论变了则大版本（v5→v6），文案/排版/同数据局部修正则小版本（v5→v5.1）。禁止对已有交付物路径覆写。`steps/`、`data/` 中间文件与 `result.yaml` 除外。
</qwenpaw-data-analysis-environment>

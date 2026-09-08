<qwenpaw-data-analysis-environment>
当前 QwenPaw Data 分析环境：
- 可用工具：`Bash`（执行命令）、`Read`/`Write`/`Edit`/`Glob`/`Grep`（文件操作）。
- 当前没有持久 IPython 内核；需要跑 Python 时，用 `Write` 落 `.py` 脚本后用 `Bash` 执行。
- 模型可见 workspace 绝对路径：`{workspace_dir}`。
- 当前 session_id：`{session_id}`。
- 当前 session 产物绝对目录：`{artifact_dir}`。
- 技能只读根目录：`{workspace_dir}/skills/default`；生成报告时必须读取
  `{workspace_dir}/skills/default/bi-report-generation/SKILL.md`，不要在 session 产物目录下查找技能。
- MCP 工具若返回 `file_path`、`download_url`、`rows` 等字段，按工具返回语义处理；
  不要假设固定工具名。
- Matplotlib/Seaborn 绘图时，请先探测当前 Python 环境可用字体。
- 没有活动 TaskGraph 节点时，产物直接写入 `{artifact_dir}`，不要臆造
  `graph_id` 或 `node_id`。
- 有活动 TaskGraph 节点时，节点产物写入
  `{artifact_dir}/<graph_id>/<node_id>/...`。
- 仅在记录节点 `update_subtask(..., 'done', files=...)` 时，FileRef.path 使用
  相对当前 session 产物根的 `<graph_id>/<node_id>/...`，不要带 workspace、
  `artifacts` 或 session_id 前缀。
</qwenpaw-data-analysis-environment>

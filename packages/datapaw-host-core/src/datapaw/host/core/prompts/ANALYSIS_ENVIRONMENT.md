<datapaw-analysis-environment>
当前 DataPaw 分析环境：local workspace（宿主侧执行）。
- 可用工具：`Bash`（执行命令）、`Read`/`Write`/`Edit`/`Glob`/`Grep`（文件操作）。
- 当前没有持久 IPython 内核；需要跑 Python 时，用 `Write` 落 `.py` 脚本后用 `Bash` 执行。
- 文件路径由 DataPaw runtime 解析；记录产物时使用相对当前 session artifacts 根的路径。
- MCP 工具若返回 `file_path`、`download_url`、`rows` 等字段，按工具返回语义处理；
  不要假设固定工具名。
- Matplotlib/Seaborn 绘图时，请先探测当前 Python 环境可用字体。
- 当前 session_id：`{session_id}`。
- 当前 session 产物根目录为 `artifacts/{session_id}/`（相对 workspace 根）。
- 没有活动 TaskGraph 节点时，产物直接写入当前 session 目录，不要臆造
  `graph_id` 或 `node_id`。
- 有活动 TaskGraph 节点时，节点产物写入
  `artifacts/{session_id}/<graph_id>/<node_id>/...`。
- 仅在记录节点 `update_subtask(..., 'done', files=...)` 时，path 使用
  `<graph_id>/<node_id>/...`，不要带 `artifacts/{session_id}/` 前缀。
</datapaw-analysis-environment>

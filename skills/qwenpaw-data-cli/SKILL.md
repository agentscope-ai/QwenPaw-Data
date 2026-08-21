---
name: qwenpaw-data-cli
description: 指导外部自动化 agent 使用 QwenPaw Data CLI 发现 Data Bridge 数据源、通过自然语言规划与执行数据分析任务、复用 SOP YAML，并根据流式输出、执行摘要和错误信息验收结果。用于需要调用本地 `qwenpaw-data` 命令完成数据任务、选择 `run` 或 `plan`/`execute`、传入 `--datasource-id`、处理长任务输出或排查 CLI 失败的场景。
---

# QwenPaw Data CLI

将 `qwenpaw-data` 作为外部任务执行入口。若当前 agent 已经运行在 QwenPaw Data 任务内部，不要递归启动另一个 `qwenpaw-data` 进程。

## 执行前检查

1. 运行 `qwenpaw-data --help`，确认命令存在且公开子命令为 `plan`、`execute`、`run`、`chat` 和 `datasource`。若命令不可用，报告需要先运行项目的 `scripts/init_local.sh`；不要搜索或直接调用 `.venv/bin/qwenpaw-data`，也不要自行安装或改写项目环境。
2. CLI 会自动加载项目根目录 `.env` 或 `QWENPAW_DATA_ENV_FILE` 指定的文件。不要读取、打印或手动 `source` dotenv 文件。模型命令会优先使用 `QWENPAW_DATA_MODEL_*`，未配置时回退到 `LLM_MODEL`、`OPENAI_API_KEY` 和 `OPENAI_BASE_URL`。
3. 使用 `QWENPAW_DATA_CM_BASE_URL` 指定 Data Bridge 地址；未配置时默认使用 `http://127.0.0.1:8765`。
4. 需要访问数据源时，先运行：

   ```bash
   qwenpaw-data datasource list
   ```

   从返回 JSON 的 `items` 中选择精确的 `datasource_id`。优先使用用户明确指定的 ID；名称或类型只有一个明确匹配时才自动选择；存在多个合理候选时向用户确认。不要从已掩码的凭据推断数据源。
5. 假定模型凭据和 MCP 配置已由运行环境提供。不要写入凭据或自行创建、覆盖 MCP 配置。

## 选择命令

### 直接完成普通任务

对不需要预先审阅或复用计划的一次性任务使用 `run`：

```bash
qwenpaw-data run --datasource-id "sales-prod" "分析最近 30 天销售额趋势"
```

需要传递较长、包含多行或容易被 shell 错误解释的请求时，将请求保存到文件并使用 `--file`：

```bash
qwenpaw-data run --file request.md --datasource-id "sales-prod"
```

位置 prompt 与 `--file` 互斥，不要同时传递。

### 审阅或复用计划

当用户要求先看计划、任务需要人工审阅，或 SOP 需要重复执行时，先生成 YAML：

```bash
qwenpaw-data plan --file request.md --datasource-id "sales-prod" --output plan.yaml
```

确认命令成功、`plan.yaml` 存在且内容符合任务目标后再执行：

```bash
qwenpaw-data execute plan.yaml --datasource-id "sales-prod"
```

在 `plan` 和 `execute` 中重复传入同一个 `--datasource-id`；该参数属于每次 CLI 请求的上下文，不要假定它已写入 SOP。

### 交互式对话

只在有人值守且终端支持标准输入时使用：

```bash
qwenpaw-data chat --datasource-id "sales-prod"
```

不要在无人值守的自动化流程中使用 `chat`，因为它会持续等待输入，直至收到 `exit`、`quit` 或 EOF。

## 选择输出模式

`run` 和 `execute` 默认启用流式输出。保留默认模式，持续读取文本增量、工具调用和工具结果，直至进程退出：

```bash
qwenpaw-data run --file request.md --datasource-id "sales-prod"
qwenpaw-data execute plan.yaml --datasource-id "sales-prod"
```

不要仅因短时间没有新输出就判定任务失败；使用支持长超时或会话轮询的命令执行工具，并确认进程是否仍在运行。

仅在同时满足以下条件时使用 `--no-stream`：

- 需要干净的最终回复以及 `Execution summary`；
- 调用方允许任务期间没有 stdout；
- 调用方提供足够长的超时或能够轮询进程状态。

```bash
qwenpaw-data run --no-stream --file request.md --datasource-id "sales-prod"
qwenpaw-data execute plan.yaml --no-stream --datasource-id "sales-prod"
```

`plan` 不支持 stream 选项。运行 `plan` 时预留长超时并轮询进程；不要添加 `--stream` 或 `--no-stream`。

当前流式路径在结束后不会追加 `Execution summary`。若任务必须严格检查节点状态和产物列表，应在执行前选择 `--no-stream`，不要为获取汇总而重复执行已经完成的任务。

## 验收结果

始终等待 CLI 进程结束并检查退出码：

- `0`：CLI 正常结束；仍需检查任务内容是否完整。
- `1`：运行时异常；读取 stderr 并按错误类型排查。
- `2`：命令或参数无效；使用对应子命令的 `--help` 修正调用。
- `130`：任务被中断；不要当作成功交付。

流式模式下，检查最终回复、工具结果和任何显式失败信息。由于该模式没有 execution summary，不能仅凭退出码 `0` 声称所有 DAG 节点成功；需要严格节点级验收时应预先选择非流式模式。

非流式模式下，额外检查 `Execution summary`：

1. 确认存在 `graph_id`。
2. 确认 `completed: true`。
3. 逐一检查节点状态。只有全部节点为 `done` 才视为完整成功；任何 `failed` 或 `abandoned` 都应标记为部分失败，即使 `completed: true`。
4. 检查 `artifacts` 中的路径，并在可访问时确认文件存在且可读。引用 CLI 返回的实际路径，不要虚构产物。

向用户交付时，说明使用的数据源、完成状态、失败或跳过的节点、核心结论和产物路径。

## 排查失败

- **参数错误**：运行 `qwenpaw-data <subcommand> --help`，根据公开参数修正；不要猜测参数名。
- **模型未配置**：报告错误中列出的缺少变量名称，不要读取 dotenv 文件，也不要输出、记录或构造 API Key。
- **Data Bridge 不可用**：检查 `QWENPAW_DATA_CM_BASE_URL` 指向的服务是否可达，再重试 `qwenpaw-data datasource list`。
- **数据源不存在或不明确**：重新读取 `datasource list`，使用精确 ID；多个候选无法消歧时请求用户选择。
- **MCP 不可用**：说明运行环境需要预配置 MCP。公开 CLI 不提供 MCP 管理命令，不要调用 `qwenpaw-data mcp` 或自行覆盖配置文件。
- **节点执行失败**：保留已有回复和产物，说明失败节点及影响。修正明确的暂时性问题后再重试，不要无条件重复整个任务。

## 禁止事项

- 不要调用不存在的 `qwenpaw-data serve`、`qwenpaw-data mcp` 或 `qwenpaw-data data-source`。
- 不要绕过公开命令去搜索或调用 `.venv/bin/qwenpaw-data`。
- 不要向 `datasource list` 传递不存在的 `--base-url`；通过 `QWENPAW_DATA_CM_BASE_URL` 配置地址。
- 不要打印模型密钥、数据库密码、AccessKey 或 STS Token。
- 不要为了获得 execution summary 重复执行一个已经通过流式模式完成的有副作用任务。

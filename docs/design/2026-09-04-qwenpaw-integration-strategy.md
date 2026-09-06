# 决策记录:QwenPaw-Data × QwenPaw 集成战略定位

- 状态:Reviewed(对齐基线)
- 性质:**战略决策记录(ADR)**,非执行 plan;不含代码改动
- 关联仓库:`QwenPaw-Data`(本体,本文所在)与 `QwenPaw`(主应用,`plugins/apps/qwenpaw-data` 插件)
- 一句话结论:**不做原生重写,走周边接线;QwenPaw 唯一结构性护城河是多 app 生态,其余能力 qwenpaw-data 都能自力更生。**

## 1. 背景与要决策的问题

QwenPaw 主应用通过 `plugins/apps/qwenpaw-data`(PawApp,type=app)集成 QwenPaw-Data。
随着 qwenpaw-data 的分析能力经 QwenPaw 频道交付(频道桥接),一个战略问题
被反复提出:

> **如何把 qwenpaw-data 植入 QwenPaw 生态,才能让 QwenPaw 不沦为"鸡肋"(glorified iframe + 频道壳)?**

候选路径有二:① 把整个 qwenpaw-data **重写成 PawApp 原生**;② 保留 qwenpaw-data 主权,
只在**周边**用 PawApp SDK 接线。本文记录选择 ② 的依据,并明确护城河到底在哪。

### 非目标(Out of Scope)

- 不规划具体 epic 任务拆分(另有执行 plan)。
- 不重新讨论 data-console 前端归属——其编译产物已作为 reviewed vendor snapshot 跟踪在 QwenPaw PawApp 中,源码仍由 Cloud 维护;本文只说明它**不是护城河**。
- 不复述频道桥接的实现细节(作为既成架构现状,见 §2.4);本文聚焦战略定位。

## 2. 已核实的事实基础

以下为决策所依据的架构事实。

### 2.1 两个对等的 AgentScope 运行时

- **QwenPaw host agent**:AgentScope 运行时,内建 research 工具(`web_search`/`web_fetch`/
  `browser`/`file_io`/`shell`/`send_file`/`view_media`/`delegate_external_agent`/
  `agent_management`)、跨会话记忆检索(`recall_history(op="search")`)、tool 总线、频道、多 app。
- **qwenpaw-data engine agent**:同为 AgentScope 运行时(`agent/toolkit.py` 用 `agentscope.tool.Toolkit`),
  具备 MCP 工具总线、`spawn_subagent`、plan mode(plan 态门控 `execute_sql`)、middleware、
  DAG 编排(`orchestration/`:task_graph/dag_store/artifact/state/events)。
- 结论:engine **不是"哑分析后端"**,而是一个完整的专科 agent 运行时,与 host **对等**。

### 2.2 engine 是 MCP 可扩展的(能力差非架构鸿沟)

- engine 工具从**可编辑的 `.mcp` 配置**加载(`paths.mcp_config_file`),支持多 client、多类型
  (http_mcp/stdio),`add_mcp` 可注册任意 MCP server。
- `is_cm_mcp_config` 是**特判而非白名单**:仅对 URL 含 `/mcp/v1/cm` 的 http_mcp 返回 True,
  用于给 context-manager MCP 加长超时(2400s)、注入鉴权头、重写 Docker URL;非 CM client 走通用路径。
- 结论:给 `.mcp` 挂一个 web-search/browser MCP,**engine 自己就能联网 research,不需要 QwenPaw**。
  qwenpaw-data 默认不 bundle research 是**定位选择**(事实锚定/可信 grounding,拒绝未锚定的开放 web),非能力缺失。

### 2.3 体量:原生化要吸收的规模

| 子系统(host-core) | LOC | 性质 |
|---|---|---|
| `algo` | 9,740 | 分析算法——核心 IP |
| `orchestration` | 3,091 | DAG task-graph + artifact + state + 自恢复 |
| `api` | 3,048 | engine 自有 HTTP/SSE 协议 |
| `store` | 3,005 | 持久化 |
| `agent` | 1,986 | engine agent + subagent + toolkit + middleware |
| `runtime` | 1,594 | ChatRuntime / 任务跟踪 |
| **host-core 合计** | **27,494** | 专科运行时 |
| context 服务 | 64,652 | 图谱编排 + 语义配置 |

整个 qwenpaw-data Python ≈ **92K+ LOC**。

### 2.4 周边接线的现状

- **频道交付**:分析结果经 QwenPaw host 的 ChannelManager 交付——`DataBridgeMiddleware` 在频道会话中
  整轮接管(跳过 host agent LLM),经 `bridge/EngineClient` 驱动 engine,`events.translate_frames` 把 engine
  SSE 帧翻译成 AgentScope 事件,由任意 QwenPaw 频道原生渲染;`/data`、`/datasource` 命令门控;`ctx.notify()` 实接 ChannelManager。
  host-core 已移除内建 IM channel 与频道管理路由,QwenPaw host 因而成为明确的频道交付边界;PawApp gateway
  继续拒绝遗留频道管理路径,vendored console 亦无 Channel Configuration 页。standalone OSS 保留 CLI、HTTP/SSE、
  artifact 与 scheduling 能力,但不再自带 IM delivery。
- **跨 app tool 面**:插件仅注册 4 个**只读** context tool(`search_context`/`list_domains`/
  `explore_entity`/`execute_sql`);**无灌入类 tool**;`ContextGateway` 仅 JSON/proxy,**无 multipart upload**。
- **灌入目标存在**:context `doc_api` `POST /api/v1/docs/upload`(txt/docx/pdf/md → KG ingest,
  状态 building/ready/failed)+ 四层图谱灌入(physical/semantic/knowledge/trace)。
- **身份短板**:`ctx.user` 是 stub,`user_id="default"`。
- **前端**:data-console(chat/分析主面)源码仍由内部 Cloud 维护,但经过审核的编译 snapshot 已跟踪在 QwenPaw
  `qwenpaw-data` PawApp 中,常规构建、CI 与用户安装均不依赖 Cloud;context-console 可从 OSS 源码重建。
- **生态规模**:当前仅 **3 个 PawApp**(agent-kanban、qwenpaw-creator、qwenpaw-data)。
- **原生前端已证伪**:完整原生 chat 前端曾被实现,后整体放弃、退回 embed 经审核的 vendor snapshot。

## 3. 决策

### D1 — 周边接线,不做原生重写;engine 保持主权

host-core engine 作为**专科主权运行时**(managed sidecar)保留,IP 一行不动;
PawApp SDK 只在**周边**把它接进 QwenPaw:频道(bridge/middleware)、生态(tool 总线)、
身份(ctx)、UX(embed 或轻量原生壳)。频道交付即此模式的首个实例(engine 未改,仅在周边接管)。

### D2 — 唯一结构性护城河 = 多 app 生态(tool 总线 / 节点身份)

在"两个对等 AgentScope 运行时"前提下,QwenPaw 对 qwenpaw-data 的不可替代价值**不能建立在能力差上**
(research、agent 运行时,qwenpaw-data 都能经 MCP 或自有子系统自力更生;频道则已明确由 host 负责交付)。
engine 作为"单 app 域内运行时"无法复制的,是**多 app 生态里的节点身份**(被别的 app 组合、引用别的 app 产物)
+ **跨 app 共享层**(统一外壳、host 级身份/治理/频道)。

### D3 — 正向拉力,非锁定

目标是让 QwenPaw 成为跑 qwenpaw-data 的**最优路径**(用户主动选),而非**唯一路径**(强制锁定)。
OSS 独立版(CLI+context+host-core+skills,`start_local.py` 可跑)保留为**入门 on-ramp**,
反向为 QwenPaw 导流。两仓同属一方、无外部竞争者,"无法独立存在"作为目标既无意义又损害 OSS 价值。

## 4. 被否决的方案

- **R1 全 PawApp 原生重写** — 三难,每条都输:
  (a) 在通用 host agent 上重表达 27.5K LOC 专科逻辑,丢失 grounding/隔离/长任务自恢复保证;
  (b) 砍 engine 只留 tool,丢 IP 且 standalone OSS 直接死;
  (c) 只原生化前端——**已被实践证伪**(完整实现后整体放弃;连最浅一层都如此,原生运行时难一个数量级)。
- **R2 在前端上竞争/复刻** — data-console 源码是 Cloud 资产、commodity;其 reviewed 编译产物已由 PawApp vendor,但前端 Δ=0,仍不是护城河。
- **R3 lock-in 框架** — "让 qwenpaw-data 无法独立"在无竞争者场景下不是护城河,反而切断 OSS on-ramp。
- **R4 把 research 互补当护城河** — 已证伪:engine 可自挂 MCP 做 research;"research→喂 KG"是**用户价值工作流**,非结构性护城河。

## 5. 推论与后续

1. **epic 重心钉死在 tool 总线 / 多 app 组合**;频道、身份、UX 均为配角。
2. **复用 bridge/ 原语**:面向 engine 的新 tool 复用 `bridge/EngineClient`(`download_artifact`/
   `stream_events`/`create_session`/`create_chat`)与 `events.translate_frames`(终态 = `object=="response"`
   且 status∈{completed,failed,cancelled}),不另建 engine 传输/消费层。
3. **tool vs middleware 边界**:两条路径按 session 命名空间隔离——middleware 以 `session_id.startswith("pawapp:")`
   守卫,跳过 app 域会话(留给正常 agent + tool 总线),仅接管频道会话(有 `request.channel` 且 data mode active)。
   即「跨 app 组合走 **tool**、同 host 频道接管走 **middleware**」;两者共享 EngineClient/events,不重复驱动 engine。
4. **身份是 present 可硬化的 Δ**:`ctx.user`/`user_id` 做实,是**今天就能加厚、且不依赖生态 populated** 的护城河补漏,优先级应高于跨 app 总线。
5. **research→KG 工作流**:落地需 `ingest_document` tool + `ContextGateway` 加 multipart upload + ingest 状态轮询 + **策展/格式转换**(web HTML→md/txt,避免污染 grounding)。它是 D2 生态价值的**首个具体用例**,但本身非护城河。
6. **前端**:embed 经审核的 vendor snapshot,不复刻;源码维护仍是 Cloud 资产议题,常规构建与安装不依赖 Cloud。

## 6. 未决问题

- **Q1**:engine 的 `.mcp` 能否被用户/插件**自由扩展**(有无 allowlist / 签名校验拦截外部 MCP)?若能,则"生态节点身份是 engine 唯一守不住的边界"彻底坐实,D2 闭环。
- **Q2**:生态 populated 路径——第 2/3 个**值得组合**的 app 是谁(agent-kanban?qwenpaw-creator?设想的 research app?),tool 总线的现货价值取决于此。
- **Q3**:host 侧是否存在真实 user 态可供 `ctx.user` 接驳(决定 D2 身份补漏能做到 app 级还是 user 级 provenance)。
- **Q4**:若未来某 tool 从 app 会话内驱动 engine,与频道会话的 engine session 交叉时,归属/去重如何约定?(当前两路径 session key 不同,暂无冲突;属前瞻性约定。)

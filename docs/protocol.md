# 协议层

QwenPaw-Data Console ↔ Host 的 HTTP / SSE 协议，以及 Host 内部的 Agent /
Runtime 契约。

| 项目 | 内容                                                                                   |
| --- |--------------------------------------------------------------------------------------|
| 适用范围 | 鉴权与用户、Preferences（Provider / ActiveModels / RuntimeSettings）、Skills（Agent skills / 版本历史 / 会话草稿）、CronJob、Session、Chat、Plan、BizEvent、Segment、Trace、Artifact、Steer、Clarification、Feedback、Fork、Review、MCP 连通、我的用量 |
| 读者 | 前端、Host、Agent                                                                        |
| `schema_version` | 35                                                                                   |
| 全局前缀 | `/api`                                                                               |

### 变更记录

仅保留最近 10 次；新条目置顶。

| 版本 | 日期         | 改动 |
|----|------------| --- |
| 35 | 2026-10-28 | RuntimeSettings 增加 `tool_confirmation_level`（`off` / `auto` / `all`，缺省 `auto`）。用户偏好；下一轮 Chat 生效。`auto` 按 Host 名单 `auto_ask_names` 原样匹配工具名。待确认调用的 `plugin_call` Message `metadata` 带 `awaiting_confirmation` / `expires_at` / `reason`。见 §4.3 / §6 RuntimeSettings / Message |
| 34 | 2026-10-27 | `GET /sessions/{session_id}/artifacts`：全 Session 产物索引，同时返回 Agent 视角 `artifacts` 与业务视角 `segment_artifacts`。见 §4.5 |
| 33 | 2026-10-27 | `Chat.enable_clarification` 为 `false` 时，Host 在等待处对 `ask_user_question` 自动作答，不再依赖客户端提交。见 §4.3 |
| 32 | 2026-10-27 | Session / Chat 增加 `enable_clarification`。Session 为开关；开轮复制到 Chat。`false` 时客户端对反问自动作答。见 §4.3 |
| 31 | 2026-09-18 | 配置中心「我的用量」：`GET /usage/summary`、`/timeseries`、`/sessions`、`/background-tasks`。只读，身份取当前登录用户；按 Session 与后台任务两类聚合生成 / Embedding Token |
| 30 | 2026-09-17 | `GET /sessions/{id}/snapshot` 按 `Chat.sequence` 窗口读取：默认最近 15 轮（`limit` `1..100`），`before_sequence` 取更早页，`has_more` 表示还有更早轮次。`session.chat_count` 仍为全 Session 轮次数 |
| 29 | 2026-09-17 | Chat 运行失败用 `ChatError`（`Chat.error` 与 `response.failed.error` 同形）。`code` 区分模型鉴权 / 额度 / 限流 / 模型不存在 / 上下文超长 / 服务不可用 / 配置缺失 / 内部错误。`message` 为面向用户的说明，`details.cause` 为原始异常 |
| 28 | 2026-09-16 | SSE 增加 `mcp.connect`：开聊探测每个已启用 MCP 各推一条。`context-manager` 失败后本轮以 `response.failed` 结束 |
| 27 | 2026-09-15 | stop 回执可为 `running`，终态以 SSE `response.cancelled` 为准。澄清不再返回 `CLARIFICATION_ALREADY_RESOLVED`。运行中重连 `/events` 可续 `content`；终态后回放只剩完整 `message` |
| 26 | 2026-09-15 | Preferences RuntimeSettings 增加 `tool_timeout_enabled` 与 `tool_timeout_seconds`（秒，最小 1）。下一轮 Chat 生效 |

---

## 1. 对象关系一览

```
User 1 ──── Preferences（Provider 凭证 / 模型扩展 / ActiveModels / RuntimeSettings）
       ├── n Agent skills（workspace/skills + skill_revisions）
       ├── n CronJob（定时任务定义；到点新开 Session/Chat）
       └── n Session（fork / review 可通过 parent_session_id 指向源 Session）
                 ├── n SkillDraft（定稿前只落表；确定后写该 Session 所属 Agent 的 skills）
                 └── n Chat
                       ├── 0..1 Plan ──── n Task
                       ├── n Message（原始轨迹；含 Steer 注入的 user Message）
                       ├── n BizEvent（业务轨迹）
                       ├── n Segment（cover 连续 BizEvent，可关联 Task）
                       ├── n Feedback / 0..1 FollowUp
                       └── n Artifact
```
---

## 2. 鉴权端点(待定)

请求携带 `Authorization: Bearer <token>`（或 Host 配置的等价 Cookie）。未登录 → `401`。Session 无权限或不存在 → `404`；其他非本人资源 → `403`。

#### `POST /auth/login`

用户名密码登录，返回访问令牌。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| username | str | |
| password | str | |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| access_token | str | Bearer token |
| user | User | |

#### `GET /auth/me`

返回当前登录用户。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| user | User | |

#### `POST /auth/logout`

注销当前令牌。

---

## 3. 公共约定

### 3.0 通道职责

| 通道 | 职责 |
| --- | --- |
| REST（JSON） | 用户发起的读写：Session、开聊、停跑、确认提交、用户改计划、Steer（成败回执）、反馈、Skills（Agent 安装/启停/版本、会话草稿确认进 Agent）、CronJob（定时任务）、snapshot、fork、review、轨迹查询、成果 access、我的用量等。以 HTTP 响应为回执，客户端用响应更新本地状态。Steer 成功为 `200` + `{"ok": true}`、失败为 §3.1 错误体，注入正文不通过本通道回传 |
| SSE | 推送 Agent 运行增量（含 Steer 注入后进入轨迹的 `message`）；线格式、对象目录与流程见 §5。唯一入口：`GET .../chats/{chat_id}/events` |

### 3.1 错误体

HTTP 与流内 `object`=`error` 的形状：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| code | str | `UNAUTHORIZED` / `FORBIDDEN` / `NOT_FOUND` / `CONFLICT` / `VALIDATION` |
| message | str | 面向开发者的说明 |
| details | object \| null | 可选补充信息 |

流内 `object`=`error` 时，上表字段与 `sequence_number` / `session_id` / `chat_id` 同层（见 §5.1）。

Chat 运行失败不走本表，见 §6 `ChatError`。挂在 `Chat.error` 与 SSE `response`（`status`=`failed`）的 `error` 上。
---

## 4. HTTP 端点

前缀：`/api`。仅 `GET .../chats/{chat_id}/events` 响应 `Content-Type: text/event-stream`（`data` 为 §5 流式对象）；其余为 JSON。

### 4.1 Session

分享由 `share_scope` 决定（新建 `none`）：

| 值 | 谁可读 |
| --- | --- |
| `none` | 所有者 |
| `workspace` | 同租户同空间已登录用户 |
| `tenant` | 同租户已登录用户 |
| `anyone` | 任意已登录用户 |

可读：`GET /sessions/{id}`、snapshot、artifacts、trace、events、`files/access`、`feedback/reaction`。写（改标题/范围/澄清开关、删除、fork、以该 Session 为 parent 建 review、开聊/停跑/附件、steer、澄清、改计划、`files/share`、skill-draft、settlement、feedback 写入）仅所有者。`GET /sessions` 只列自己的。无独立 token。无权限或不存在 → `404`。

#### `POST /sessions`

创建空 Session，归属当前用户，前端可立即进入主对话页。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| title | str \| null | 可选初始标题；缺省可由首轮问题生成。审核会话由前端传入，例如 `审核：{源标题}` |
| datasource_id | str \| null | 可选初始数据源。审核会话不传，保持 null |
| agent_id | str | 绑定的 Agent；缺省为 `default`，Session 创建后不可修改。审核会话传 `review` |
| parent_session_id | str \| null | 可选源 Session。审核会话必传，指向被审核的 Session；不存在或不属于当前用户 → `404` |
| enable_clarification | bool | 可选；缺省 `true` |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| session | Session | `owner_user_id` 已填充 |

建审核 Session：传 `agent_id=review`、`parent_session_id`（源 Session）、`title`。源 Session 不动，不拷贝 Chat / Plan / 轨迹；新 Session `datasource_id`=`null`、`chat_count`=`0`、`status`=`idle`、`channel`=`console`，`forked_from_chat_id` 为空。不自动开聊。打开后走既有 snapshot / `POST /console/chat` / events / files。

#### `GET /sessions`

分页检索当前用户拥有的 Session。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| search_text | str \| null | URL 查询参数；按标题或对话内容搜索 |
| status | enum \| null | URL 查询参数；`idle` / `running`，见 §6 Session.status |
| datasource_id | str \| null | URL 查询参数 |
| sort | enum | URL 查询参数；`updated_desc` / `updated_asc` / `chat_count_desc` |
| page | int | URL 查询参数；页码，从 1 起 |
| page_size | int | URL 查询参数；每页条数 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| items | Session[] | |
| total | int | |

#### `GET /sessions/{session_id}`

返回 Session 元数据。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| session | Session | |

#### `PATCH /sessions/{session_id}`

更新 Session 元数据。仅所有者。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| title | str \| null | 传 null 表示不修改 |
| share_scope | enum \| null | `none` / `workspace` / `tenant` / `anyone`；传 null 表示不修改 |
| enable_clarification | bool \| null | 传 null 或不传表示不修改。写入后同步当前 `running` Chat |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| session | Session | |

#### `DELETE /sessions/{session_id}`

删除 Session 及其下各 Chat 的事件缓冲与投影。仅所有者。`running` → `CONFLICT`。不存在或已删除 → `404`。

#### `POST /sessions/{session_id}/fork`

从源 Session 的某一已完成 Chat 切开，生成独立的新 Session。源 Session 不动。新 Session 含切开点及之前的轮次，`status`=`idle`、`channel`=`console`，不自动开下一轮。打开新 Session 走既有 snapshot / trace。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| chat_id | str | 切开点；须属于该 Session、`status` 为 `completed`，且已保存 Agent 状态（即 snapshot 中 `forkable=true`） |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| session | Session | 新 Session；`parent_session_id` / `forked_from_chat_id` 已填充 |

Session 为 `running`、切开点 Chat 不是 `completed`，或没有可恢复的 Agent 状态时拒绝。Session / Chat 不存在或不属于当前用户 → `404`。

#### `GET /sessions/{session_id}/snapshot`

进入某一 Session 时按轮次窗口读取：返回一页 Chat 业务投影（含 `biz_events`、`segments` 等），供渲染主对话已发生过程。默认最近 15 轮。原始轨迹（`Message`，含 Steer 注入的 user Message）体量大，不进本接口，见打开页步骤与 `GET .../trace`。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| limit | int | URL 查询参数，默认 `15`，范围 `1..100`；本页轮次数 |
| before_sequence | int \| null | URL 查询参数；有值时取 `sequence < before_sequence` 的更早轮次。打开页取当前最老一轮的 `sequence` |

打开 / 刷新 / 重连（同一路径）：

1. 本接口取得最近一页 Chat 投影（含 `last_sequence_number`）。`has_more` 为 true 时，用本页最小 `sequence` 作 `before_sequence` 再取更早轮次；
2. 主对话需展示原始轨迹的 Chat：再调 `GET .../chats/{chat_id}/trace?view=raw`（或 `both`）；
3. 若该 Chat `status`=`running`：以 `last_sequence_number` 为 `after_sequence_number`，订阅 `GET .../chats/{chat_id}/events`；
4. 若 raw trace 中存在待回答的 `ask_user_question`（见 §6 Chat）：`Chat.enable_clarification` 为 `true` 时渲染确认卡片。answered 沿当前 SSE 继续；timeout 则当前 SSE 随该 Chat 进入 `canceled` 而结束。仅当连接自然断开时，才以更新后的 `last_sequence_number` 重订 `/events`；
5. 此后增量仅来自上述 SSE。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| session | Session | Session 元数据（标题、status、数据源等）。`chat_count` 为全 Session 轮次数 |
| chats | Chat[] | 本页轮次，按 `sequence` 升序；每项为 Chat 聚合（含 `plan`、`last_sequence_number`，见 §6 Chat） |
| has_more | bool | 更早方向是否还有轮次 |

#### `GET /sessions/{session_id}/chats/{chat_id}/events`

SSE。订阅**指定 Chat** 运行增量的**唯一**入口：推送该 Chat 内 `sequence_number > after_sequence_number` 的条目并持续推送新事件。开聊后接流、打开页接流均用本接口。权限只在订阅时检查。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| after_sequence_number | int | URL 查询参数，默认 `-1`；订阅起点，只推 `sequence_number > after` 的事件。开聊固定 `-1`；打开页取 snapshot 的 `last_sequence_number`。也可用请求头 `Last-Event-ID` |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| （SSE 流） | 流式对象 | `text/event-stream`；见 §5；条目的 `chat_id` 均为路径中的 Chat |

#### `POST /console/chat`

创建正式 Chat 并异步启动 Agent；JSON 回执，不推送 SSE。Session 已有活动 Chat（`status=running`）时拒绝，返回 `CONFLICT`。客户端随即以返回的 `chat.id` 订阅 `GET .../events`。

当前用户未配置可用 ActiveModels（见 §4.8）时返回 `VALIDATION`，不创建 Chat。

Session 唯一数据源：`datasource_id` 必填。`Session.datasource_id` 为空时按本次请求写入；已绑定则须与请求一致，否则 `VALIDATION`。客户端首 Chat 成功后锁定选择器。


开聊顺序：

1. 本接口取得 `chat`；
2. `GET .../chats/{chat_id}/events?after_sequence_number=-1`；
3. 运行增量仅来自上述 SSE。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| session_id | str | 目标 Session（仅所有者） |
| text | str | 用户输入 |
| datasource_id | str | 必填；须与已绑定的 Session 数据源一致 |
| attachments | AttachmentRef[] \| null | 附件 |
| artifact_comments | ArtifactComment[] | 可选；本轮对成果文件的行评。Host 原样写入 Chat，不改回传的 `text`；喂 Agent 的 UserMsg 会拼上这些评论 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| chat | Chat | 已创建的轮次；`status` 一般为 `running`；`last_sequence_number` 为 `-1` |

#### `POST /console/upload`

上传对话附件，返回可供 `attachments` 引用的文件描述。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| file | multipart | 文件本体 |
| session_id | str | 归属 Session |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| attachment | AttachmentRef | 见 §6 AttachmentRef |

### 4.2 Steer

#### `POST /sessions/{session_id}/chats/{chat_id}/steer`

向当前活动 Chat 注入用户引导。Host 接受后立即返回 `200` + `{"ok": true}`，在下一安全 ReAct 边界落入 Agent 上下文。
同一 Chat 上多次 Steer 按到达顺序 FIFO 串行处理。
注入后的轨迹条目不由本响应携带，经该 Chat 的 SSE / `GET .../trace` 以 `Message` 出现（见下）。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| text | str | 引导正文 |
| artifact_comments | ArtifactComment[] | 可选；本条引导对成果文件的行评。Host 原样写入 hint Message `metadata`，不改 SSE 的 `text`；喂 Agent 的 HintBlock 会拼上这些评论 |

成功：

| HTTP | 响应体 |
| --- | --- |
| `200` OK | `{"ok": true}` |

失败：错误体形状见 §3.1（`code` / `message` / `details`）；未注入。

| 场景 | HTTP | `code` | `details.reason` | `message`（示例） |
| --- | --- | --- | --- | --- |
| 当前活动 Chat 已结束（进入终态，无法再注入） | `409` | `CONFLICT` | `CHAT_ENDED` | `active chat has ended` |

注入投影：sse或trace中：`role`=`user`、`type`=`hint` 的 Message；`metadata.source`=`steer`；有行评时同对象带 `artifact_comments`；正文为 `text`。不进 snapshot。

### 4.3 Clarification Answer / Stop

#### `POST /sessions/{session_id}/chats/{chat_id}/clarification/answer`

提交当前待回复的 `ask_user_question` 工具消息结果。Host 接受后立即返回；answered 在同一 Chat 异步续跑，timeout 异步取消当前 Chat。

请求字段：


| 字段               | 类型                        | 说明                                                               |
| ---------------- | ------------------------- | ---------------------------------------------------------------- |
| clarification_id | str                       | 目标确认标识；其值必须等于对应 `ask_user_question` 工具调用的 `FunctionCall.call_id` |
| result           | AskUserQuestionToolResult | 前端提交的确认结果；`answered` 时 Host 原样回填给 LLM |


响应字段：


| 字段   | 类型   | 说明                                                                                    |
| ---- | ---- | ------------------------------------------------------------------------------------- |
| chat | Chat | 同一 `chat_id`；接受成功时本字段仍为 `running`。answered 后续继续该 Chat；timeout 随后将该 Chat 置为 `canceled` |


约束：

- `clarification_id` 必须命中当前 Chat 中待回答的那次 `ask_user_question` 工具调用。
- `result` 必须符合 `ask_user_question` 对 LLM 暴露的 tool result 结构。
- 前端负责选择完整性、单选/多选与自由输入规则，并直接构造语义答案；Host 不根据 tool arguments 二次校验或转换。
- `result.status`=`timeout` 时，Host 停止后续 ReAct，并将当前 Chat 置为 `canceled`。
- 本接口不以 Chat status 表示等待态；等待与否只取决于是否存在待回答的 `ask_user_question`。
- `Chat.enable_clarification` 为 `true` 时等人作答；为 `false` 时 Host 在等待处自动作答。该字段开轮时从 Session 复制；`PATCH /sessions` 写入时同步当前 `running` Chat。

#### `POST /sessions/{session_id}/chats/{chat_id}/tool-confirmation/answer`

批准或拒绝当前待确认的工具调用。Host 接受后立即返回；同一 Chat 继续跑。拒绝和超时都把这次工具标为失败，不取消 Chat。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| call_id | str | 对应 `plugin_call` 的 `FunctionCall.call_id` |
| decision | enum | `approved` / `denied` |

响应 `{ chat }`。Chat 仍为 `running`。

`tool_confirmation_level` 是用户 RuntimeSettings：`off` 全部直接执行；`auto`（缺省）按 Host 名单 `auto_ask_names` 原样匹配，命中才等人确认；`all` 除 `ask_user_question` 外都确认。开一轮时读取，下一轮 Chat 才换新值。超时未确认按拒绝处理。待确认调用的 `plugin_call` Message `metadata` 见 §6 Message。

错误码：

错误体形状见 §3.1；本接口重点使用 `code` / `message` / `details.reason` 表达错误。


| 场景 | HTTP | `code` | `details.reason` |
| --- | --- | --- | --- |
| Chat 与 `session_id` / `chat_id` 不匹配 | `404` | `NOT_FOUND` | |
| Chat 不是 `running` | `409` | `CONFLICT` | |


#### `POST /console/chat/stop`

停止指定 Chat 的运行。回执中的 `chat` 可能仍为 `running`；终态以该轮 SSE 的 `response`（`status`=`cancelled`）为准。若回执已是 `canceled`，直接采用。
Session current Plan 保持当前状态，后续 Chat 可继续执行。

请求字段：


| 字段         | 类型  | 说明               |
| ---------- | --- | ---------------- |
| session_id | str | URL 查询参数         |
| chat_id    | str | URL 查询参数；活动 Chat |


响应字段：


| 字段   | 类型   | 说明                   |
| ---- | ---- | -------------------- |
| chat | Chat | `running` 或 `canceled` |


### 4.4 Plan

#### `POST /sessions/{session_id}/chats/{chat_id}/plan/edit`

用户提交完整 Plan 定义，覆盖指定 Chat 的 Plan；`plan=null` 表示清空。
后到的编辑覆盖先到的编辑。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| plan | Plan \| null | 完整 Plan 定义；null 表示清空 |
| reason | str \| null | 可选的本次修改原因；不持久化 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| plan | Plan \| null | 当前 Plan 投影 |

活动 Chat 热更新且不中断运行。

### 4.5 Artifact

- **Agent 视角**：各 Chat 的 `artifacts`（薄索引）+ SSE `artifact.registered`；全 Session 合并列表见 `GET /sessions/{session_id}/artifacts` 的 `artifacts`
- **业务视角**：各 Chat 的 `segments[].artifact`；全 Session 合并列表见同一接口的 `segment_artifacts`
- **正文**：`GET /sessions/{session_id}/files/access?path=&purpose=`（`preview` / `download`）；`path` 相对 Session 产物目录。跨 Chat 合并列表时同 path 去重，内容始终读磁盘最新。
- **外网分享**：`POST /sessions/{session_id}/files/share`。仅所有者。请求 `{ path }`（相对产物目录）。Host 上传文件到私有 OSS，返回 `{ url, expires_at, name }`。`url` 是 OSS 预签名下载链接（`Content-Disposition: attachment`），默认 7 天内有效。每次分享覆盖上传同一路径。接收方不访问 Host。产品只对 `segments[].artifact` 暴露此入口。预览走 `/files/access`。

#### `GET /sessions/{session_id}/artifacts`

全 Session 两种视角的产物索引。不扫磁盘。不替代 snapshot 的 `chats[].artifacts` / `chats[].segments`。运行中增量仍走 SSE `artifact.registered` 与 `segment`。

- `artifacts`：Agent 视角。按 `path` 去重，保留 `updated_at` 最新的一行。
- `segment_artifacts`：业务视角。按 `relative_path` 去重，保留先出现的 `name` / `description`，`segments` 收集引用过该路径的全部分段。按 Chat `sequence`、Segment `coverage` 升序扫描。

权限：可读该 Session。无权限或不存在 → `404`。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| artifacts | Artifact[] | 按 `updated_at` 倒序；每项带 `chat_id`（最后写入的轮次） |
| segment_artifacts | SessionSegmentArtifact[] | 业务视角；每项带 `chat_id`（首次出现的轮次）与 `segments` |

### 4.6 Trace / Feedback

#### `GET /sessions/{session_id}/chats/{chat_id}/trace`

按 Chat 拉取轨迹。`biz_events` 已在 snapshot 的 Chat 中；本接口主要用于原始轨迹（`messages`），或一次性同时取两者。运行中增量见 SSE（`object=message` / `content`，以及 `biz_event` / `segment`）。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| view | enum | URL 查询参数，见下表 |

| view | 含义 | 响应 |
| --- | --- | --- |
| `raw` | 原始轨迹 | 填充 `messages` 和 `mcp_connections`；`biz_events` 为 `[]` |
| `business` | 业务轨迹；默认不含 `channel=subagent` | 仅填充 `biz_events`；`messages` 为 `[]`（与 snapshot 重复时可不再调） |
| `both` | 同时返回两者 | 填充 `messages`、`biz_events` 和 `mcp_connections` |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| messages | Message[] | 原始轨迹；`view`=`business` 时为 `[]` |
| mcp_connections | McpConnectEvent[] | 按 `sequence_number` 排序的失败连接事件；`view`=`business` 时为 `[]`。前端在本轮用户输入后、Agent 原始消息前按序展示失败工具卡片，成功事件不展示 |
| biz_events | BizEvent[] | 业务轨迹；`view`=`raw` 时为 `[]`；默认仅含 `channel=main` |

#### `POST /sessions/{session_id}/chats/{chat_id}/feedback`

以本响应为回执。`like` / `dislike` 表示当前用户对 Chat 的单一 Reaction：
重复提交幂等，切换类型时覆盖原 Reaction；`copy` / `artifact_comment` 仍按事件追加。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| kind | enum | `like` / `dislike` / `copy` / `artifact_comment` |
| reason | str \| null | |
| detail | str \| null | |
| artifact_ref | ArtifactLineRef \| null | `kind`=`artifact_comment` 时必填 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| feedback | Feedback | `owner_user_id` 为当前用户 |

#### `GET /sessions/{session_id}/chats/{chat_id}/feedback/reaction`

读取当前用户对 Chat 的点赞/点踩状态。响应为 `feedback: Feedback | null`。

#### `DELETE /sessions/{session_id}/chats/{chat_id}/feedback/reaction`

清除当前用户对 Chat 的点赞/点踩状态。成功返回 `204`；重复清除仍返回 `204`。

### 4.7 数据源

#### `GET /datasources`

当前用户可见的数据源列表（权限由 DataBridge / Host 鉴权结果决定）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| items | DatasourceOption[] | 见 §6 DatasourceOption |

### 4.8 Preferences

用户级模型与运行配置。可选 Provider 由服务端固定提供，客户端不可新建 Provider；可配置凭证、扩展/覆盖模型，并设置 ActiveModels 与 RuntimeSettings。`api_key` 明文仅出现在写入请求中，读取接口只返回脱敏字段。

#### `GET /preferences/providers`

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| providers | Provider[] | 见 §6 Provider |

#### `PUT /preferences/providers/{provider_id}`

配置 / 更新指定 Provider 的凭证。首次配置时 `api_key` 必填；之后可只改 `base_url` 或轮换 `api_key`。未知 `provider_id` → `VALIDATION`。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| api_key | str \| null | 省略表示不修改已有 key |
| base_url | str \| null | 省略表示不修改；显式 `null` 表示使用 Provider 默认地址 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| provider | Provider | |

#### `DELETE /preferences/providers/{provider_id}`

清除该 Provider 的用户凭证。未配置过 → `NOT_FOUND`。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| ok | bool | `true` |

#### `GET /preferences/providers/{provider_id}/models`

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| models | ProviderModel[] | 见 §6 ProviderModel |

#### `PUT /preferences/providers/{provider_id}/models/{model_id}`

新增或更新模型配置。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| source | enum | `extra` / `override` |
| name | str \| null | `extra` 时必填 |
| context_size | int \| null | |
| thinking_enabled | bool \| null | |
| generate_kwargs | object \| null | |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| model | ProviderModel | |

#### `DELETE /preferences/providers/{provider_id}/models/{model_id}`

删除用户侧模型配置。不存在 → `NOT_FOUND`。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| ok | bool | `true` |

#### `GET /preferences/active-models`

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| active_models | ActiveModels | 见 §6 ActiveModels |

#### `PUT /preferences/active-models`

设置激活模型。所选模型当前不可用（如 Provider 未配置）→ `VALIDATION`。`light_*` 须成对出现或同时省略。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| default_provider_id | str | |
| default_model_id | str | |
| light_provider_id | str \| null | |
| light_model_id | str \| null | 与 `light_provider_id` 成对 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| active_models | ActiveModels | |

#### `GET /preferences/runtime-settings`

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| runtime_settings | RuntimeSettings | 见 §6 RuntimeSettings |

#### `PUT /preferences/runtime-settings`

部分更新（仅提交的字段生效）。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| react_max_iters | int \| null | |
| llm_retry_enabled | bool \| null | |
| llm_max_retries | int \| null | |
| context_trigger_ratio | float \| null | |
| context_reserve_ratio | float \| null | |
| tool_result_limit | int \| null | |
| tool_timeout_enabled | bool \| null | 是否启用单次工具执行超时 |
| tool_timeout_seconds | float \| null | 单次工具执行上限（秒），最小 1 |
| tool_confirmation_level | enum \| null | `off` / `auto` / `all`。见 §4.3 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| runtime_settings | RuntimeSettings | |

### 4.9 Skills

只有一层技能：Agent 工作区 `workspace/skills/<agent_id>/`（AgentScope 的技能分区，按 frontmatter `name` 平铺）。目录是运行时的唯一来源，`.index` 由 AgentScope 维护；`source` / `group` 由打包内置技能反查现算（出现在包里即 `builtin`）。每次写入同时向 DB 表 `skill_revisions` 追加一份全目录快照，供查看历史与回滚。启停为 Host 侧软标记（DB 表 `agent_skill_states` 的 `disabled`，无行即默认启用），目录保留，被禁用的技能不进 Toolkit。

行为约定：

- 打开 Agent / 访问 skills API 时，`ensure_ready` 把打包内置 skill 中该 Agent 尚缺的补齐（只增，不覆盖已有目录）；无需单独安装接口。
- 内置 skill（`source=builtin`）**人工不可改、不可卸载**：`PUT` 与 `DELETE` 一律 `FORBIDDEN`；只有技能自我演进（`origin=evolution_accept`）能改写。自定义 skill 可编辑内容与卸载，**不可改名**（目录名与 frontmatter `name` 创建后固定）。
- 编辑带 `base_content`（客户端读到的 `SKILL.md` 原文）；与磁盘现状不一致 → `CONFLICT`，避免覆盖别人的改动。
- 首次改动前若该 skill 尚无历史，会自动补一条 `origin=install` 的初始版本，回滚始终有底。
- 会话草稿：对已终态 Chat 调 `POST /sessions/{session_id}/skill-drafts`。Host 建行 `status=generating`、`files={}`，**不**写 `artifact_dir` / `workspace/skills`，**不**进 `artifacts` / `settlement_cards`。算法自行 `generate`（历史为该 Session 中 `sequence <=` 源 Chat；始终新建，不去重跳过；经 repository 写 `files`，`status=ready`；失败 `failed`）。改稿走 `POST .../revise`，不走 `/console/chat`。详情读 `files["SKILL.md"]`，不走 `/files/access`。不往 UserMsg 塞「请写 skill」。草稿状态见 §4.9.1。
- 确定：`POST .../confirm` 仅 `ready`；只安装 `SKILL.md` 与 `scripts/`、`references/`（侧车不装），写入**该 Session 所属 Agent** 的 `workspace/skills/<agent_id>/<name>/`（`source=custom`、`enabled=true`），并追加 `skill_revisions`（`origin=import`）。`name` 不落表，从 `SKILL.md` frontmatter 现算。

#### `GET /skills/agents/{agent_id}`

列出指定 Agent 已安装 skills（读前补齐缺失 builtin）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| skills | AgentSkill[] | 见 §6 AgentSkill |

#### `GET /skills/agents/{agent_id}/{name}`

按名读取该 Agent 的 `SKILL.md` 全文。不存在 → `NOT_FOUND`；目录在但缺 `SKILL.md` → `VALIDATION`。

响应字段：见 §6 AgentSkillDetail。

#### `POST /skills/agents/{agent_id}`

手写一份新 skill。`name` 须与 `content` frontmatter 的 `name` 一致；同名已存在 → `CONFLICT`；无 frontmatter → `VALIDATION`。落 `origin=create` 版本。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| name | str | 目录名，须等于 frontmatter `name` |
| content | str | `SKILL.md` 原文（含 YAML frontmatter） |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| created | str | 新建的 `name` |

#### `PUT /skills/agents/{agent_id}/{name}`

编辑一份自定义 skill 的 `SKILL.md`；**不可改名**：请求体 `name` 必须等于路径 `{name}`，且与 frontmatter `name` 一致，否则 `VALIDATION`。builtin → `FORBIDDEN`；`base_content` 与磁盘不一致 → `CONFLICT`。落 `origin=manual_edit` 版本。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| name | str | 必须等于路径 `{name}`；与路径不同 → `VALIDATION`（禁止改名） |
| content | str | 新的 `SKILL.md` 原文；frontmatter `name` 须等于路径 `{name}` |
| base_content | str \| null | 客户端读到的 `SKILL.md` 原文；`null` 跳过并发校验 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| saved | str | 保存后的 `name` |

#### `POST /skills/agents/{agent_id}/import`

上传 zip 导入 skill 到该 Agent。`multipart/form-data`，字段名 `file`；zip 内须含至少一个 `SKILL.md`（可多包一并导入）。已存在同名 → `CONFLICT`。每个导入项落一条 `origin=import` 版本。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| imported | str[] | 导入成功的 skill `name` 列表 |

#### `POST /skills/agents/{agent_id}/{name}/enable`

启用已安装 skill。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| enabled | bool | `true` |
| name | str | |

#### `POST /skills/agents/{agent_id}/{name}/disable`

禁用已安装 skill（目录保留）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| enabled | bool | `false` |
| name | str | |

#### `DELETE /skills/agents/{agent_id}/{name}`

从 Agent 卸载 skill（删目录与 manifest 项，历史版本保留）。不存在 → `NOT_FOUND`。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| uninstalled | str | |

#### `GET /skills/agents/{agent_id}/{name}/revisions`

列出该 skill 的版本历史，按 `revision_no` 倒序（首项即当前内容）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| revisions | SkillRevision[] | 见 §6 SkillRevision |
| count | int | 条数 |

#### `GET /skills/agents/{agent_id}/{name}/revisions/{revision_no}`

读取某个版本的全目录文件快照。不存在 → `NOT_FOUND`。

响应字段：见 §6 SkillRevisionDetail。

#### `POST /skills/agents/{agent_id}/{name}/revisions/{revision_no}/revert`

按该版本快照覆盖当前目录（多余文件删除），并追加一条 `origin=revert` 的新版本；历史只增不改。builtin 也可回滚。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| ok | bool | `true` |
| revision | SkillRevision | 新追加的版本 |

### 4.9.1 Skill drafts

对已终态分析 Chat 起草可确认安装的技能。生成在后台进行；HTTP 立即返回。确认后写入该 Session 所属 Agent 的 `workspace/skills/<name>/`，并落 `skill_revisions`。

状态机：`generating` → `ready` / `failed`；`ready` → `revising` → `ready` / `failed`；`ready` → `accepted`（确认安装）或 `dismissed`。`accepted` 不可再改。

`files` 可含 `SKILL.md`、`scripts/`、`references/`，以及算法侧车（`evidence/digest.md`、`skill-card.md`、`decision.json`、`report.md`）。确认时只安装 `SKILL.md` 与 `scripts/`、`references/`。

#### `POST /sessions/{session_id}/skill-drafts`

对已终态源 Chat 建一条草稿，立刻回执。源 Chat 须属该 Session，且 `status` 为 `completed` / `failed` / `canceled`，否则 `VALIDATION`。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| source_chat_id | str | 范围是这一轮及之前（算法读历史时 `sequence <=` 该 Chat） |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| skill_draft | SkillDraft | 新建行；`status=generating`；`files` 为空对象 |

#### `GET /sessions/{session_id}/skill-drafts`

按 Session 列出草稿（新到旧）。列表不下发 `files` 全文，带现算的 `name`。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| skill_drafts | SkillDraft[] | 无 `files` |
| count | int | |

#### `GET /sessions/{session_id}/skill-drafts/{draft_id}`

草稿详情（含 `files`）。不存在 → `NOT_FOUND`。预览读这里的 `files`，不走 `/files/access`。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| skill_draft | SkillDraft | 含 `files` |

#### `POST /sessions/{session_id}/skill-drafts/{draft_id}/revise`

仅 `ready` → `revising`。`text` 去空白后为空 → `VALIDATION`；当前不是 `ready` → `VALIDATION`。算法自行改 `files` 并写回 `ready`（失败 `failed`）。`text` 不存对话历史。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| text | str | 当次改稿说明；不存对话历史 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| skill_draft | SkillDraft | `status=revising` |

#### `POST /sessions/{session_id}/skill-drafts/{draft_id}/confirm`

仅 `ready`。只把 `SKILL.md` 与 `scripts/`、`references/` 写入该 Session 所属 Agent 的 `workspace/skills/<name>/`，追加 `skill_revisions`（`origin=import`），再标 `accepted`。不调算法。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| created | str | 新登记的 Agent skill `name`（取自 frontmatter `name`） |
| skill_draft | SkillDraft | `status=accepted` |

校验：不是 `ready` / 缺 `SKILL.md` / frontmatter `name` 非法 → `VALIDATION`；该 Agent 已有同名 skill → `CONFLICT`。

#### `POST /sessions/{session_id}/skill-drafts/{draft_id}/dismiss`

丢弃草稿。已 `accepted` → `VALIDATION`。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| skill_draft | SkillDraft | `status=dismissed` |

### 4.10 CronJob

按当前登录身份（user / tenant / workspace）隔离的定时任务。MVP **仅支持 agent 执行**：到点或手动触发时，Host 新建一条 `channel=console` 的 Session 与 Chat（`user_input=message`，绑定 `datasource_id`），再走既有 `ChatRuntime`；执行过程与产物通过该 Chat 的 snapshot / SSE / trace 查看，**不**另开推送通道。到点执行时经 `generate_temporary_token` 现查成员/role 并签短时 access 后跑 agent（CM 再校验可见性）；**不**依赖用户在线 cookie。

行为约定：

- 创建 / 全量替换请求体见 §6 `CronJobWrite`；响应中的任务见 §6 `CronJob`。
- `schedule.type=cron`：客户端提交 **5 段** crontab 字符串（分 时 日 月 周）。「每天 9:30」等 UI 拼装由**前端**完成后再提交；Host 校验段数，并将星期数字规范为 `sun`/`mon`/… 以便解析。
- `schedule.type=once`：必须带 `run_at`；Host 清掉 `cron`。
- `POST .../pause` / `resume` 等价于把 `enabled` 设为 `false` / `true`。Host 按库中的下次时间触发，不另同步本机调度器。
- `POST .../run`：立即异步触发一次（不改变 schedule，停用任务也可）；HTTP 只回执已受理，跑完后的对话在会话列表中出现。空 `session_id` 时新建一条 console Session。
- 「从分析计划提炼为定时任务」为**前端**预填 `name` / `message`（及当前 Chat 的 `datasource_id`）后调创建接口；Host 无单独提炼端点。无数据源时前端应拦截，勿提交空 `datasource_id`。

#### `GET /cron/jobs`

列出当前身份下的定时任务（新到旧）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| jobs | CronJob[] | 见 §6 CronJob |
| count | int | |

#### `POST /cron/jobs`

创建定时任务。Host 按库中的下次时间触发。

请求体：`CronJobWrite`（§6）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| job | CronJob | |

校验失败（缺字段、非法 cron 等）→ `422`。

#### `GET /cron/jobs/{job_id}`

读取单个任务。不存在或不属于当前身份 → `NOT_FOUND`。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| job | CronJob | |

#### `PUT /cron/jobs/{job_id}`

全量替换任务定义。请求体同创建：`CronJobWrite`。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| job | CronJob | |

#### `DELETE /cron/jobs/{job_id}`

删除任务。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| ok | bool | `true` |

#### `POST /cron/jobs/{job_id}/pause`

禁用任务（`enabled=false`）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| job | CronJob | |

#### `POST /cron/jobs/{job_id}/resume`

启用任务（`enabled=true`）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| job | CronJob | |

#### `POST /cron/jobs/{job_id}/run`

立即执行一次；后台开跑，本接口不等待 Chat 结束。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| ok | bool | `true` |
| job | CronJob | 触发时的任务快照 |

### 4.11 我的用量

配置中心「我的用量」页，只读。身份固定为当前登录用户的 tenant / workspace / user，**不得**用查询参数指定他人。租户管理员在本页也只看自己。

四个接口共用时间窗。前端先请求 Summary，把响应里的 `window.as_of` 传给其余三个，保证同页快照。只统计 `created_at ∈ [start, end)` 且 `created_at <= as_of` 的调用。

公共查询参数：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| period | enum | URL 查询参数；`1d` / `7d` / `30d`，默认 `7d`。`1d` 为用户时区当日 00:00 至 `as_of`；`7d` / `30d` 为含当日的最近 7 / 30 个自然日 |
| timezone | str \| null | IANA；缺省读用户画像，再缺省 `Asia/Shanghai`。非法 → `VALIDATION`，`details.reason`=`INVALID_TIMEZONE` |
| as_of | datetime \| null | RFC 3339；省略则服务端取当前时间并回传。晚于当前时间 → `VALIDATION`，`details.reason`=`AS_OF_IN_FUTURE` |

公共响应 `window` 见 §6 UsageWindow。Token 为整数，`K` / `M` 由前端格式化。不返回 `cached_tokens`、`source_step`。生成对应 `model_type=text`，Embedding 对应 `model_type=embedding`。供应商未返回 usage 时该行 input / output 记 `0`。

```text
总 Token = generation.total_tokens + embedding.tokens
```

period / page / page_size 非法 → `VALIDATION`。未登录 → `401`。无当前工作空间 → `403`。

对账（同一 identity、window、timezone、as_of）：

```text
summary.generation = timeseries.totals.generation
  = sessions.totals.generation + background_tasks.totals.generation
summary.embedding = timeseries.totals.embedding
  = sessions.totals.embedding + background_tasks.totals.embedding
summary.sessions.count = sessions.totals.sessions = sessions.pagination.total
summary.background_tasks.count = background_tasks.totals.groups = background_tasks.pagination.total
```

#### `GET /usage/summary`

顶部统计卡。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| window | UsageWindow | |
| generation | GenerationUsage | |
| embedding | EmbeddingUsage | |
| total_tokens | int | `generation.total_tokens + embedding.tokens` |
| sessions | object | `{count}`：窗口内有用量的 Session 数 |
| background_tasks | object | `{count}`：窗口内后台任务来源数 |

#### `GET /usage/timeseries`

趋势。`1d` 每 4 小时一桶；`7d` / `30d` 每自然日一桶。服务端返回完整桶（含 0），前端不补点、不分桶。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| window | UsageWindow | |
| granularity | enum | `hour4` / `day` |
| points | UsageTimeseriesPoint[] | 按时间升序 |
| totals | object | `{generation, embedding, total_tokens}`；全窗口合计，等于各 point 之和 |

#### `GET /usage/sessions`

Session 用量。一行一个 `session_id`。`session_id` 非空的调用按 Session 聚合。默认 `last_activity_at DESC, session_id ASC`。分页在聚合之后。软删除 Session 仍返回用量，`session_state`=`deleted`；标题从 Host `sessions` 读取，读不到则为「未命名 Session」。

附加查询参数：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| page | int | 默认 `1`，最小 `1` |
| page_size | int | 默认 `20`，范围 `1..100` |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| window | UsageWindow | |
| items | SessionUsageItem[] | 当前页 |
| totals | object | `{sessions, generation, embedding, total_tokens}`；**全部**匹配 Session 的合计，不是当前页 |
| pagination | UsagePagination | `total` 为聚合后的 Session 数 |

不返回任务步骤或 `source_step`。

#### `GET /usage/background-tasks`

后台任务用量。`session_id` 为空的调用按 `source` 聚合。默认 `total_tokens DESC, last_activity_at DESC, group_id ASC`。分页在聚合之后。只返回 `source`，不返回任务名称、任务 ID、`source_step`。

附加查询参数同 Session 接口。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| window | UsageWindow | |
| items | BackgroundTaskUsageItem[] | 当前页 |
| totals | object | `{groups, generation, embedding, total_tokens}`；全部来源合计，不是当前页 |
| pagination | UsagePagination | `total` 为聚合后的来源数 |

---

## 5. SSE 运行增量

Agent 运行过程中的增量仅通过 SSE 推送。唯一入口：`GET /sessions/{session_id}/chats/{chat_id}/events`。

`POST /console/chat` 只负责创建并启动，以 JSON 返回 `chat`；订流一律走 `/events`。用户写操作（停跑、确认、改计划等）以对应 REST 响应为回执，不在本流重复推送写回执。Steer 的 REST 仅回报成败；注入后的 user Message 作为运行轨迹增量出现在本流（亦进 `GET .../trace`），不进 snapshot。其余历史过程由 snapshot / trace 读取。

### 5.0 生命周期与顺序

本 Chat 仅一条事件流，共用同一 `sequence_number` 序列。

`GET .../events` 按序推送该 Chat 的增量（含 `content`）。运行中重连从
`after_sequence_number` 续上；终态后回放只剩完整 `message`。

**成功路径**

`response`（`status`=`completed`）为流内最后一条业务事件。此前可交错推送：

- `response`（`created` → `in_progress`）；
- `mcp.connect`（每个已启用 MCP 一条；在 `in_progress` 之后、执行过程之前）；
- `message` / `content` 等执行过程事件；
- `biz_event`、`segment`、`artifact.registered`、`context.usage`（完成即推，可出现在执行过程中途，也可多段交替出现）；
- `followup.generated`（若有；须在本轮最终 `completed` 之前）。

`response.status`=`completed` 表示：本轮已决定进入本 SSE 的事件均已推送完毕。其后本流不再推送业务事件。

**等待用户回复**

1. Host 实时转发所有 tool call 和 tool result，包括参数校验失败的 `ask_user_question` 调用及其错误结果。
2. 合法的 `ask_user_question` 发布后，当前 SSE 保持连接且不推送 `response`（`status`=`completed`）；Chat 保持 `running`。
3. `Chat.enable_clarification` 为 `true` 时，客户端按 raw trace 渲染确认卡片并通过 `POST .../clarification/answer` 上报；为 `false` 时 Host 自动作答（见 §4.3）。
4. Answer 接受后立即返回，响应中 `chat.status` 仍为 `running`。
5. 若 `result.status`=`answered`：Host 原样回填 result，在同一 Chat 异步续跑，tool result 与后续事件沿当前 SSE 发送。
6. 若 `result.status`=`timeout`：Host 停止后续 ReAct，将当前 Chat 置为 `canceled`；当前 SSE 以 `response`（`status`=`cancelled`）结束；后续需新开 Chat。
**失败 / 取消**

此前已产生的 `mcp.connect` / `biz_event` / `segment` / `artifact.registered` 尽数推送后，再推送 `response`（`status`=`failed` / `cancelled`）作为流内终态。`status`=`failed` 时 `error` 为 `ChatError`（见 §6）。`context-manager` 探测失败时，先推该条 `mcp.connect`（`status`=`failed`），再推 `response`（`status`=`failed`）。

### 5.1 线格式

响应头：`Content-Type: text/event-stream`。

每条事件：

| SSE 字段 | 取值 |
| --- | --- |
| `id` | `sequence_number` |
| `event` | `object` |
| `data` | 该对象的 JSON |

`data` 公共字段（每条均含）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| sequence_number | int | **Chat 内**从 `0` 起单调递增，由 Host 对该 Chat 持久分配；与 `chat_id` 共同标识一条事件 |
| object | str | 对象种类，见 §5.2 |
| session_id | str | 所属 Session |
| chat_id | str | 归属轮次；本条序号计入该 Chat |

### 5.2 对象种类

`data.object` 为对象鉴别符，取值分为基础对象与领域扩展。每条 `data` 在 §5.1 公共字段之外，按 `object` 携带专有字段。

#### `response`

本轮 SSE 运行态。顺序与终态语义见 §5.0。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | 本轮 response 标识 |
| status | enum | `created` / `in_progress` / `completed` / `failed` / `cancelled`。`completed`：本轮进流事件已全部推送，本条为流内最后一条业务事件 |
| usage | object \| null | 终态可选；令牌用量 |
| error | ChatError \| null | `status`=`failed` 时必填；见 §6 ChatError |

#### `message`

原始轨迹中的一条消息。专有字段见 §6 Message；流式推送时 `object` 固定为 `message`。

#### `content`

某条 Message 的内容块或其流式片。专有字段见 §6 Content。

流式推送时 `object=content`，`msg_id` 必填。

| `delta` | 客户端 | 载荷 |
| --- | --- | --- |
| `true` | 拼接 | 文本：`text` 增量片；`plugin_call`：`data.arguments` 增量片（中间可只有 `arguments`） |
| `false` | 覆盖 | 文本：该块全文；`plugin_call`：完整 `data`；`plugin_call_output`：完整 `data`（可多次推，`output` 为累计快照，收尾可带 `state`） |

`message`（`completed`）里的 `content[]` 是最终块，可不带 `msg_id`。

#### `error`

流内错误。专有字段见 §3.1（`code` / `message` / `details`）。

#### 领域扩展

`object` 取值为 §5.3 中的扩展名（例如 `segment`、`biz_event`）。专有字段与公共字段同层，见 §5.3。

### 5.3 领域扩展 object

以下各节字段均与 §5.1 公共字段同层。嵌套类型定义见 §6。

| object | 说明 |
| --- | --- |
| `segment` | Segment 完成时推送；须在 `response.completed` 之前 |
| `biz_event` | BizEvent 状态更新时推送；须在 `response.completed` 之前 |
| `artifact.registered` | Agent 视角文件登记（产物目录 diff）；须在 `response.completed` 之前 |
| `followup.generated` | 推荐追问就绪；须在 `response.completed` 之前 |
| `context.usage` | 当前上下文占用；须在 `response.completed` 之前 |
| `mcp.connect` | 开聊探测已启用 MCP 的连通结果；须在 `response.completed` 之前 |




#### `segment`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| segment | Segment | 已完成的分段 |

#### `biz_event`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| biz_event | BizEvent | 业务事件；同一 `event_id` 后到覆盖先到 |

#### `artifact.registered`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| artifact | Artifact | 登记后的成果元数据；同一 `artifact.id` 后到覆盖先到 |

#### `followup.generated`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| followup | FollowUp | 推荐追问 |

#### `context.usage`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| context_size | int | |
| used_tokens | int | |
| trigger_ratio | float | |
| reserve_ratio | float | |
| tool_result_limit | int | |

#### `mcp.connect`

开聊时对每个已启用 MCP 推一条。HTTP MCP 带 `url`，STDIO MCP 带 `command`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| mcp_name | str | MCP 名 |
| status | enum | `connected` / `failed` |
| error | str \| null | `failed` 时的错误信息 |
| url | str \| null | HTTP MCP 地址 |
| command | str \| null | STDIO MCP 启动命令 |

`context-manager` 为 `failed` 时本轮结束。其他 MCP `failed` 后继续开聊。

### 5.4 完整流程示例

场景：用户在已有 Session 发起一轮分析（`POST /console/chat` 已返回 `chat_1`，客户端已订阅 `GET .../events`）。顺序符合 §5.0。下列为该 Chat 上 SSE `data` 的示意序列（字段可裁剪；嵌套实体见 §6）。`session_id=ses_1`，`chat_id=chat_1`。

每种 `message.type` / 领域 `object` **各出现一次**，且流式路径写全（`in_progress` → `content` → `completed`），不省略中间事件。

```
data: {"sequence_number":0,"object":"response","session_id":"ses_1","chat_id":"chat_1","id":"response_1","status":"created"}

data: {"sequence_number":1,"object":"response","session_id":"ses_1","chat_id":"chat_1","id":"response_1","status":"in_progress"}

data: {"sequence_number":2,"object":"mcp.connect","session_id":"ses_1","chat_id":"chat_1","mcp_name":"context-manager","status":"connected","url":"http://cm/mcp"}

data: {"sequence_number":3,"object":"message","session_id":"ses_1","chat_id":"chat_1","id":"msg_1","sequence":1,"type":"reasoning","role":"assistant","status":"in_progress","content":[],"source_id":"blk_think_1"}

data: {"sequence_number":4,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"text","delta":true,"msg_id":"msg_1","index":0,"text":"先确认口径与时间范围…"}

data: {"sequence_number":5,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"text","delta":false,"msg_id":"msg_1","index":0,"text":"先确认口径与时间范围…"}

data: {"sequence_number":6,"object":"message","session_id":"ses_1","chat_id":"chat_1","id":"msg_1","sequence":1,"type":"reasoning","role":"assistant","status":"completed","content":[{"object":"content","type":"text","delta":false,"index":0,"text":"先确认口径与时间范围…"}],"source_id":"blk_think_1"}

data: {"sequence_number":7,"object":"message","session_id":"ses_1","chat_id":"chat_1","id":"msg_2","sequence":2,"type":"plugin_call","role":"assistant","status":"in_progress","content":[],"source_id":"call_1"}

data: {"sequence_number":8,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"data","delta":true,"msg_id":"msg_2","index":0,"data":{"call_id":"call_1","name":"execute_sql","arguments":""}}

data: {"sequence_number":9,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"data","delta":true,"msg_id":"msg_2","index":0,"data":{"arguments":"{\"sql\":\""}}

data: {"sequence_number":10,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"data","delta":true,"msg_id":"msg_2","index":0,"data":{"arguments":"select 1\"}"}}

data: {"sequence_number":11,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"data","delta":false,"msg_id":"msg_2","index":0,"data":{"call_id":"call_1","name":"execute_sql","arguments":"{\"sql\":\"select 1\"}"}}

data: {"sequence_number":12,"object":"message","session_id":"ses_1","chat_id":"chat_1","id":"msg_2","sequence":2,"type":"plugin_call","role":"assistant","status":"completed","content":[{"object":"content","type":"data","delta":false,"data":{"call_id":"call_1","name":"execute_sql","arguments":"{\"sql\":\"select 1\"}"}}],"source_id":"call_1"}

data: {"sequence_number":13,"object":"message","session_id":"ses_1","chat_id":"chat_1","id":"msg_3","sequence":3,"type":"plugin_call_output","role":"tool","status":"in_progress","content":[],"source_id":"call_1"}

data: {"sequence_number":14,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"data","delta":false,"msg_id":"msg_3","index":0,"data":{"call_id":"call_1","name":"execute_sql","output":""}}

data: {"sequence_number":15,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"data","delta":false,"msg_id":"msg_3","index":0,"data":{"call_id":"call_1","name":"execute_sql","output":"..."}}

data: {"sequence_number":16,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"data","delta":false,"msg_id":"msg_3","index":0,"data":{"call_id":"call_1","name":"execute_sql","output":"...","state":"success"}}

data: {"sequence_number":17,"object":"message","session_id":"ses_1","chat_id":"chat_1","id":"msg_3","sequence":3,"type":"plugin_call_output","role":"tool","status":"completed","content":[{"object":"content","type":"data","delta":false,"data":{"call_id":"call_1","name":"execute_sql","output":"...","state":"success"}}],"source_id":"call_1"}

data: {"sequence_number":18,"object":"biz_event","session_id":"ses_1","chat_id":"chat_1","biz_event":{"event_id":"call_1","seq":3,"channel":"main","block_id":"call_1","status":"done","presentation":{"card_type":"tool","caption":"执行 SQL","body":"..."},"started_at":1722074400.0,"ended_at":1722074405.0}}

data: {"sequence_number":19,"object":"artifact.registered","session_id":"ses_1","chat_id":"chat_1","artifact":{"id":"art_1","session_id":"ses_1","chat_id":"chat_1","name":"gmv_mom.csv","path":"gmv_mom.csv","created_at":"...","updated_at":"..."}}

data: {"sequence_number":20,"object":"segment","session_id":"ses_1","chat_id":"chat_1","segment":{"segment_id":"seg_0001","title":"拉取本月与上月 GMV","input":"本月/上月时间窗","behavior":"执行 SQL 汇总本月与上月 GMV。","conclusion":"本月 GMV 环比 -12%。","artifact":[{"name":"gmv_mom.csv","description":"环比明细","relative_path":"gmv_mom.csv"}],"coverage":{"start_seq":3,"end_seq":3},"started_at":1722074400.0,"ended_at":1722074405.0}}

data: {"sequence_number":21,"object":"message","session_id":"ses_1","chat_id":"chat_1","id":"msg_4","sequence":4,"type":"message","role":"assistant","status":"in_progress","content":[],"source_id":"blk_text_1"}

data: {"sequence_number":22,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"text","delta":true,"msg_id":"msg_4","index":0,"text":"本月 GMV 环比下降约 12%。"}

data: {"sequence_number":23,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"text","delta":true,"msg_id":"msg_4","index":0,"text":"主要来自新客订单减少，详见 gmv_mom.csv。"}

data: {"sequence_number":24,"object":"content","session_id":"ses_1","chat_id":"chat_1","type":"text","delta":false,"msg_id":"msg_4","index":0,"text":"本月 GMV 环比下降约 12%。主要来自新客订单减少，详见 gmv_mom.csv。"}

data: {"sequence_number":25,"object":"message","session_id":"ses_1","chat_id":"chat_1","id":"msg_4","sequence":4,"type":"message","role":"assistant","status":"completed","content":[{"object":"content","type":"text","delta":false,"index":0,"text":"本月 GMV 环比下降约 12%。主要来自新客订单减少，详见 gmv_mom.csv。"}],"source_id":"blk_text_1"}

data: {"sequence_number":26,"object":"followup.generated","session_id":"ses_1","chat_id":"chat_1","followup":{"chat_id":"chat_1","questions":["拆到一级类目看贡献？","对比去年同期？"]}}

data: {"sequence_number":27,"object":"response","session_id":"ses_1","chat_id":"chat_1","id":"response_1","status":"completed","usage":{"input_tokens":1200,"output_tokens":400}}
```


---

## 6. 字段字典

### User

登录用户身份，用于鉴权与资源归属。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| user_id | str | 稳定标识 |
| username | str | 登录名 |
| display_name | str \| null | 展示名 |

### Session

数据会话容器，聚合多轮 Chat。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | `ses_*` |
| owner_user_id | str | 所有者；列表与 ACL 依据 |
| agent_id | str | 绑定的 Agent；用于定位该用户下共享的 MCP、skills 和 workspace。分析会话为业务 Agent（缺省 `default`）；审核会话固定 `review`，工作区与分析会话隔离 |
| title | str | |
| status | enum | `idle` / `running`。由活动 Chat 推导：无活动 Chat → `idle`；有 `status=running` 的 Chat → `running` |
| datasource_id | str \| null | Session 唯一数据源 id；见 `POST /console/chat`。审核会话为 null |
| chat_count | int | |
| channel | str | 当前为 `console` |
| parent_session_id | str \| null | fork 或 review 产生时为源 Session；否则 null。有 parent 不等于 fork，用 `forked_from_chat_id` / `agent_id` 区分 |
| forked_from_chat_id | str \| null | 仅 fork 有切开点 Chat；review 为 null |
| share_scope | enum | `none` / `workspace` / `tenant` / `anyone`。新建 `none`。见 §4.1 |
| enable_clarification | bool | 本 Session 新开 Chat 是否等人回答反问。缺省 `true`。见 §4.3 |
| created_at | datetime | |
| updated_at | datetime | |

### Chat

一轮对话的聚合根：Snapshot 嵌套子资源；SSE 增量与 `sequence_number` 均按本 Chat 作用域。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | `chat_*` |
| session_id | str | |
| owner_user_id | str | 与 Session 所有者一致 |
| sequence | int | Session 内轮次号，从 1 递增（与 SSE `sequence_number` 无关） |
| user_input | str | 外层用户问题 |
| datasource_id | str \| null | 本轮数据源 id；与 `Session.datasource_id` 相同 |
| kind | enum | `simple` / `planned` |
| status | enum | `created` / `running` / `completed` / `failed` / `canceled`。等待 clarification 或工具确认时仍为 `running` |
| forkable | bool | 该 Chat 是否为可用的 fork 切开点；仅 `completed` 且已保存 Agent 状态时为 true。Session 处于 `running` 时仍不可发起 fork |
| last_sequence_number | int | 本 Chat 已分配的最大 SSE `sequence_number`；新建为 `-1`（尚无事件）。供打开页与确认后续订：`after_sequence_number` 取该值，只收投影之后的增量 |
| started_at | datetime \| null | 开始执行时间 |
| completed_at | datetime \| null | 进入终态时间（`completed` / `failed` / `canceled`） |
| active_duration_ms | int | 有效处理时长 |
| error | ChatError \| null | |
| plan | Plan \| null | 本轮 Plan 投影；无任务时为 null |
| segments | Segment[] | 本轮 Segment |
| biz_events | BizEvent[] | 本轮业务轨迹；进 snapshot |
| artifacts | Artifact[] | 本轮 Agent 写出的文件索引（不含用户上传）；正文经 `/files/access?path=` 读取 |
| followup | FollowUp \| null | 本轮最终完成后的推荐追问 |
| artifact_comments | ArtifactComment[] | 本轮开聊时提交的成果行评；无则为 `[]` |
| enable_clarification | bool | 本轮是否等人回答反问。开轮时从 Session 复制。见 §4.3 |

嵌套子对象在聚合内可省略与父级重复的 `chat_id`；出现在独立事件或跨 Chat 列表中时须带 `chat_id`。

主对话顺序：`user_input` 置顶；Steer 注入的 user Message（`metadata.source`=`steer`）与其余原始轨迹（含 `ask_user_question` 工具消息）一并由 `GET .../trace` / SSE 按序渲染，不进 snapshot；`segments` 按 `coverage.start_seq` 排序；`biz_events` 按 `seq` 排序，并用 Segment `coverage` 区间折叠。原始轨迹经 `GET .../trace` 按 `BizEvent.block_id ↔ Message.source_id` 或整 Chat `messages` 关联；工具对（调用卡/结果卡）以 `block_id` 同值配对，“正在执行”由“有调用卡、同 `block_id` 无结果卡”判定。所有 `ask_user_question` 调用及其 tool result 都实时发布，包括参数校验失败的调用与错误结果。待回答的 `ask_user_question` 指：`status=running` 的 Chat 中，存在 `name=ask_user_question` 的 `plugin_call`，且该 `call_id` 尚无对应的最终语义 tool result（`answered`）；此时前端按 `Chat.enable_clarification` 渲染确认卡片，`false` 时由 Host 自动作答（`call_id` 即 `clarification_id`）。`FunctionCall.arguments` 原样保留 LLM tool arguments，提示截止时间位于 Message `metadata.expires_at`。`followup` 与操作栏挂在本轮终态之后。

#### ChatError

Chat 运行失败。`Chat.error` 与 SSE `response`（`status`=`failed`）的 `error` 同形。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| code | enum | 见下表 |
| message | str | 面向用户的说明 |
| details | object \| null | `cause` 为原始异常文案 |

| code | message |
| --- | --- |
| `MODEL_AUTH` | 模型密钥无效或没有权限，请到智能体配置里检查 API Key。 |
| `MODEL_QUOTA` | 模型额度不足或已欠费，请检查账号配额后重试。 |
| `MODEL_RATE_LIMIT` | 调用太频繁，请稍后再试。 |
| `MODEL_NOT_FOUND` | 当前模型不可用，请在配置里换一个模型。 |
| `CONTEXT_OVERFLOW` | 这次对话太长，超出模型上下文。请新开会话，或缩短输入后再试。 |
| `REACT_MAX_ITERS` | 已达到最大 ReAct 循环次数，本轮执行已停止。请在智能体配置中调整循环上限后重试。 |
| `MODEL_UNAVAILABLE` | 模型服务暂时不可用，请稍后重试。 |
| `CONFIG` | 模型还没配置完整，请先到智能体配置里补齐。 |
| `INTERNAL` | 执行失败，请稍后重试。 |
| `VALIDATION` | 执行失败，请稍后重试。 |

`ReplyEnd.finished_reason` 为 `exceed_max_iters` 时，本轮状态为 `failed`。Host 先保存 `Chat.error`，再发送同形的 `response.failed.error`。前端用回复失败卡片展示。

### AskUserQuestionInput

`ask_user_question` 工具调用的参数载荷。它同时是 LLM tool input 与前端卡片数据：Host 不转换、不补字段；前端从 `FunctionCall.arguments` 原样读取。


| 字段        | 类型                        | 说明                |
| --------- | ------------------------- | ----------------- |
| title     | str                       | 卡片标题              |
| questions | AskUserQuestionQuestion[] | 题目列表；数量必须为 `1..4` |


`expires_at` 不在 arguments 中。Host 将其写入承载该 `plugin_call` 的 Message `metadata.expires_at`；前端按该提示时间决定何时提交 timeout，Host 不以该时间拒绝 answered。

#### AskUserQuestionQuestion

`ask_user_question` 中的单道题目。


| 字段          | 类型                      | 说明                     |
| ----------- | ----------------------- | ---------------------- |
| question    | str                     | 题目正文                   |
| description | str                     | 题目说明                   |
| multiSelect | bool                    | `false` 为单选，`true` 为多选 |
| options     | AskUserQuestionOption[] | 选项；数量必须为 `2..4`        |


`required` 与 `allow_custom_text` 是前端交互策略，不属于 Host/LLM 传输字段：

- `required`：该题是否必须得到回答，才允许前端提交整张卡片；为 `false` 时用户可跳过该题。
- `allow_custom_text`：除预设 options 外，前端是否允许用户输入自由文本；为 `true` 时前端负责把该文本整理进最终语义 `answer`。

它们的取值、默认值和交互表现由前端产品逻辑决定，LLM 不能声明，Host 不补充也不校验。

如果解析错误，那么预期行为是sse还会发送错误的tool result，此时对该tool不做处理。

#### AskUserQuestionOption

题目下的可选答案项。


| 字段          | 类型  | 说明     |
| ----------- | --- | ------ |
| label       | str | 选项展示文案 |
| description | str | null   |


#### AskUserQuestionToolResult

前端提交的确认结果。`status`=`answered` 时，Host 绑定 `clarification_id` 后原样回填给 LLM；`status`=`timeout` 时取消当前 Chat。

`answered` 字段：


| 字段                         | 类型                              | 说明                                 |
| -------------------------- | ------------------------------- | ---------------------------------- |
| status                     | `"answered"`                    | 正常作答固定值                            |
| answers                    | AskUserQuestionSemanticAnswer[] | 按原 questions 顺序排列的语义答案             |
| answers[].question         | str                             | 原题正文，原样复制对应 `questions[].question` |
| answers[].selected_options | str[]                           | 选中的 option label；未选预设项时为空数组        |
| answers[].custom_text      | str | null                      | 自由输入或对选项的补充；没有时为 null，不与选项拼接       |


复合示例（依次为单选、多选加补充、纯自由文本）：

```json
{
  "status": "answered",
  "answers": [
    {
      "question": "你希望用哪个指标？",
      "selected_options": ["全站 DAU"],
      "custom_text": null
    },
    {
      "question": "你希望包含哪些渠道？",
      "selected_options": ["App", "Web"],
      "custom_text": "只看自然流量"
    },
    {
      "question": "还有其他分析要求吗？",
      "selected_options": [],
      "custom_text": "不包含投放"
    }
  ]
}
```

`timeout` 字段：


| 字段     | 类型          | 说明                 |
| ------ | ----------- | ------------------ |
| status | `"timeout"` | 超时固定值              |
| reason | str         | 超时说明 |


Host 收到 timeout 后停止后续 ReAct，将当前 Chat 置为 `canceled`。

### Plan

Chat 级 Plan；挂在 `Chat.plan` / snapshot `chats[].plan`。无任务时为 null。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| tasks | Task[] | 完整任务列表 |

#### Task

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | 任务标识 |
| subject | str | 任务标题 |
| description | str | 任务说明 |
| metadata | object | 元数据；无固定键 |
| created_at | str | ISO 格式创建时间 |
| state | enum | `pending` / `in_progress` / `completed` |
| owner | str \| null | 可选负责人 |
| blocks | str[] | 被当前任务阻塞的任务 ID；由 Host 按 `blocked_by` 重建 |
| blocked_by | str[] | 阻塞当前任务的任务 ID |

### Segment

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| segment_id | str | 稳定标识（如 `seg_0001`） |
| chat_id | str | 独立事件中必填；嵌在 Chat 内可省略 |
| title | str | 分段标题 |
| input | str \| null | 关键输入；确无为 null |
| behavior | str | 行为概述 |
| conclusion | str | 结论概括 |
| artifact | SegmentArtifact[] \| null | 关键工件；无则为 null |
| coverage | Coverage | 覆盖的 BizEvent `seq` 闭区间 |
| started_at | float \| null | Unix 秒 |
| ended_at | float \| null | Unix 秒 |

#### Coverage

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| start_seq | int | 首事件 `seq`（含）；分段排序键 |
| end_seq | int | 末事件 `seq`（含） |

#### SegmentArtifact

业务视角工件（Segment 筛选后的子集）。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| name | str | 文件名 |
| description | str | 简述 |
| relative_path | str | 相对 Session 产物目录；正文经 `/files/access?path=` 读取 |

#### SessionSegmentArtifact

`GET /sessions/{session_id}/artifacts` 的业务视角项。`relative_path` 与 `SegmentArtifact.relative_path` 同根。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| name | str | 文件名；取该路径首次出现的值 |
| description | str | 简述；取该路径首次出现的值 |
| relative_path | str | 相对 Session 产物目录 |
| chat_id | str | 首次出现的轮次 |
| segments | SessionSegmentRef[] | 引用过该路径的分段，按扫描顺序 |

#### SessionSegmentRef

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| segment_id | str | |
| chat_id | str | |
| title | str | 分段标题 |

### Message（原始轨迹）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | Host 分配；Chat 内唯一，作主键 |
| object | str | 流式时固定 `message` |
| chat_id | str | Host 注入 |
| sequence | int | Chat 内单调序 |
| type | enum | 见 Message.type |
| role | enum \| null | `user` / `assistant` / `system` / `tool` |
| content | Content[] | 内容块列表 |
| status | enum | `created` / `in_progress` / `completed` / `failed` / `cancelled` |
| source_id | str \| null | 对齐上游：文本 / reasoning / 媒体块为 AgentScope `block_id`；`plugin_call` 与 `plugin_call_output` 均为同一 `tool_call_id`（配对用，不等于 `id`）。无上游时省略 |
| metadata | object \| null | 可选。已知键见 Message.metadata |

一条 AgentScope 文本 / thinking / 媒体 block 对应一条 Message（一 block 一 message）。Steer 在 Runtime 使用 AgentScope `HintBlock`；经 Formatter 转为通用 `role=user` 消息后，按本表投影为一条 Message 进入 SSE / trace。

#### Message.metadata

挂在对应 Message 上，随 SSE / `GET .../trace` 发出。

##### 工具确认

需要人确认的 `plugin_call`。不需要确认的 `plugin_call` 无本组字段。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| awaiting_confirmation | bool | 固定 `true` |
| expires_at | str | ISO 截止时间 |
| reason | str | `tool_confirmation:off` / `tool_confirmation:auto` / `tool_confirmation:all` |

##### 反问

`ask_user_question` 的 `plugin_call`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| expires_at | str | ISO 截止时间 |

##### Steer

`type`=`hint` 且来自 Steer 的 Message。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| source | str | 固定 `steer` |
| artifact_comments | ArtifactComment[] | 可选；本条引导的成果行评 |

#### Message.type

Message 的类型枚举及含义。

| type | 含义 |
| --- | --- |
| `message` | 普通助手/用户消息 |
| `hint` | AgentScope HintBlock 投影（含 Steer） |
| `reasoning` | Agent thinking |
| `plugin_call` | 工具/插件调用 |
| `plugin_call_output` | 工具/插件返回 |
| `mcp_tool_call` | MCP 工具调用 |
| `mcp_tool_call_output` | MCP 工具返回 |
| `progress` | 进度 |
| `result` | 结果 |

#### Content

Message 内的单个内容块（文本、媒体或工具载荷）。

公共字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| object | str | 流式时固定 `content` |
| type | enum | `text` / `image` / `audio` / `video` / `data` / `file` / `refusal` |
| delta | bool | `true` 增量，`false` 完整值；见 §5.2 |
| index | int \| null | 同一 Message 内序号 |
| status | enum \| null | 块级运行态 |
| msg_id | str \| null | 所属 Message.id；SSE 顶层必填 |

按 `type` 的专有字段（`null` 字段可不传）：

| type | 专有字段 |
| --- | --- |
| `text` | `text` |
| `image` | `image_url` |
| `audio` | `data`、`format` |
| `video` | `video_url` |
| `file` | `filename`、`file_url` |
| `data` | `data`（工具消息时为 FunctionCall / FunctionCallOutput） |
| `refusal` | `refusal` |

#### FunctionCall

`plugin_call` 的 `Content.data`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| call_id | str \| null | 与 Output 配对 |
| name | str \| null | 工具名 |
| arguments | str \| null | 参数 JSON 字符串 |

#### FunctionCallOutput

`plugin_call_output` 的 `Content.data`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| call_id | str \| null | 对应 FunctionCall.call_id |
| name | str \| null | 工具名 |
| output | str \| null | 返回内容 |
| state | str \| null | 可选，如 `success` / `error` |

### BizEvent（业务轨迹）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| event_id | str | upsert 键；算法侧生成、会话内唯一；重复到达仅发生在会话结束时的补写（未收到结果的调用卡改写为 error） |
| chat_id | str | 嵌在 Chat 内可省略 |
| seq | int | 卡片排序；与 Segment `coverage` 对齐 |
| channel | enum | `main` / `subagent`；本版前端仅消费 `main` |
| block_id | str \| null | 原始轨迹块级对齐键，与 `Message.source_id` 同值：文本/思考/hint 为 agentscope 原生 `block_id`，工具对（调用卡与结果卡）同为 `tool_call_id`；无对应块时 null |
| status | enum | `done` / `error`（一次写定，无 running；“正在执行”由前端以“有调用卡、同 block_id 无结果卡”判定） |
| presentation | Presentation \| null | 呈现块；可读内容一律由此承载 |
| started_at | float | Unix 秒 |
| ended_at | float \| null | 完成时间 |

#### Presentation

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| card_type | str | `user` / `thinking` / `text` / `hint` / `tool` |
| caption | str | 小标题 |
| body | str | 内容（markdown） |

### Artifact

Agent 视角的薄索引：工具成功后 diff Session 产物目录，新增/改写各登记一条（SSE `artifact.registered`）。同 Chat 同 `path` 覆盖更新；内容始终以磁盘最新为准，经 `/files/access?path=` 读取。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | `art_*` |
| session_id | str | |
| chat_id | str | 独立事件或跨 Chat 列表中必填；嵌在 Chat 内可省略 |
| name | str | 文件名 |
| path | str | 相对 Session 产物目录；与 SegmentArtifact.`relative_path` 同根 |
| created_at | datetime | |
| updated_at | datetime | |

#### ArtifactLineRef

行级评论锚定。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| artifact_id | str | |
| content_hash | str | 评论时对正文现算 |
| line_start | int | 起始行（含） |
| line_end | int | 结束行（含） |
| quote | str | 选中原文 |

#### ArtifactComment

开聊时提交的成果行评，挂在该轮 Chat 上。与 `ArtifactLineRef` 无关。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| path | str | 相对 Session 产物目录 |
| line_start | int | 起始行（含） |
| line_end | int | 结束行（含） |
| comment | str | 用户写的评论 |

### AttachmentRef

用户上传附件的引用，供开聊请求的 `attachments` 携带。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| attachment_id | str | |
| filename | str | |
| mime_type | str | |
| size_bytes | int | |

### Feedback

用户对 Chat 或成果的反馈（点赞、点踩、复制、行评等）；默认不进 snapshot，经 REST 写入。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | `fbk_*` |
| owner_user_id | str | 提交反馈的用户 |
| session_id | str | |
| chat_id | str | |
| kind | enum | `like` / `dislike` / `copy` / `artifact_comment` |
| reason | str \| null | |
| detail | str \| null | |
| artifact_ref | ArtifactLineRef \| null | |
| created_at | datetime | |

### FollowUp

Chat 完成后的推荐追问列表，供填入输入框。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| chat_id | str | 独立事件中必填；嵌在 Chat 内可省略 |
| questions | str[] | 2～3 条 |

### DatasourceOption

数据源选择器中的可选数据源项。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | |
| name | str | |
| status | str | |
| description | str | |
| recommended | bool | |

### Provider

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | |
| name | str | |
| chat_model | str | |
| api_key_prefix | str | 输入提示用前缀 |
| catalog_base_url | str | Provider 默认地址 |
| base_url | str | 当前生效地址 |
| configured | bool | 是否已配置凭证 |
| api_key_masked | str | 脱敏预览；未配置为 `""` |
| models | ProviderModel[] | |

### ProviderModel

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | |
| name | str | |
| source | enum | `catalog` / `extra` / `override` |
| context_size | int \| null | |
| thinking_enabled | bool \| null | |
| generate_kwargs | object | |
| chat_model | str \| null | |

### ActiveModels

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| default_provider_id | str \| null | |
| default_model_id | str \| null | |
| light_provider_id | str \| null | |
| light_model_id | str \| null | |

### RuntimeSettings

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| react_max_iters | int | |
| llm_retry_enabled | bool | |
| llm_max_retries | int | |
| context_trigger_ratio | float | |
| context_reserve_ratio | float | |
| tool_result_limit | int | |
| tool_timeout_enabled | bool | 是否启用单次工具执行超时 |
| tool_timeout_seconds | float | 单次工具执行上限（秒），最小 1 |
| tool_confirmation_level | enum | `off` / `auto` / `all`。缺省 `auto`。见 §4.3 |

### AgentSkill

Agent `workspace/skills` 中已安装的一项。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| name | str | |
| enabled | bool | 软开关；`false` 时不进入运行时有效 skill 路径 |
| source | enum | `builtin` / `custom`；`builtin` 人工不可编辑 |
| group | str | 分组；builtin 取源码一级目录名，custom 为 `""` |
| description | str | 来自 `SKILL.md` frontmatter |

### AgentSkillDetail

`AgentSkill` 全部字段，另加正文。仅详情 GET 返回，不出现在列表里。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| content | str | `SKILL.md` 原文（含 YAML frontmatter） |

### SkillRevision

一次写入留下的版本记录（append-only）。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| revision_no | int | 从 0 递增，按 skill 独立编号 |
| origin | enum | `create` / `install` / `import` / `manual_edit` / `evolution_accept` / `revert` |
| source_ref | str | 按 origin 取值：演进 proposal id、被回滚版本的 id 等；否则 `""` |
| note | str | 说明文本，可为 `""` |
| created_at | datetime | |

### SkillRevisionDetail

`SkillRevision` 全部字段，另加文件快照。仅详情 GET 返回。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| files | {relative_path, content}[] | 该版本整个 skill 目录的文本文件；`relative_path` 相对 skill 根 |

### SkillDraft

会话级 Skill 草稿。定稿前只落 `skill_drafts` 表；`name` / `agent_id` 不存（后者在 Session 上）。列表接口不含 `files`；详情 / 创建 / 改稿 / 确认 / 丢弃含 `files`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | `draft_*` |
| session_id | str | |
| source_chat_id | str | 触发时的源 Chat（须已终态） |
| status | enum | `generating` / `ready` / `revising` / `accepted` / `dismissed` / `failed` |
| name | str | 从 `files["SKILL.md"]` frontmatter 现算；生成前为 `""` |
| files | object \| 缺省 | 相对路径 → 正文，如 `{"SKILL.md": "...", "scripts/x.py": "..."}`。仅详情下发；列表省略 |
| created_at | datetime | |
| updated_at | datetime | |

### CronJob

定时任务定义（落盘）；归属当前登录 identity，列表与读写均按身份过滤。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | `cron_*` |
| name | str | 展示名；到点新建 Session 时可用作 title |
| enabled | bool | `false` 时到点不触发（pause） |
| message | str | 到点写入新 Chat 的 `user_input` |
| datasource_id | str | 必填；到点绑定到新 Session/Chat |
| schedule | ScheduleSpec | 见下 |
| created_at | datetime | |
| updated_at | datetime | |

### CronJobWrite

创建 / 全量替换请求体。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| name | str | 非空（trim 后） |
| enabled | bool | 默认 `true` |
| message | str | 非空（trim 后） |
| datasource_id | str | 非空（trim 后） |
| schedule | ScheduleSpec | |

### ScheduleSpec

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| type | enum | `cron`（默认）/ `once` |
| cron | str \| null | `type=cron` 时必填：5 段 crontab；Host 可能改写第 5 段星期为 `sun`…`sat`；`type=once` 时为 null |
| run_at | datetime \| null | `type=once` 时必填；`type=cron` 时为 null |
| timezone | str | 默认 `Asia/Shanghai`；Host 按此时区解释 cron / `run_at` |

### UsageWindow

我的用量查询时间窗。四个接口同形。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| period | enum | `1d` / `7d` / `30d` |
| start | datetime | UTC；含 |
| end | datetime | UTC；不含 |
| timezone | str | 分桶用的 IANA |
| as_of | datetime | UTC 快照时刻 |

### GenerationUsage

生成模型（`model_type=text`）汇总。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| input_tokens | int | |
| output_tokens | int | |
| total_tokens | int | `input_tokens + output_tokens` |
| calls | int | 物理 provider 调用次数 |

### EmbeddingUsage

Embedding（`model_type=embedding`）汇总。DashScope 兼容接口 usage 为 `prompt_tokens` / `total_tokens`（二者均为输入 Token，无 `completion_tokens`）；计入 `tokens`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| tokens | int | 输入 Token |
| calls | int | 物理 provider 调用次数 |

### UsageModel

窗口内实际用过的模型。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| provider_id | str | |
| model_id | str | |
| model_type | enum | `text` / `embedding` |

### UsagePagination

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| page | int | |
| page_size | int | |
| total | int | 聚合后的行数 |
| page_count | int | |
| has_previous | bool | |
| has_next | bool | |

### UsageTimeseriesPoint

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| bucket_start | datetime | UTC；含 |
| bucket_end | datetime | UTC；不含 |
| label | str | 前端直接展示，如 `09/14` 或 `16:00` |
| is_partial | bool | 当前未结束的桶为 `true` |
| generation | GenerationUsage | |
| embedding | EmbeddingUsage | |

### SessionUsageItem

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| session_id | str | |
| title | str | |
| session_state | enum | `active` / `deleted` |
| last_activity_at | datetime | 窗口内最近一次用量 |
| models | UsageModel[] | 去重 |
| generation | GenerationUsage | |
| embedding | EmbeddingUsage | |
| total_tokens | int | `generation.total_tokens + embedding.tokens` |

### BackgroundTaskUsageItem

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| group_id | str | 由 `source` 计算，稳定，非表主键 |
| source | str | 写入时的业务来源，原样返回 |
| last_activity_at | datetime | |
| models | UsageModel[] | 去重 |
| generation | GenerationUsage | |
| embedding | EmbeddingUsage | |
| total_tokens | int | `generation.total_tokens + embedding.tokens` |

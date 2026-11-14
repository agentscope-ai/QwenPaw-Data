# Model

`context_manager.model` 负责模型接入：文本生成、批量 embedding、用户模型选择、Host Provider 凭据解析，以及模型 client 的创建、复用和关闭。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `llm.py` | 定义 `ChatMessage`、`LlmCaller` 和 `LlmError`，实现文本生成 client `OpenAILlmCaller`。 |
| `embedding.py` | 定义 `TextEmbedder`，实现 `OpenAIEmbedCaller` 的向量请求、拆批、并发、排序和维度校验。 |
| `embedding_cache.py` | 按可信调用身份、向量空间与实发文本维护进程级 TTL/LRU 缓存。 |
| `repository.py` | `ModelConfigRepository` 封装配置、Provider 引用及工作区约定的 SQL 读写与事务，读取用户所属的 Host Provider 记录；独立的 `ModelUsageRepository` 批量幂等写入用量事件并检查 Host 表依赖。 |
| `service.py` | `ModelService` 统一配置查看、保存、清空、测连、Provider 凭据解析和工作区约定校验；创建操作级客户端并负责关闭。`ResolvedModel` 表达解析后的配置。 |
| `usage.py` | 定义不可变用量事件和来源上下文；`ModelUsageTracker` 固定调用快照、归一化 SDK usage，并通过回调提交事件。 |
| `usage_recorder.py` | `ModelUsageRecorder` 管理有界队列、批量刷新、有限重试、丢弃计数与限时停止。 |

`LlmCaller` 表达文本生成 client，`TextEmbedder` 表达向量生成 client；两者都声明调用能力与 transport 关闭契约。`OpenAILlmCaller`、`OpenAIEmbedCaller` 是默认实现，后者额外公开向量维度和无凭据配置快照。`ModelService` 的异步上下文管理器负责客户端的创建与关闭，业务操作决定复用范围。

`profiling.cm_adapter` 直接从 `model` 引用 `ChatMessage`，将 profiling 的双 prompt 接口适配到本次操作的 LLM client，并保留 profiling 的响应缓存。

## 配置管理

Runtime 按认证身份装配 ModelService，Repository 借用共享 CMDatabase 读取 SQL 配置和 Host Provider。
CM 保存 Provider 引用、模型选择与维度，API Key 从该用户的 Host Provider 解析。
仅当该通道完全没有 Provider 引用时，读取旧 `cm_user_model_config` 中仍存在的
加密 Key；新库无需旧 Key 列。两种来源共用地址、解密和 Workspace 向量空间校验，
配置状态与实际调用使用同一解析规则，失败原因通过 `errors` 按通道返回。
已绑定但被删除或失效的 Provider 必须报错，不回退旧 Key。
旧 Embedding 配置仍要求正整数维度；缺失时明确报错，不替换或推测维度。
新保存及测连仍要求 Provider；保存时在同一事务清除该通道旧 Key。
Host 删除 Provider 保留 CM 引用，CM 删除配置则同时删除配置行和引用；没有单独解绑接口。

- `read()` 返回掩码、可用状态和工作区匹配状态。
- `save()` 在同一 SQL 事务保存选择、引用与工作区约定。
- `probe()` 使用候选配置发出测试请求，结束即关闭客户端。
- `delete()` 清空本人选择和引用，保留 Host Provider 与工作区约定。

`cm_workspace_embedding_space` 按 tenant/workspace 保存模型、Provider 实际地址和维度，指纹即时计算。
首次保存 embedding 认领约定；普通用户后续保存须匹配，管理员可更新约定。
用户选择按 tenant/user 保存，切换 Workspace 后可能需要重新对齐。
配置一致性与已有图向量的重建状态分别判断；历史向量通过 [Embedding](../embedding/README.md) 显式重建。

## 一次操作内复用配置

```python
models = runtime.models(authorization)
async with models.open() as (llm, embedder):
    first = await embedder.embed(first_texts)
    answer = await llm.complete(messages)
    second = await embedder.embed(second_texts)
```

`open()` 读取配置并创建两个客户端，`llm()`、`embedder()` 只读取各自 channel。连接凭据、模型默认值和 embedding 默认维度在 client 构造时确定；配置变更影响下一次操作。后台任务在实际执行时创建 client；SSE 持有 client 直到流结束。重建等需要在受理时固定配置的入口可通过 `resolve_for_use(channel)` 取得 `ResolvedModel`，再显式传给 `llm(config)` 或 `embedder(config)`。

`OpenAIEmbedCaller.dimensions` 提供默认维度，`config_snapshot` 返回独立的公开字典，仅包含 `model`、`base_url`、`dim`。重建 Service 先解析并保存公开配置，在任务实际执行时据此创建 client，整次重建使用同一模型和维度。

退出异步上下文时释放本次操作的 client，覆盖成功、异常和取消。Service 与 Repository 是借用共享数据库的轻量对象。Runtime 关闭时先收尾模块后台任务，再释放共享资源。直接创建的 client 支持 `async with`；注入的 SDK client 由注入方关闭。

## 用量计量

`ModelService` 使用绑定的 tenant/workspace/user 和本次解析的 Provider 创建跟踪器。客户端在 SDK 返回响应后、业务结果校验前记录用量；来源上下文由协议和业务入口绑定。

Runtime 装配一份记录器并管理启动和停止，向模型工厂传递 `enqueue` 回调。记录器通过独立的 `ModelUsageRepository` 借用共享 SQL 连接池，以独立事务写入 Host 管理的 `model_usages` 表。入队和持久化允许失败，计量失败不改变模型结果。
旧 Key 配置的 `provider_id` 为 `null`；在切换到 Provider 前不生成要求 Provider 归属的用量事件。

## HTTP 配置

- `LLM_HTTP_TIMEOUT` 同时配置 LLM 和 embedding client 的 HTTP 超时（秒），默认 `300`；空值或无效数字回退到默认值，非正数使用 SDK 默认超时。构造时显式传入的 `timeout_seconds` 优先。
- `LLM_HTTP_IGNORE_PROXY` 默认 `0`。去除首尾空白、忽略大小写后，空值、`0`、`false`、`no`、`off` 关闭开关，其余值让 LLM 专用 HTTP client 使用 `trust_env=False`。Embedding client 保持环境代理行为。

部署配置统一由 `settings/model.py` 读取；创建 client 时使用 `get_settings().model` 的进程配置快照，适用于 Runtime 模型快照和配置页面的连接测试。自建 HTTP client 随 SDK client 一并关闭；注入的 SDK client 保持调用方设置和生命周期。

`LlmCaller.complete(timeout_seconds=...)` 可为单次请求指定正数超时，随后调用继续使用 client 默认值。`process.complete_json` 将该参数传给每次 JSON 请求；KG 的独立超时配置由 `settings/knowledge.py` 声明，`Settings` 根据模型默认超时补齐其缺省值。

## 有界批量 embedding

`OpenAIEmbedCaller` 在创建时使用 `get_settings().embedding` 的以下部署配置；显式构造参数 `batch_size`、`max_concurrency` 优先于对应环境变量。

| 环境变量 | 默认值 | 行为 |
| --- | --- | --- |
| `EMBED_API_BATCH` | `10` | 每批文本数量，最小 `1`。 |
| `EMBED_API_CONCURRENCY` | `24` | 每次 `embed()` 调用的并发请求数，最小 `1`。 |
| `EMBED_API_MAX_RETRIES` | `5` | 限流后的外层重试次数，最小 `0`。 |
| `EMBED_API_MAX_INPUT_CHARS` | `8000` | 实发文本的字符上限，最小 `100`。 |
| `CM_EMBED_CACHE_TTL_SEC` | `600` | 缓存有效期（秒）；非正数关闭缓存。 |
| `EMBED_CACHE_MAX` | `2048` | 进程级缓存条目上限，最小 `1`，按 LRU 淘汰。 |

整数参数使用 `int` 解析，TTL 使用 `float` 解析；空值与非数字会导致配置错误。环境变量中的低于下限值按表中下限处理。

输入统一校验后截断发送文本，原始节点属性及 Process 生成的文本/hash 保持原值。缓存按 `(identity, endpoint, model, dimension, clipped_text)` 区分调用身份、向量空间与输入；`identity` 是由 `cache_namespace`（tenant/workspace/user）与 API Key 计算的进程内 HMAC，未提供 `cache_namespace` 或注入 SDK client 时只在该 client 内复用；身份与空间均相同的 client 可复用不可变向量。缓存有效期从写入时计时，访问只更新 LRU 顺序。启用时只对未命中的实发文本去重、拆批；关闭时按原输入逐条拆批请求。缓存是可丢弃的进程内加速数据，配置 Service 在操作开始时检查用户配置和工作区向量空间。

SDK 默认重试 `1` 次，可通过构造参数 `max_retries` 显式覆盖；外层限流重试由 `EMBED_API_MAX_RETRIES` 控制，退避等待为 `0.5 × 2^attempt + [0, 0.25]` 秒。错误消息中的 `429`、`rate`、`limit_burst_rate` 及 HTTP 429 状态触发这层重试，其余请求失败直接上抛。

返回向量与输入顺序一一对应。每个批次及整次调用校验向量数量、索引、有限数值和维度；任一批失败或调用取消时，取消并等待其余请求。全部缓存缺失请求成功并完成校验后，再发布缓存结果。

网络请求及完整响应体读取保持异步；SDK JSON/base64 解码、结果排序和向量校验合并为一次同步处理，通过构造时注入的 `run_sync` 执行。Runtime 为后台业务注入共享的 `background.run_sync`，在已准入工作单元内处理响应；前台模型调用注入 `graph_read.run`，独立申请前台额度。取消时等待已运行的响应处理结束后再释放额度和客户端。完整执行规则见 [Runtime](../runtime/README.md#执行池)。独立使用且未注入执行入口时，默认使用 `asyncio.to_thread`，不具有 Runtime 的共享准入保证；自定义模型工厂负责自己的执行资源。

## 与图处理的协作

- `process/embedding.py` 的 `EmbeddingEnricher` 根据 Schema 生成节点文本、输入 hash，并回填 Draft；注入的 embedder 执行模型请求。
- 顶层 `embedding/` 负责图节点扫描、重建任务状态和事务写回。
- API 负责认证用户选择与 HTTP 契约，Runtime 按需装配 ModelService，业务操作决定客户端的使用范围。
- `ResolvedModel.api_key` 从 `repr` 中隐藏，配置接口返回掩码，模型错误使用清洗后的文案。

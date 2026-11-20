# Process

`context_manager.process` 提供 MG/KG/UG 共用的分块、抽取、候选解析和向量准备能力，消费 Store 的 Draft/Record。各业务模块提供 prompt、合并规则和受 Scope 约束的候选查询。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `chunker.py` | `Chunker` 绑定切分尺寸、重叠和输出上限，生成带稳定字符偏移的 `TextChunk`。 |
| `extractor.py` | `ChunkExtractor` 定义单 Chunk 抽取协议，`BatchExtractor` 绑定并发上限并保持结果顺序。 |
| `judge.py` | 定义 `SemanticJudge` 和结构化同一性判定。 |
| `output.py` | `complete_json` 调用模型并解析 JSON object，按调用方提供的 JSON Schema 校验输出，在指定次数内重试格式错误。 |
| `deduplicator.py` | 仅定义 `Sequence[NodeDraft] -> Sequence[NodeDraft]` 的 `Deduplicator` 协议；具体去重语义由业务图实现。 |
| `embedding.py` | `build_embedding_text` 按 Schema 统一投影草稿与既有节点文本；`EmbeddingEnricher` 复用该投影，调用 embedder 并回填 `embedding`、`embedding_hash`。 |
| `search.py` | 定义既有节点召回协议及 subject-candidate 对应的 `ScoredCandidate`。 |
| `resolver.py` | `Resolver` 把一个 `NodeDraft` 解析为 new、ambiguous 或既有 `NodeRecord`。 |
| `__init__.py` | 暴露稳定 package API。 |

## 依赖方向

```text
Knowledge/Metadata/User application -> Process
                                          -> Store.NodeDraft/EdgeDraft/NodeRecord
                                          -> Resolver/EmbeddingEnricher -> LLM.TextEmbedder
                                          -> CandidateSearch -> Store.NodeRecord
```

Store 以 `NodeSpec`/`EdgeSpec` 约束 `NodeDraft`/`EdgeDraft`，Process 的 Deduplicator 协议和 Resolver 直接消费这些数据库无关协议；Resolver 的既有候选是 Store `NodeRecord`。业务实现负责 exact matching、语义判断、合并策略、prompt、Scope 查询和响应的业务校验。`build_embedding_text(spec, properties)` 根据 `NodeSpec.embedding_fields` 统一生成 embedding 输入，接受 Draft 或 Store Record 的属性。Knowledge 的批内去重、Resolver 召回与候选补算，以及 Enricher 的持久化/重建向量都复用这一投影。

`complete_json(caller, messages, schema=None, attempts=1, max_tokens=None)` 统一使用 JSON mode，解析纯 JSON 或 fenced JSON object。提供 `schema` 时，将其加入 system prompt 并校验结果；格式或 Schema 不匹配按 `attempts` 重试，模型访问异常直接交给调用方。Knowledge 使用单次调用，保留原有 token 上限及 Draft/Pydantic 业务校验；User 各阶段提供响应 Schema，最多尝试两次。业务图负责来源证据、候选身份和关系约束，并将异常转换为自己的错误类型。

`EmbeddingEnricher` 接收调用方注入的 `TextEmbedder`，把本次有效节点文本统一交给 `embed(texts)`。模型接入层负责 provider、model、dimensions、请求拆批和并发控制；Enricher 负责节点文本准备、输入 hash 和 Draft 回填。

`prepare(nodes)` 和 `apply(prepared, pending, vectors)` 分别提供同步的文本准备与结果校验、回填步骤；`enrich(nodes)` 复用这两步及异步模型调用。需要执行资源隔离的业务可将这两个完整步骤提交到自己的执行函数，Process 不接收池或调度参数。

需要跨多次模型调用保持一致的 application，通过 `ModelService` 的异步上下文取得固定配置的客户端，在完整操作内复用，退出上下文时关闭。算法接收调用方注入的客户端。

文本按 `embedding_fields` 的声明顺序组成紧凑 JSON，保留字段名和文本列表的元素边界；文本去除首尾空白，忽略缺失、`None` 和空白内容。`embedding_hash` 为实际输入文本 UTF-8 字节的 SHA-256。每次调用都会重新生成所选节点的向量：hash 表达输入内容的一致性，模型切换后的重算由调用方发起。

## 输入与结果

- Draft 的 `ref` 和边端点引用用于本次计划，`transient` 保存算法状态；`output()` 投影 canonical 属性。
  Store 创建节点后返回有序 key，事务计划将 ref 映射为 key。
- Chunk 偏移对应输入原文；BatchExtractor 保持结果顺序，任一抽取失败向调用方传播。
- Enricher 保留输入顺序和原 Draft 的其他字段；空 `embedding_fields` 原样返回，配置了字段但文本为空时清除向量及 hash。
- 向量与输入逐项对应，数值有限且维度一致；失败保留原 Draft，由模型层收尾并发请求。
- Deduplicator 接收并返回 Draft 序列；Resolver 使用注入的 CandidateSearch 和 SemanticJudge 返回 new、ambiguous 或既有 Record。
- ScoredCandidate 表示一次 subject/candidate 配对的得分，候选缓存限定在本次请求。

## 使用方式

调用方在自己的 application service 生命周期中构造编排器：

```python
chunker = Chunker(max_chars=4000, overlap_chars=400, max_chunks=64)
extractor = BatchExtractor(domain_chunk_extractor, max_concurrency=4)
deduplicator: Deduplicator = domain_deduplicator
async with models.embedder() as embedder:
    enricher = EmbeddingEnricher(embedder)
    resolver = Resolver(
        embedder=embedder,
        build_semantic_text=lambda node: build_embedding_text(
            node.spec, node.properties
        ),
        candidate_search=request_scoped_search,
        identity_judge=domain_resolution_judge,
    )
    enriched_nodes = await enricher.enrich(node_drafts)
```

`Deduplicator.deduplicate()` 的所有算法参数由业务实现封装。`Resolver.resolve()` 的召回数量、发送给 LLM 的候选数量及最低召回分数提供内部默认值；它们是应用层调优参数，不应直接扩散成 HTTP 用户参数。

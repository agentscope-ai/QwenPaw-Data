"""Develop Graph Center tool-calling loop, adapted to scoped Store queries."""

from __future__ import annotations

import json
import logging
import time
from contextlib import nullcontext
from dataclasses import replace
from typing import Any, AsyncIterator, Optional

from openai import AsyncOpenAI

from ...executor import BoundedExecutor
from ...model import ModelUsageTracker, bind_usage_context, current_usage_context
from ...store import StoreError
from .access import ExplorerAccess
from .graph_tools import expand_subgraph, find_anchors

log = logging.getLogger(__name__)


def prepare_exploration(
    service, scope, model_config, query, *, max_rounds=8, lang=None
):
    """Pin user configuration before opening a stream; own the tool transport.

    Generic LlmCaller is text-only. Explorer's native tool-calling loop uses
    the same ModelConfigRepository credentials without extending other consumers.
    """
    config = model_config.resolve("llm")
    usage_tracker = model_config.create_usage_tracker(config)
    usage_context = replace(current_usage_context(), source_step="journey_explore")

    async def stream():
        with bind_usage_context(usage_context):
            # 与 develop 的模型 HTTP 默认值一致，允许推理模型完成整轮输出。
            async with AsyncOpenAI(
                api_key=config.api_key, base_url=config.base_url, timeout=300
            ) as client:
                async for event in run_explore_sse(
                    ExplorerAccess(service.store, scope),
                    query,
                    client,
                    config.model,
                    executor=service.executor,
                    max_rounds=max_rounds,
                    lang=lang,
                    usage_tracker=usage_tracker,
                ):
                    yield event

    return stream()


DEFAULT_MAX_ROUNDS = 8
MAX_TOOL_CALLS_TOTAL = 20
WALL_TIMEOUT_SEC = 150.0
TOOL_RESULT_MAX_CHARS = 3600  # 喂回模型的单条工具结果上限
SSE_RESULT_MAX_CHARS = 60_000  # SSE tool_result 原始 JSON 上限（画布另有 graph 载荷）
PROPS_VALUE_MAX_CHARS = 300  # 画布节点 props 单字段截断

# ---------------------------------------------------------------------- #
# 工具 schema —— 描述沿用 MCP cm_curation_server 的实战文案（节选）
# ---------------------------------------------------------------------- #

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "describe_graph",
            "description": (
                "图拓扑快照：schema 卡 + 标签计数。探索前先调这个了解有哪些节点类型、"
                "哪些可遍历边、哪些标签可全文检索，不要凭记忆猜 label。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_anchors",
            "description": (
                "检索第一步：全文搜索定位锚点节点（手里只有文本、没有 key 时从这里进）。"
                "可搜 label：Metric(指标)/Dimension(维度)/"
                "Dataset/DatasetField(数据集字段)/Event/Entity。"
                "★查指标别只搜 Metric——Dimension 能按维度文本搜；MetricCaliber 无全文索引，"
                "先搜 Metric 再沿 HAS 边看公式。"
                "不要传 Table/Column 作 labels（物理层无全文索引必零命中）。"
                "零命中不要直接放弃或原样重试：自己改写查询词再试（同义词、英文/缩写、"
                "更短或更宽泛的说法，至多 3 次）——零命中返回会附 sample_names（图内"
                "现存名字抽样），照它的命名风格改写；仍零命中再去掉 labels 或 enumerate_domain。"
                "★k 建议 ≤10：画布按锚点逐步展现，一次太多会糊。"
                "返回 {anchors:[{label,key,name,score,edge_types}], searched_labels, unsearchable_labels}。"
                "★edge_types={out:[...],in:[...]} 是该锚点真实存在的边类型；下一步 "
                "expand_subgraph 的 relations 直接从这里面选，不要凭记忆猜边名、零结果反复重试。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词（支持中文）"},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": '限定 label 列表，如 ["Metric","MetricCaliber"]；省略=全部可搜标签',
                    },
                    "k": {
                        "type": "integer",
                        "description": "每个 label 的召回数，默认 8",
                        "default": 8,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_subgraph",
            "description": (
                "检索第二步：从锚点 key 沿**你指定的关系集**扩展邻域（原语级：只拉需要的边），depth 上限 2。"
                "relations 省略=沿全部读白名单展开（边会很多，慎用）；传 relations 可精确控制拓展哪类关系。"
                "★按场景选关系，避免过度扩展："
                '指标血缘/取数路径用 ["DERIVED_FROM","HAS","CALIBER_ON","MAPS_TO_FIELD"]；'
                '维度→数据集字段用 ["MAPS_TO_FIELD","HAS"]；'
                '目录归属用 ["HAS"]。'
                "HAS 同时用于目录、字段、口径等包含关系，具体含义由起止节点类型决定。"
                "depth=2 经 Domain 会拉出兄弟节点，域内清单请用 enumerate_domain。"
                "返回 nodes（含完整业务属性：MetricCaliber 的 formula/time_window、Dataset 的 sql/parents、"
                "DatasetField 的 data_type 等）+ edges {from,rel,to}。"
                "★画布小步增量：limit 建议 ≤24、seeds 聚焦最相关的几个；更大范围拆多次调用。边多用 limit/page 翻页。"
                "节点 key 不可推断，必须使用检索结果返回的 key。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seeds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "种子节点 key 列表",
                    },
                    "relations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "限定展开的关系类型（白名单内）；省略=全部读白名单",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "扩展跳数 1-2，默认 1",
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "每页边数上限，默认 200",
                        "default": 200,
                    },
                    "page": {
                        "type": "integer",
                        "description": "页码 0 开始",
                        "default": 0,
                    },
                },
                "required": ["seeds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enumerate_domain",
            "description": (
                "粗粒度域内全量枚举：给业务域名（如 Sales），穷举该域全部 "
                "Dataset/Metric/Dimension 清单（非检索召回，保证不漏分表），不写 Cypher。"
                "返回 {domain, datasets, metrics, dimensions, counts}。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "业务域名；空串=全部域概览",
                    },
                },
                "required": [],
            },
        },
    },
]

# 前端 MiniMarkdown 只渲染该子集；prompt 与收尾指令必须与之对齐。
_REPLY_LANG_NAMES = {
    "zh": "中文",
    "cn": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "vi": "越南语",
    "th": "泰语",
    "id": "印尼语",
}


def reply_language_directive(lang: str | None) -> str:
    """UI 语言 → 注入句。中文为「中文回复」，其它为「xx语言回复」/「xx回复」。"""
    raw = (lang or "zh").strip().lower().replace("_", "-")
    primary = raw.split("-", 1)[0] if raw else "zh"
    if primary in ("zh", "cn"):
        return "中文回复"
    name = _REPLY_LANG_NAMES.get(primary)
    if name:
        return f"{name}回复"
    return f"{primary}语言回复"


def build_explore_system_prompt(lang: str | None = None) -> str:
    """组装探索 agent 的 system prompt（含回复语言指令）。"""
    reply_lang = reply_language_directive(lang)
    return f"""你是 CM 知识图谱上的自主探索 agent。用户当前处于**上下文探索任务**：目标不是做封闭问答，而是把用户关心的概念在图里的上下文补全，写成一份可读的探索报告。

【回复语言】{reply_lang}
最终报告、小节标题、叙述正文必须使用该语言。指标名、公式原文、表名、列名、节点 key 保持图中原文，不要翻译标识符。

任务默认识别：
- 用户输入可以是完整问句，也可以只是一个指标名、维度名、数据集名、域名或业务概念。
- 只要不是明确的闲聊或无关指令，一律按上下文探索处理：定位该概念，补上定义、口径、取数路径、相关对象与缺口。
- 不要反问「你想问什么」；用工具把上下文找齐后再写报告。短语查询（如「DAU」「会话时长」）默认等于「请探索这个概念及其上下文」。

工作方式：
- 你完全自主：调哪些工具、什么顺序、调几轮、何时停止，都由你决定。可以回溯、换关键词、从不同锚点分头探索。
- 只依据工具返回的证据写作；图里没有的就是没有，不要编造指标/公式/表列。
- 没有自由 Cypher，一律粗粒度工具：describe_graph 看拓扑 → find_anchors 定位 → expand_subgraph 沿边展开 → enumerate_domain 穷举域内清单。
- ★expand_subgraph 是原语级：用 relations 参数**只展开需要的关系**（如血缘用 DERIVED_FROM/HAS/CALIBER_ON/MAPS_TO_FIELD），不要默认全关系展开（边太多会糊）；HAS 的具体含义由端点类型区分，域内清单用 enumerate_domain 而非经 Domain 全展开。
- 证据足够就停止调用，直接输出最终探索报告。
- 如果多轮探索仍一无所获，也如实写成报告：尝试过什么、图里缺什么。

最终输出必须是一份完整、连贯的探索报告，不是节点/术语/变量清单。用完整句子讲清楚「这是什么、怎么算、数据从哪来、和什么相关、还缺什么」。技术标识（key、label、关系类型）只在必要时用行内 `code` 点到为止，不要在正文堆砌变量名或字段罗列。

严格 markdown（前端只渲染下列子集，违反会导致排版错乱）：
- 只用 `#` / `##` / `###` / `####` 标题，井号后必须有一个空格，例如 `## 它是什么`
- 块与块之间空一行
- 加粗只用 `**text**`，行内代码只用 `code`（反引号成对）
- 列表只用行首 `- ` 或 `1. `，不要嵌套列表
- 表格必须含表头分隔行，列数对齐：
  | 列1 | 列2 |
  | --- | --- |
  | 值 | 值 |
- 禁止：HTML、``` 围栏代码块、把整篇包进代码围栏、图片、`[链接](url)`、`*斜体*`、标题与正文粘在同一行

报告结构（按此顺序；某节无证据可省略，不要自造其它一级标题）：
# {{概念名}}：探索报告

一段话概述：这是什么、业务上做什么用、这次探索覆盖了哪些上下文。

## 它是什么

用白话解释定义与业务含义，写成段落，不要罗列属性字段。

## 怎么算、数据从哪来

用段落讲口径与取数路径。公式先用人话解释，必要时再给原文。表/列写成「落在某某表的某某列」，不要只丢 key。

## 相关上下文

把相关指标、维度、经验主张串成叙述：它们如何与主题相连。需要对照时用一张不超过 6 行的小表。

## 尚未在图中找到的

如实写尝试过什么、图里缺什么。没有缺口则整节省略。"""


def _budget_wrapup_prompt(lang: str | None = None) -> str:
    reply_lang = reply_language_directive(lang)
    return (
        "（预算已用完）请基于以上已获得的信息直接输出最终探索报告，不要再调用工具。"
        f"回复语言：{reply_lang}。严格遵守系统提示中的报告结构与 markdown 子集。"
    )


# ---------------------------------------------------------------------- #
# 工具执行
# ---------------------------------------------------------------------- #


def _exec_describe_graph(access: Any, args: dict[str, Any]) -> dict[str, Any]:
    from .graph_tools import graph_health

    health = graph_health(access)
    return {
        "schema": access.store.schema.get_overview(),
        "label_counts": health.get("labels") or {},
        "status_counts": health.get("status") or {},
        "node_total": sum((health.get("labels") or {}).values()),
    }


def _annotate_anchor_edges(access: Any, anchors: list[dict[str, Any]]) -> None:
    """Annotate the chosen anchors using only their authorized neighborhoods."""
    try:
        for anchor in anchors[:12]:
            node = access.node(str(anchor.get("key") or ""))
            directions = {"out": set(), "in": set()}
            if node is not None:
                for _, edge in access.neighbors(node):
                    directions["out" if edge.source_key == node.key else "in"].add(
                        edge.spec.relationship
                    )
            anchor["edge_types"] = {
                key: sorted(value) for key, value in directions.items()
            }
    except StoreError as exc:
        log.debug("annotate anchor edges failed: %s", exc)


def _sample_existing_names(
    access: Any, labels: Optional[list[str]] = None, cap_per_label: int = 6
) -> dict[str, list[str]]:
    """Offer real names when the model needs to reformulate a search."""
    out = {}
    for label in labels or ():
        try:
            names = []
            for node in access.nodes((label,)):
                name = node.properties.get("name")
                if name is not None:
                    names.append(str(name))
                if len(names) >= cap_per_label:
                    break
            if names:
                out[label] = names
        except StoreError as exc:
            log.debug("sample existing names failed: %s", exc)
    return out


def _exec_find_anchors(access: Any, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    labels = args.get("labels") or None
    k = int(args.get("k") or 8)
    result = find_anchors(access, query, labels=labels, k=min(max(1, k), 50))
    # Fall back only when none of the requested types has a usable index.
    # A successful search with zero hits must remain an empty result.
    if not result.get("anchors") and not result.get("searched_labels"):
        from .retrieval import search_explorer_nodes

        # labels 过滤是在扫描 top-N 之后做的；若限定 label（如 Domain），
        # 必须放大扫描上限，否则目标 label 会在 post-filter 前被截掉。
        scan_limit = 100 if labels else min(max(1, k), 50) * 3
        rows = search_explorer_nodes(
            access,
            query,
            limit=scan_limit,
        )
        anchors = []
        for r in rows:
            group = str(r.get("group") or "")
            if labels and group not in labels:
                continue
            anchors.append(
                {
                    "label": group,
                    "key": r.get("id"),
                    "name": r.get("label"),
                    "score": float(r.get("score") or 0),
                }
            )
        result = {
            "anchors": anchors[: min(max(1, k), 50) * 3],
            "searched_labels": sorted({a["label"] for a in anchors}) or ["<scan>"],
            "unsearchable_labels": [],
            "method": "contains_scan_fallback",
        }
    anchors_out = result.get("anchors") or []
    _annotate_anchor_edges(access, anchors_out)
    if not anchors_out:
        # 零命中不给死答案：附现存名字抽样，让模型照命名风格自主改写重试
        # （而非原样重发或直接放弃）。
        sample = _sample_existing_names(access, labels)
        if sample:
            result["sample_names"] = sample
        result["zero_hit_hint"] = (
            "零锚点。请自己改写查询词重试：换同义词、英文/缩写、更短或更宽泛的说法，"
            "对照 sample_names 里图内真实名字的命名风格来改；仍无命中再去掉 labels "
            "限制或改用 enumerate_domain，不要直接断定概念不存在。"
        )
    return result


def _exec_expand_subgraph(access: Any, args: dict[str, Any]) -> dict[str, Any]:
    seeds = args.get("seeds") or []
    if not isinstance(seeds, list) or not seeds:
        raise ValueError("seeds must be a non-empty list of node keys")
    relations = args.get("relations") or None
    if relations is not None:
        relations = [str(r) for r in relations if r]
        relations = relations or None
    return expand_subgraph(
        access,
        [str(s) for s in seeds],
        relations=relations,
        depth=min(max(1, int(args.get("depth") or 1)), 2),
        limit=min(max(1, int(args.get("limit") or 200)), 1000),
        page=max(0, int(args.get("page") or 0)),
    )


def _resolve_key(access: Any, name_or_key: str, prefix: str, labels: list[str]) -> str:
    """把名字/key 归一成节点 key。已是前缀 key 直接返回；否则粗粒度检索取 top1。"""
    s = (name_or_key or "").strip()
    if not s:
        raise ValueError("name_or_key is required")
    if s.startswith(prefix + ":"):
        return s
    res = _exec_find_anchors(access, {"query": s, "labels": labels, "k": 1})
    anchors = res.get("anchors") or []
    if not anchors:
        raise ValueError(f"no {prefix} node matched {s!r}")
    return str(anchors[0]["key"])


def _group_by_label(
    expand_res: dict[str, Any], want: dict[str, list[str]]
) -> dict[str, list[dict[str, Any]]]:
    """把 expand 的 nodes 按 label 分桶，取 name/key/关键属性。"""
    out = {k: [] for k in want}
    for key, n in (expand_res.get("nodes") or {}).items():
        n = n or {}
        label = str(n.get("label") or "")
        for bucket, labels in want.items():
            if label in labels:
                out[bucket].append(
                    {
                        "key": key,
                        "name": n.get("name") or key,
                        **{
                            f: n.get(f)
                            for f in (
                                "formula",
                                "expression",
                                "table_name",
                                "data_type",
                                "description",
                            )
                            if n.get(f) is not None
                        },
                    }
                )
    return out


def _exec_enumerate_domain(access: Any, args: dict[str, Any]) -> dict[str, Any]:
    domain = (args.get("domain") or "").strip()
    if not domain:
        from .graph_tools import graph_health

        health = graph_health(access)
        return {
            "scope": "all_domains",
            "label_counts": health.get("labels") or {},
            "note": "传 domain 名可穷举该域 Dataset/Metric/Dimension 清单",
        }
    key = _resolve_key(access, domain, "dom", ["Domain"])
    exp = expand_subgraph(access, [key], depth=1, limit=500)
    g = _group_by_label(
        exp,
        {
            "datasets": ["Dataset"],
            "metrics": ["Metric"],
            "dimensions": ["Dimension"],
        },
    )
    return {
        "scope": "domain",
        "domain": domain,
        "datasets": g["datasets"],
        "metrics": g["metrics"],
        "dimensions": g["dimensions"],
        "counts": {k: len(v) for k, v in g.items()},
        "nodes": exp.get("nodes") or {},
        "edges": exp.get("edges") or [],
    }


_EXECUTORS = {
    "describe_graph": _exec_describe_graph,
    "find_anchors": _exec_find_anchors,
    "expand_subgraph": _exec_expand_subgraph,
    "enumerate_domain": _exec_enumerate_domain,
}


def _slim_props(props: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in props.items():
        if k in ("workspace_id", "owner_id"):
            continue
        s = v if isinstance(v, (int, float, bool)) else str(v)
        if isinstance(s, str) and len(s) > PROPS_VALUE_MAX_CHARS:
            s = s[:PROPS_VALUE_MAX_CHARS] + "…"
        out[k] = s
    return out


def _graph_payload(name: str, result: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Extract normalized {nodes, edges} for the canvas, if the tool produced any.

    画布载荷做上限截断：模型读的是完整 tool result，画布只拿一小批，
    避免一次弹出一大片节点糊掉场景（动态逐粒展现由前端排队完成）。
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    try:
        if name == "find_anchors":
            for a in (result.get("anchors") or [])[:12]:
                nodes.append(
                    {
                        "key": a.get("key"),
                        "label": a.get("label"),
                        "name": a.get("name"),
                        "props": {"score": a.get("score")},
                    }
                )
        elif name in ("expand_subgraph", "enumerate_domain"):
            # BFS 序种子在前，截断保留探索核心
            for key, n in list((result.get("nodes") or {}).items())[:24]:
                n = n or {}
                label = n.get("label") or ""
                props = {
                    k: v for k, v in n.items() if k not in ("label", "name", "key")
                }
                nodes.append(
                    {
                        "key": key,
                        "label": label,
                        "name": n.get("name") or key,
                        "props": _slim_props(props),
                    }
                )
            kept = {n["key"] for n in nodes}
            for e in result.get("edges") or []:
                if e.get("from") in kept and e.get("to") in kept:
                    edges.append(
                        {"from": e.get("from"), "rel": e.get("rel"), "to": e.get("to")}
                    )
            edges = edges[:40]
    except Exception as exc:  # noqa: BLE001 - payload is cosmetic, never crash the loop
        log.debug("graph_payload(%s) failed: %s", name, exc)
        return None
    if not nodes and not edges:
        return None
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------- #
# SSE helpers
# ---------------------------------------------------------------------- #


def _sse(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _assistant_record(msg: Any) -> dict[str, Any]:
    """Serialize an assistant message (incl. tool_calls) back into messages."""
    rec: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        rec["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return rec


# ---------------------------------------------------------------------- #
# 主循环
# ---------------------------------------------------------------------- #


async def run_explore_sse(
    access: Any,
    query: str,
    client: Any,
    model: str,
    *,
    executor: BoundedExecutor,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    lang: str | None = None,
    usage_tracker: ModelUsageTracker | None = None,
) -> AsyncIterator[str]:
    """Stream an autonomous tool-calling exploration as SSE frames."""
    max_rounds = min(max(1, max_rounds), 16)

    yield _sse("start", {"query": query, "model": model, "max_rounds": max_rounds})

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_explore_system_prompt(lang)},
        {"role": "user", "content": query},
    ]
    t0 = time.time()
    tool_calls_total = 0
    rounds_used = 0
    final_text: Optional[str] = None
    budget_reason: Optional[str] = None

    def _budget_hit() -> Optional[str]:
        if tool_calls_total >= MAX_TOOL_CALLS_TOTAL:
            return f"工具调用总数达到上限 {MAX_TOOL_CALLS_TOTAL}"
        if time.time() - t0 > WALL_TIMEOUT_SEC:
            return f"墙钟时间超过 {WALL_TIMEOUT_SEC:.0f}s"
        return None

    try:
        for rnd in range(1, max_rounds + 1):
            rounds_used = rnd
            tracking = (
                usage_tracker.track_call("chat", model)
                if usage_tracker is not None
                else nullcontext()
            )
            with tracking as record_response:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=_TOOLS,
                    tool_choice="auto",
                )
                if record_response is not None:
                    record_response(resp)
            msg = resp.choices[0].message

            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                yield _sse("thinking", {"round": rnd, "text": str(reasoning)})
            if msg.content and not getattr(msg, "tool_calls", None):
                # 无 tool_call 的 content = 模型决定收尾
                final_text = msg.content
                messages.append(_assistant_record(msg))
                break
            if msg.content:
                yield _sse("thinking", {"round": rnd, "text": str(msg.content)})

            tool_calls = list(getattr(msg, "tool_calls", None) or [])
            if not tool_calls:
                # finish 但既无内容也无调用——视为收尾失败，强制总结
                budget_reason = "模型未产生内容也未调用工具"
                break

            messages.append(_assistant_record(msg))

            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                yield _sse(
                    "tool_call",
                    {
                        "round": rnd,
                        "id": tc.id,
                        "name": name,
                        "args": args,
                    },
                )

                ok = True
                result: Any
                operation = _EXECUTORS.get(name)
                if operation is None:
                    ok = False
                    result = {"error": f"unknown tool: {name}"}
                else:
                    try:
                        result = await executor.run(operation, access, args)
                    except Exception as exc:  # noqa: BLE001 - feed error back, let model adapt
                        ok = False
                        result = {"error": str(exc)}
                        log.warning("explore tool %s failed: %s", name, exc)
                tool_calls_total += 1

                # 喂回模型的结果做尺寸封顶
                result_json = json.dumps(result, ensure_ascii=False, default=str)
                fed = result_json
                if len(fed) > TOOL_RESULT_MAX_CHARS:
                    fed = (
                        fed[:TOOL_RESULT_MAX_CHARS]
                        + " …[结果过长已截断，可加 limit/翻页或缩小 seeds]"
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": fed,
                    }
                )

                sse_result = result_json
                if len(sse_result) > SSE_RESULT_MAX_CHARS:
                    sse_result = sse_result[:SSE_RESULT_MAX_CHARS] + "…[truncated]"
                evt: dict[str, Any] = {
                    "round": rnd,
                    "id": tc.id,
                    "name": name,
                    "ok": ok,
                    "result": sse_result,
                }
                if ok:
                    gp = _graph_payload(
                        name, result if isinstance(result, dict) else {}
                    )
                    if gp:
                        evt["graph"] = gp
                yield _sse("tool_result", evt)

            budget_reason = _budget_hit()
            if budget_reason:
                break

        if final_text is None:
            # 预算耗尽或异常收尾：强制一次无工具总结
            if budget_reason:
                yield _sse("budget", {"reason": budget_reason})
            messages.append(
                {
                    "role": "user",
                    "content": _budget_wrapup_prompt(lang),
                }
            )
            tracking = (
                usage_tracker.track_call("chat", model)
                if usage_tracker is not None
                else nullcontext()
            )
            with tracking as record_response:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                )
                if record_response is not None:
                    record_response(resp)
            final_text = resp.choices[0].message.content or "（模型未返回总结）"

        yield _sse(
            "final",
            {
                "text": final_text,
                "rounds": rounds_used,
                "tool_calls": tool_calls_total,
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )
    except Exception as exc:  # noqa: BLE001 - report and close the stream
        log.exception("explore agent failed: %s", exc)
        yield _sse("error", {"message": str(exc)})
    yield _sse("done", {})

"""从环境变量与项目根目录的 .env 加载配置。

主 Agent / Explorer 检索与图浏览上限：``config/agent_explorer.json``（或环境变量 ``AGENT_EXPLORER_CONFIG`` 指向的 JSON），
与内置默认值合并；改参编辑 JSON 后需重启进程。``thinking.agent`` / ``thinking.explorer`` 按步骤控制
是否对兼容 API 发送 ``enable_thinking``（如 DashScope 思考模式）。
``llm.sql_explanation``：NL→SQL 是否在模型回复中要求 ``### Explanation`` + ``### SQL``（关则仅 SQL，省 token）。
"""
from __future__ import annotations  # 允许在类型注解中使用尚未定义的类型名

import json
import os  # 读取进程环境变量
from dataclasses import dataclass, field  # 用数据类承载不可变配置快照；field 用于超时工厂
from pathlib import Path  # 跨平台路径对象
from typing import Any, Literal, Optional

TraversalEdgeDirection = Literal["out", "in", "both"]

from qwenpaw_data.context.env import load_qwenpaw_data_env
from qwenpaw_data.context.paths import kg_documents_dir, sessions_db_path as _qwenpaw_data_sessions_db

# src/context_manager/config.py 上溯三级 = 包根（packages/qwenpaw-data-context）。
ROOT = Path(__file__).resolve().parent.parent.parent
load_qwenpaw_data_env(override=False)


def _env_flag(name: str, default: str = "1") -> bool:
    """把环境变量解析为布尔：空/0/false/no/off 视为 False，其余为 True。"""
    return os.getenv(name, default).strip().lower() not in {"", "0", "false", "no", "off"}


def _llm_http_ignore_proxy() -> bool:
    """为 LLM 专用 httpx 客户端关闭 trust_env，使本次 API 流量不走 HTTP(S)_PROXY（与 HF 等其它请求解耦）。"""
    return _env_flag("LLM_HTTP_IGNORE_PROXY", "0")


def _llm_http_timeout_default() -> Optional[float]:
    """单次 Chat Completions 的 HTTP 读超时（秒）。``LLM_HTTP_TIMEOUT<=0`` 表示不设（交给 SDK，可能无限等）。"""
    raw = (os.getenv("LLM_HTTP_TIMEOUT") or "180").strip()
    try:
        v = float(raw)
    except ValueError:
        return 180.0
    return None if v <= 0 else v


def _doc_storage_dir_default() -> str:
    """KG documents 默认落到 ``${QWENPAW_DATA_HOME}/data-bridge/kg/documents``。"""

    raw = (os.getenv("DOC_STORAGE_DIR") or "").strip()
    if raw:
        return str(Path(raw).expanduser().resolve())
    return str(kg_documents_dir())


def knowledge_ingest_heavy_llm_timeout_sec() -> float:
    """Pass A/B/C 等大 JSON 请求的单次 HTTP 读超时（秒）。

    默认不低于 ``max(600, LLM_HTTP_TIMEOUT)``，避免 Pass B 聚类在 180s 时被截断。
    可用 ``KNOWLEDGE_INGEST_LLM_TIMEOUT`` 覆盖（正数秒）。
    """
    raw = (os.getenv("KNOWLEDGE_INGEST_LLM_TIMEOUT") or "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    base = _llm_http_timeout_default()
    if base is None:
        return 900.0
    return max(600.0, float(base))


def resolve_neo4j_database(*, role: Literal["default", "demo", "mcp"] = "default") -> Optional[str]:
    """按用途解析 Neo4j 逻辑库名。

    - ``default`` — ``NEO4J_DATABASE``（setup / bench / ``make serve`` 等）
    - ``demo`` — ``NEO4J_DATABASE_DEMO``，回退 ``NEO4J_DATABASE``
    - ``mcp`` — ``NEO4J_DATABASE_MCP``，回退 ``NEO4J_DATABASE``
    """
    if role == "demo":
        d = (os.getenv("NEO4J_DATABASE_DEMO") or os.getenv("NEO4J_DATABASE") or "").strip()
    elif role == "mcp":
        d = (os.getenv("NEO4J_DATABASE_MCP") or os.getenv("NEO4J_DATABASE") or "").strip()
    else:
        d = (os.getenv("NEO4J_DATABASE") or "").strip()
    return d or None


def _neo4j_database() -> Optional[str]:
    """Neo4j 5 逻辑库名；非空时 driver session 使用该库（Makefile/setup 按数据集隔离）。"""
    return resolve_neo4j_database(role="default")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """浅层合并两级：``agent`` / ``explore`` / ``llm`` 内未写的键保留默认值。

    ``thinking`` 再多合并一层：``thinking.agent`` / ``thinking.explorer`` 可按步骤单独覆盖。
    """
    out: dict[str, Any] = dict(base)
    for k, v in override.items():
        if (
            k in out
            and isinstance(out[k], dict)
            and isinstance(v, dict)
        ):
            if k == "thinking":
                merged_t = dict(out[k])
                for sk, sv in v.items():
                    if (
                        sk in merged_t
                        and isinstance(merged_t.get(sk), dict)
                        and isinstance(sv, dict)
                    ):
                        merged_t[sk] = {**(merged_t.get(sk) or {}), **sv}
                    else:
                        merged_t[sk] = sv
                out[k] = merged_t
            else:
                out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


_DEFAULT_AGENT_EXPLORER: dict[str, Any] = {
    "agent": {
        "decision_temperature": 0.0,
        "decision_max_retries": 2,
        "sql_temperature": 0.0,
        "semantic_split_temperature": 0.0,
        "semantic_split_max_retries": 2,
        "retrieval_rewrite_temperature": 0.0,
        "retrieval_rewrite_max_retries": 2,
        "retrieval_entity_expand_temperature": 0.0,
        "retrieval_entity_expand_max_retries": 2,
        "strategy_semantics_temperature": 0.2,
        "strategy_semantics_max_retries": 1,
    },
    # 各阶段是否调用 LLM（false = 该步跳过模型，走规则/模板/兜底）
    "llm": {
        "retrieval_rewrite": True,
        "retrieval_entity_expand": True,
        "semantic_split": True,
        "decision": True,
        "sql_generation": True,
        "strategy_semantics_distill": True,
        # NL→SQL 是否在回复中要求 ### Explanation + ### SQL（关则仅输出 SQL，评测省 token）
        "sql_explanation": True,
    },
    # 兼容 DashScope / Qwen OpenAI 兼容接口的 ``enable_thinking``（逐步 + 场景）
    "thinking": {
        "agent": {
            "retrieval_rewrite": False,
            "retrieval_entity_expand": False,
            "semantic_split": False,
            "decision": False,
            "sql_generation": False,
            "strategy_semantics_distill": False,
        },
        # Explorer（Topology UI / HTTP）未列出的键继承 agent 同名字段
        "explorer": {},
    },
    # 拓扑管线召回与候选图规模（anchor / 策略卡 / 决策候选边）
    "recall": {
        "anchor_k_fulltext": 25,
        "anchor_k_vector": 25,
        # RRF 三路融合时 sparse (fulltext) leg 的权重；dense (lex/facet) 固定 1.0。
        # 默认 2.0:dense 两条腿天然 2× 加权,sparse 一条腿要追平至少 2.0。
        "sparse_weight": 2.0,
        # Event / Entity 全文分数在并入锚点全集前乘以该系数（<1 降低 KG 相对 schema 的排序权重）
        "anchor_knowledge_score_scale": 0.4,
        "anchor_knowledge_merge_cap": None,
        "strategy_card_top_k": 5,
        "strategy_card_auto_accept_threshold": 0.85,
        "strategy_card_auto_accept_gap": 0.1,
        "semantic_split_max_facets": 8,
        "candidate_edges_max_anchors": 16,
        "candidate_edges_per_node_limit": 80,
        "candidate_edges_max_total_edges": 400,
        "decision_candidate_edge_hops": 2,
        "decision_max_path_edges": 8,
        "decision_react_max_rounds": 4,
        "decision_react_expand_new_edges_cap": 200,
        # 锚点 BFS / 决策 ReAct 扩边 / BFS 兜底方向：out | in | both
        "traversal_edge_direction": "out",
        # ---- L1/L2 relevance gate（search_context / search_event / explore_entity）----
        # 命中判据 = text_weight*软文本匹配(CJK bigram) + vec_weight*向量余弦。
        # 旧实现把 60% 权重压在"子串包含"上 + 用 RRF 排名分当置信度，导致
        # "访问趋势分析"这类纯语义改写永远过不了 0.40 门槛（RRF 上限 ~0.27）。
        # 现默认让向量主导，门槛对齐余弦尺度。
        "relevance_text_weight": 0.4,
        "relevance_vec_weight": 0.6,
        "relevance_threshold": 0.40,
        "relevance_floor": 0.20,
        # ---- 召回后精排（cross-encoder / LLM judge）：默认开 ----
        # text+vec 混合分无法稳健区分近似打平的 niche 指标（如「分享页访问用户数」）
        # 与正典指标（「当日访问用户数」），需要语义精排来定序；其分数经
        # api.semantic_pack.score_anchor 融合进最终 relevance_score。
        "rerank_enabled": True,
        "rerank_provider": "llm",      # llm | embedding
        "rerank_model": None,           # None → 复用 CFG.llm_model
        "rerank_top_n": 12,             # 仅对池内前 N 个候选精排（越小越快）
        "rerank_score_weight": 0.5,     # 精排分与原召回分的融合权重（0=只用召回，1=只用精排）
        "rerank_timeout_sec": 8.0,
    },
    "explore": {
        "global_graph_max_edges": 800,
        "global_graph_max_nodes": 600,
        "search_nodes_limit": 25,
        "expand_max_edges": 80,
        "domain_graph_max_nodes": 200,
    },
}


def _load_agent_explorer_json() -> dict[str, Any]:
    """主 Agent + Explorer 超参数：默认 ``config/agent_explorer.json``；路径可用 ``AGENT_EXPLORER_CONFIG`` 覆盖。"""
    raw_path = (os.getenv("AGENT_EXPLORER_CONFIG") or "").strip()
    path = Path(raw_path) if raw_path else (ROOT / "config" / "agent_explorer.json")
    merged = dict(_DEFAULT_AGENT_EXPLORER)
    merged["agent"] = dict(_DEFAULT_AGENT_EXPLORER["agent"])
    merged["explore"] = dict(_DEFAULT_AGENT_EXPLORER["explore"])
    merged["llm"] = dict(_DEFAULT_AGENT_EXPLORER["llm"])
    merged["thinking"] = {
        "agent": dict((_DEFAULT_AGENT_EXPLORER.get("thinking") or {}).get("agent") or {}),
        "explorer": dict((_DEFAULT_AGENT_EXPLORER.get("thinking") or {}).get("explorer") or {}),
    }
    merged["recall"] = dict(_DEFAULT_AGENT_EXPLORER["recall"])
    if not path.is_file():
        return merged
    try:
        with path.open(encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            return merged
        return _deep_merge(merged, user)
    except (OSError, json.JSONDecodeError):
        return merged


def normalize_traversal_edge_direction(raw: Any) -> TraversalEdgeDirection:
    """``recall.traversal_edge_direction``：out（出边）| in（入边）| both（双向）。"""
    v = str(raw or "out").strip().lower()
    if v in ("out", "outward", "down"):
        return "out"
    if v in ("in", "inward", "up"):
        return "in"
    if v in ("both", "all", "bidirectional", "undirected"):
        return "both"
    return "out"


_AGENT_EXPLORER = _load_agent_explorer_json()
_A = _AGENT_EXPLORER["agent"]
_X = _AGENT_EXPLORER["explore"]
_L = _AGENT_EXPLORER.get("llm") or {}
_R = _AGENT_EXPLORER.get("recall") or {}
_TRAVERSAL_EDGE_DIR = normalize_traversal_edge_direction(_R.get("traversal_edge_direction", "out"))


def _json_bool(d: dict[str, Any], key: str, default: bool = True) -> bool:
    v = d.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() not in {"", "0", "false", "no", "off"}
    return default


_THINKING_STEPS = frozenset(
    {
        "retrieval_rewrite",
        "retrieval_entity_expand",
        "semantic_split",
        "decision",
        "sql_generation",
        "strategy_semantics_distill",
    },
)


def llm_thinking_enabled(step: str, *, context: str = "agent") -> bool:
    """是否对该步 LLM 请求附加 ``extra_body.enable_thinking``（与 ``llm.*`` 开关无关）。

    ``context``：``agent`` = 评测 CLI / 默认管线；``explorer`` = Topology Explorer HTTP。
    ``thinking.explorer`` 中未写的步骤继承 ``thinking.agent``。
    """
    if step not in _THINKING_STEPS:
        return False
    root = _AGENT_EXPLORER.get("thinking")
    if not isinstance(root, dict):
        return False
    agent_map = root.get("agent")
    explorer_map = root.get("explorer")
    if not isinstance(agent_map, dict):
        agent_map = {}
    if not isinstance(explorer_map, dict):
        explorer_map = {}
    ctx = (context or "agent").strip().lower()
    if ctx == "explorer" and step in explorer_map:
        return _json_bool(explorer_map, step, False)
    return _json_bool(agent_map, step, False)


@dataclass(frozen=True)  # frozen=True：实例创建后不可变，当作常量配置用
class Config:
    """运行时配置：Neo4j、OpenAI 兼容 API、Embedding。"""

    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")  # Bolt 连接串
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")  # 数据库用户名
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")  # 数据库密码
    # Neo4j 5 逻辑库名；不设则默认库（仅手动调试）；数据集流水线应设 NEO4J_DATABASE
    neo4j_database: Optional[str] = field(default_factory=_neo4j_database)
    # 图后端类型；社区版内置 'neo4j'，自定义后端经 graph.backends.registry 注册
    graph_backend: str = os.getenv("GRAPH_BACKEND", "neo4j")

    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")  # LLM API Key（空则部分客户端用占位）
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")  # 可指向兼容服务
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")  # 默认聊天模型名（重活：SQL 生成 / 决策）
    llm_http_timeout: Optional[float] = field(default_factory=_llm_http_timeout_default)  # 秒；防 LLM 挂死整轮
    llm_http_ignore_proxy: bool = field(default_factory=_llm_http_ignore_proxy)  # True 时仅 OpenAI 客户端忽略环境代理

    embed_model: str = os.getenv("EMBED_MODEL", "text-embedding-v3")  # OpenAI 兼容 embedding 模型名
    # 允许 embedder 和 LLM 走不同 provider：跨 vendor 评测时 LLM 与 embedder
    # 可分别配置。未设置时 fall back 到 openai_api_key / openai_base_url。
    embed_openai_api_key: str = os.getenv("EMBED_OPENAI_API_KEY", "")
    embed_openai_base_url: str = os.getenv("EMBED_OPENAI_BASE_URL", "")
    embed_dim: int = int(os.getenv("EMBED_DIM", "1024"))  # 向量维度，需与 Neo4j 向量索引一致

    # ---- SQL 执行后端（execute_sql / pipeline PG exec）----
    # 默认 direct（按 semantic_config.db 中 sync 的数据源 psycopg 直连）；
    # auto: active datasource → DW_* → jdbc；hub 仅显式指定时使用
    sql_exec_backend: str = os.getenv("SQL_EXEC_BACKEND", "direct").strip().lower()

    # ---- 数仓直连（Hologres 等 PG 协议兼容库；sql_exec_backend=direct 或 auto fallback）----
    dw_host: str = os.getenv("DW_HOST", "")
    dw_port: int = int(os.getenv("DW_PORT", "80"))
    dw_user: str = os.getenv("DW_USER", "")
    dw_password: str = os.getenv("DW_PASSWORD", "")
    dw_db: str = os.getenv("DW_DB", "")
    dw_schema: str = os.getenv("DW_SCHEMA", "public")

    # ---- JDBC 直连（sql_exec_backend=jdbc；容器内已有 JAVA_HOME 供 Neo4j）----
    jdbc_url: str = os.getenv("JDBC_URL", "")
    jdbc_driver: str = os.getenv("JDBC_DRIVER", "org.postgresql.Driver")
    jdbc_jar_path: str = os.getenv("JDBC_JAR_PATH", "")
    jdbc_user: str = os.getenv("JDBC_USER", "")
    jdbc_password: str = os.getenv("JDBC_PASSWORD", "")

    # ---- ODPS (MaxCompute) akless 连接 ----
    # app_name: 应用标识（URN 解析用）；project: ODPS project；endpoint: service endpoint
    odps_app_name: str = os.getenv("ODPS_APP_NAME", "")
    odps_project: str = os.getenv("ODPS_PROJECT", "")
    odps_endpoint: str = os.getenv(
        "ODPS_ENDPOINT", ""
    )

    # ---- Topology 主 Agent + Explorer：见仓库 ``config/agent_explorer.json``（或 ``AGENT_EXPLORER_CONFIG``）----
    agent_decision_temperature: float = float(_A["decision_temperature"])
    agent_decision_max_retries: int = max(0, int(_A["decision_max_retries"]))
    agent_sql_temperature: float = float(_A["sql_temperature"])
    agent_semantic_split_temperature: float = float(_A["semantic_split_temperature"])
    agent_semantic_split_max_retries: int = max(0, int(_A["semantic_split_max_retries"]))
    agent_retrieval_rewrite_temperature: float = float(_A["retrieval_rewrite_temperature"])
    agent_retrieval_rewrite_max_retries: int = max(0, int(_A["retrieval_rewrite_max_retries"]))
    agent_retrieval_entity_expand_temperature: float = float(
        _A.get("retrieval_entity_expand_temperature", 0.0)
    )
    agent_retrieval_entity_expand_max_retries: int = max(
        0, int(_A.get("retrieval_entity_expand_max_retries", 2))
    )
    agent_strategy_semantics_temperature: float = float(_A["strategy_semantics_temperature"])
    agent_strategy_semantics_max_retries: int = max(0, int(_A["strategy_semantics_max_retries"]))
    agent_followup_intent_temperature: float = float(_A.get("followup_intent_temperature", 0.0))
    agent_followup_intent_max_retries: int = max(0, int(_A.get("followup_intent_max_retries", 2)))

    explore_global_graph_max_edges: int = max(1, int(_X["global_graph_max_edges"]))
    explore_global_graph_max_nodes: int = max(1, int(_X["global_graph_max_nodes"]))
    explore_search_nodes_limit: int = max(1, min(100, int(_X["search_nodes_limit"])))
    explore_expand_max_edges: int = max(1, int(_X["expand_max_edges"]))
    explore_domain_graph_max_nodes: int = max(1, int(_X["domain_graph_max_nodes"]))

    llm_retrieval_rewrite: bool = _json_bool(_L, "retrieval_rewrite", True)
    llm_retrieval_entity_expand: bool = _json_bool(_L, "retrieval_entity_expand", True)
    llm_semantic_split: bool = _json_bool(_L, "semantic_split", True)
    llm_decision: bool = _json_bool(_L, "decision", True)
    llm_sql_generation: bool = _json_bool(_L, "sql_generation", True)
    llm_strategy_semantics_distill: bool = _json_bool(_L, "strategy_semantics_distill", True)
    llm_sql_explanation: bool = _json_bool(_L, "sql_explanation", True)
    llm_followup_intent: bool = _json_bool(_L, "followup_intent", True)
    trace_auto_distill_claims: bool = _env_flag("TRACE_AUTO_DISTILL_CLAIMS", "1")
    trace_auto_distill_strategy: bool = _env_flag("TRACE_AUTO_DISTILL_STRATEGY", "1")
    oss_trace_prefix: str = field(default_factory=lambda: os.getenv("OSS_TRACE_PREFIX", "traces/"))
    claim_strategy_history_top_k: int = max(1, int(_R.get("claim_strategy_history_top_k", 5)))
    claim_strategy_trace_excerpt_chars: int = max(100, int(_R.get("claim_strategy_trace_excerpt_chars", 500)))
    claim_strategy_temperature: float = float(_R.get("claim_strategy_temperature", 0.3))
    claim_strategy_supersede_threshold: float = float(
        _R.get("claim_strategy_supersede_threshold", 0.6)
    )

    recall_anchor_k_fulltext: int = max(1, int(_R.get("anchor_k_fulltext", 25)))
    recall_anchor_k_vector: int = max(1, int(_R.get("anchor_k_vector", 25)))
    recall_sparse_weight: float = max(0.0, float(_R.get("sparse_weight", 3.0)))
    recall_anchor_knowledge_merge_cap: Optional[int] = (
        None
        if _R.get("anchor_knowledge_merge_cap") is None
        else max(0, int(_R["anchor_knowledge_merge_cap"]))
    )
    recall_anchor_knowledge_score_scale: float = max(
        0.0, min(2.0, float(_R.get("anchor_knowledge_score_scale", 0.4)))
    )
    recall_strategy_card_top_k: int = max(1, int(_R.get("strategy_card_top_k", 5)))
    recall_strategy_card_auto_accept_threshold: float = float(
        _R.get("strategy_card_auto_accept_threshold", 0.85)
    )
    recall_strategy_card_auto_accept_gap: float = float(
        _R.get("strategy_card_auto_accept_gap", 0.1)
    )
    recall_semantic_split_max_facets: int = max(1, int(_R.get("semantic_split_max_facets", 8)))
    recall_candidate_edges_max_anchors: int = max(1, int(_R.get("candidate_edges_max_anchors", 16)))
    recall_candidate_edges_per_node_limit: int = max(
        1, int(_R.get("candidate_edges_per_node_limit", 80))
    )
    recall_candidate_edges_max_total_edges: int = max(
        1, int(_R.get("candidate_edges_max_total_edges", 400))
    )
    recall_decision_candidate_edge_hops: int = max(1, int(_R.get("decision_candidate_edge_hops", 2)))
    recall_decision_max_path_edges: int = max(1, int(_R.get("decision_max_path_edges", 8)))
    recall_decision_react_max_rounds: int = max(1, int(_R.get("decision_react_max_rounds", 4)))
    recall_decision_react_expand_new_edges_cap: int = max(
        1, int(_R.get("decision_react_expand_new_edges_cap", 200))
    )
    recall_traversal_edge_direction: TraversalEdgeDirection = _TRAVERSAL_EDGE_DIR

    # ---- L1/L2 relevance gate ----
    relevance_text_weight: float = max(0.0, float(_R.get("relevance_text_weight", 0.4)))
    relevance_vec_weight: float = max(0.0, float(_R.get("relevance_vec_weight", 0.6)))
    relevance_threshold: float = max(0.0, min(1.0, float(_R.get("relevance_threshold", 0.40))))
    relevance_floor: float = max(0.0, min(1.0, float(_R.get("relevance_floor", 0.20))))

    # ---- Recall rerank (global switch) ----
    rerank_enabled: bool = _json_bool(_R, "rerank_enabled", True)
    rerank_provider: str = str(_R.get("rerank_provider", "llm") or "llm").strip().lower()
    rerank_model: Optional[str] = (
        None if _R.get("rerank_model") in (None, "") else str(_R.get("rerank_model"))
    )
    rerank_top_n: int = max(1, int(_R.get("rerank_top_n", 20)))
    rerank_score_weight: float = max(0.0, min(1.0, float(_R.get("rerank_score_weight", 0.5))))
    rerank_timeout_sec: float = max(1.0, float(_R.get("rerank_timeout_sec", 12.0)))

    # ---- L1 search_context shaping caps ----
    l1_primary_metrics: int = max(1, int(_R.get("l1_primary_metrics", 1)))
    l1_max_alternatives: int = max(0, int(_R.get("l1_max_alternatives", 5)))
    l1_knowledge_max: int = max(0, int(_R.get("l1_knowledge_max", 2)))

    # ---- Semantic pack caps (shared by L1+L2) ----
    pack_max_source_columns: int = max(1, int(_R.get("pack_max_source_columns", 8)))
    pack_max_drill_dimensions: int = max(1, int(_R.get("pack_max_drill_dimensions", 8)))
    pack_max_common_filters: int = max(1, int(_R.get("pack_max_common_filters", 5)))
    pack_max_related_metrics: int = max(1, int(_R.get("pack_max_related_metrics", 5)))
    pack_max_knowledge: int = max(0, int(_R.get("pack_max_knowledge", 3)))
    pack_max_events: int = max(1, int(_R.get("pack_max_events", 5)))
    pack_max_experience_hints: int = max(1, int(_R.get("pack_max_experience_hints", 5)))
    pack_event_desc_chars: int = max(50, int(_R.get("pack_event_desc_chars", 400)))
    pack_knowledge_summary_chars: int = max(50, int(_R.get("pack_knowledge_summary_chars", 200)))

    # ---- Secrets / credential vault ----
    import_dry_return: bool = _env_flag("IMPORT_DRY_RETURN", "0")
    api_env: str = os.getenv("API_ENV", "development").strip().lower()

    # ---- Session persistence ----
    sessions_db_path: str = (
        os.getenv("DATAAGENT_SESSIONS_DB")
        or str(_qwenpaw_data_sessions_db())
    )
    sessions_persist: bool = _env_flag("DATAAGENT_SESSIONS_PERSIST", "1")

    # ---- Local document storage (KG docs) ----
    doc_storage_dir: str = field(default_factory=_doc_storage_dir_default)
    doc_max_size: int = int(os.getenv("DOC_MAX_SIZE", "52428800"))


CFG = Config()  # 全项目单例配置对象

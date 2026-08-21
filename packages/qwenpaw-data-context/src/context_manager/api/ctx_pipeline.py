"""Context retrieval pipeline orchestration (search_context internals)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from qwenpaw_data.context.blocking_io import (
    BlockingIOError,
    BlockingIOGovernor,
    BlockingPool,
)

log = logging.getLogger("api.ctx_pipeline")

def _sse_event(event: str, data: Any) -> str:
    """Format a single SSE frame."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


async def _run_in_executor(
    func,
    *,
    governor: BlockingIOGovernor | None = None,
    **kwargs,
):
    """Run a CPU / I/O-bound sync function outside the event loop.

    Worker threads do not inherit ``neo4j_database_ctx``; re-bind the request's
    logical database inside the pool thread so anchor / strategy-card queries
    hit the same Neo4j DB as the HTTP middleware selected. HTTP callers supply
    the bounded governor; the default executor remains only for standalone
    in-process callers.
    """
    from ..utils import neo4j_database_ctx

    neo4j_db = neo4j_database_ctx.get()
    def _call() -> Any:
        token = None
        if neo4j_db:
            token = neo4j_database_ctx.set(neo4j_db)
        try:
            return func(**kwargs)
        finally:
            if token is not None:
                neo4j_database_ctx.reset(token)

    if governor is not None:
        operation = f"context.pipeline.{getattr(func, '__name__', 'step')}"
        return await governor.run(BlockingPool.GRAPH, operation, _call)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _call)


# =========================================================================== #
# Internal pipeline orchestration (search_context)
# =========================================================================== #

def _run_pipeline_front(
    *,
    driver: Any,
    query: str,
    db_id: str,
    domain: str = "",
) -> dict[str, Any]:
    """Run topology pipeline steps 0b–4: anchors, strategy cards, decision, traversal.

    Returns a dict with keys: anchors, subgraph, decision, cards_visible,
    cards_blocked, top_card_gate, facets, pipeline_steps.
    """
    from .ctx_trace import (
        anchor_bucket_counts,
        cards_trace_summary,
        decision_trace_summary,
        subgraph_trace_summary,
        _anchor_rows,
    )
    from ..embedder import embed_one
    from ..config import CFG, llm_thinking_enabled
    from ..openai_client import resolve_llm_model
    from ..runtime.anchors import resolve_anchors_multi
    from ..runtime.decision_llm import DecisionOutput, decide_with_path, estimate_task_type
    from ..runtime.path_pick_llm import (
        gather_candidate_edges,
        resolve_paths_steps,
        subgraph_from_candidate_edges,
        subgraph_from_picked_paths,
        union_subgraphs,
        expand_traversal_induced_edges,
    )
    from ..runtime.traversal import subgraph_from_card, weighted_bfs_fallback
    from ..graph.strategy_card import StrategyCardRetriever, gate_strategy_cards_for_llm
    from ..runtime.semantic_split import merge_strategy_card_candidates

    from concurrent.futures import ThreadPoolExecutor
    from ..runtime.retrieval_preprocess import build_anchor_recall_queries
    from ..runtime.rerank import rerank_anchor_set

    pipeline_steps: list[dict[str, Any]] = []
    wall_t0 = time.perf_counter()

    # Steps 0b + 1a: semantic split (→ facets) and entity/alias expansion
    # (→ recall queries) are two independent LLM calls on the raw query. Run
    # them concurrently so we pay one LLM latency instead of two serially.
    def _do_semantic_split() -> tuple[list[str], dict[str, Any]]:
        if not CFG.llm_semantic_split:
            return [query.strip()], {"enabled": False}
        try:
            from ..runtime.semantic_split import semantic_split_for_retrieval
            from ..context_config import RetrievalConfig
            split_facets, meta = semantic_split_for_retrieval(
                query, RetrievalConfig(), model=resolve_llm_model(),
                domain=domain,
            )
            return (split_facets or [query.strip()]), meta
        except Exception as exc:
            log.debug("semantic_split skipped: %s", exc)
            return [query.strip()], {"error": str(exc)}

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as _ex:
        _fut_split = _ex.submit(_do_semantic_split)
        _fut_expand = _ex.submit(build_anchor_recall_queries, query, model=resolve_llm_model(), domain=domain)
        facets, semantic_meta = _fut_split.result()
        anchor_recall_queries, ee_meta = _fut_expand.result()
    pipeline_steps.append({
        "step": 0,
        "id": "semantic_split",
        "title": "语义拆分（检索 facet）",
        "ms": int((time.perf_counter() - t0) * 1000),
        "summary": f"{len(facets)} 条 facet" + ("（含 LLM 拆分）" if len(facets) > 1 else "（原问句）"),
        "detail": {"facets": facets, "meta": semantic_meta},
    })

    # Step 1: anchors (multi-query recall using the expanded queries above)
    t0 = time.perf_counter()
    exact_match_terms = ee_meta.get("exact_match_terms") if ee_meta else None
    anchors = resolve_anchors_multi(
        driver,
        anchor_recall_queries,
        primary_question=query.strip(),
        db_id=db_id,
        embedder=embed_one,
        k_fulltext=CFG.recall_anchor_k_fulltext,
        k_vector=CFG.recall_anchor_k_vector,
        knowledge_merged_cap=CFG.recall_anchor_knowledge_merge_cap,
        knowledge_score_scale=CFG.recall_anchor_knowledge_score_scale,
        domain=domain,
        exact_match_terms=exact_match_terms,
    )
    # Rerank reorders/annotates ``anchors`` (precision pass). It's independent of
    # strategy-card recall (step 2 doesn't read anchors), so run it concurrently
    # and join before step 3 (gather_candidate_edges consumes anchors). This hides
    # the rerank LLM latency behind the strategy-card Neo4j recall.
    _rerank_ex = ThreadPoolExecutor(max_workers=1)
    _rerank_future = _rerank_ex.submit(
        rerank_anchor_set, query.strip(), anchors, embedder=embed_one,
    )
    anchors_step = {
        "step": 1,
        "id": "anchors",
        "title": "锚点召回（全文 + 向量 RRF）",
        "ms": int((time.perf_counter() - t0) * 1000),
        "summary": f"共 {anchor_bucket_counts(anchors)['total']} 个锚点",
        "detail": {
            "buckets": anchor_bucket_counts(anchors),
            "time_hints": list(getattr(anchors, "time_hints", []) or []),
            "top_anchors": _anchor_rows(anchors, limit=16),
            "recall_queries": anchor_recall_queries,
            "entity_expand": ee_meta,
            "rerank": {"deferred": True},
        },
    }
    pipeline_steps.append(anchors_step)

    # Step 2: strategy cards
    t0 = time.perf_counter()
    card_retriever = StrategyCardRetriever(driver)
    per_facet_cards: list[list[dict[str, Any]]] = []
    for facet in facets:
        fq = (facet or "").strip()
        if not fq:
            continue
        try:
            qemb = embed_one(fq)
        except Exception:
            qemb = []
        per_facet_cards.append(
            card_retriever.recall_top_k(
                qemb,
                task_type=None,
                graph_db_id=(db_id or "").strip(),
                k=CFG.recall_strategy_card_top_k,
                allow_avoid=True,
            )
        )
    if not per_facet_cards:
        try:
            qemb = embed_one(query.strip())
        except Exception:
            qemb = []
        per_facet_cards = [
            card_retriever.recall_top_k(
                qemb,
                task_type=None,
                graph_db_id=(db_id or "").strip(),
                k=CFG.recall_strategy_card_top_k,
                allow_avoid=True,
            )
        ]
    candidate_cards_recalled = merge_strategy_card_candidates(per_facet_cards)
    candidate_cards, cards_blocked = gate_strategy_cards_for_llm(
        candidate_cards_recalled,
        accept_threshold=CFG.recall_strategy_card_auto_accept_threshold,
    )
    card_decision_hint = card_retriever.top_card_decision(
        candidate_cards,
        accept_threshold=CFG.recall_strategy_card_auto_accept_threshold,
        accept_gap=CFG.recall_strategy_card_auto_accept_gap,
    )
    pipeline_steps.append({
        "step": 2,
        "id": "strategy_cards",
        "title": "策略卡 ANN 召回 + Gate",
        "ms": int((time.perf_counter() - t0) * 1000),
        "summary": (
            f"召回 {len(candidate_cards_recalled)} → 可见 {len(candidate_cards)} / "
            f"屏蔽 {len(cards_blocked)}"
        ),
        "detail": cards_trace_summary(
            len(candidate_cards_recalled), candidate_cards, cards_blocked, card_decision_hint
        ),
    })

    # Join the deferred rerank (started before step 2) — step 3 reads anchors.
    try:
        rerank_meta = _rerank_future.result()
    except Exception as exc:  # noqa: BLE001
        log.debug("rerank failed: %s", exc)
        rerank_meta = {"enabled": True, "error": str(exc)}
    finally:
        _rerank_ex.shutdown(wait=False)
    anchors_step["detail"]["rerank"] = rerank_meta

    # Step 3: decision + paths
    t0 = time.perf_counter()
    trav_dir = CFG.recall_traversal_edge_direction
    cand_edges = gather_candidate_edges(
        driver,
        anchors,
        max_anchors=CFG.recall_candidate_edges_max_anchors,
        per_node_limit=CFG.recall_candidate_edges_per_node_limit,
        max_total_edges=CFG.recall_candidate_edges_max_total_edges,
        max_hops=CFG.recall_decision_candidate_edge_hops,
        edge_direction=trav_dir,
    )
    est_tt = estimate_task_type(query)

    decision: Optional[Any] = None
    picked_paths: list[Any] = []

    if CFG.llm_decision:
        try:
            decision, picked_paths = decide_with_path(
                query,
                anchors,
                candidate_cards,
                candidate_edges=cand_edges,
                estimated_task_type=est_tt,
                candidate_edge_hops=CFG.recall_decision_candidate_edge_hops,
                edge_direction=trav_dir,
                max_path_edges=CFG.recall_decision_max_path_edges,
                # L1 search_context only uses the decision to pick subgraph paths
                # for schema_prompt assembly (not for SQL planning).
                model=resolve_llm_model(),
                reuse_confirmed=card_decision_hint["auto_accept"],
                enable_thinking=llm_thinking_enabled("decision"),
            )
        except Exception as exc:
            log.warning("decide_with_path failed: %s", exc)
            decision = DecisionOutput(
                task_type=est_tt,
                reuse_key=None,
                card_confidence=0.0,
                card_reason="llm decision failed",
                negative_hints=[],
                llm_calls=0,
            )
            picked_paths = resolve_paths_steps(cand_edges, anchors, CFG.recall_decision_max_path_edges, [])
    else:
        decision = DecisionOutput(
            task_type=est_tt,
            reuse_key=None,
            card_confidence=0.0,
            card_reason="decision LLM disabled",
            negative_hints=[],
            llm_calls=0,
        )
        picked_paths = resolve_paths_steps(cand_edges, anchors, CFG.recall_decision_max_path_edges, [])

    pipeline_steps.append({
        "step": 3,
        "id": "decision",
        "title": "决策 LLM（task_type + 路径）",
        "ms": int((time.perf_counter() - t0) * 1000),
        "summary": (
            f"task_type={getattr(decision, 'task_type', '')} "
            f"reuse={getattr(decision, 'reuse_key', None) or '—'}"
        ),
        "detail": {
            **decision_trace_summary(decision),
            "candidate_edges": len(cand_edges),
            "picked_paths_n": len(picked_paths or []),
        },
    })

    # Step 4: traversal
    t0 = time.perf_counter()
    sg_two_hop = subgraph_from_candidate_edges(cand_edges)
    sg_card = None
    matched_card = None
    if decision and decision.reuse_key:
        matched_card = next((c for c in candidate_cards if c.get("key") == decision.reuse_key), None)
        if matched_card:
            sg_card = subgraph_from_card(matched_card)

    sg_paths = None
    if picked_paths:
        try:
            sg_paths = subgraph_from_picked_paths(driver, picked_paths)
        except Exception as exc:
            log.debug("subgraph_from_picked_paths failed: %s", exc)

    topology_subgraph = union_subgraphs([sg_two_hop, sg_card, sg_paths], method="traversal_union")

    if not topology_subgraph or not topology_subgraph.has_results():
        from ..context_config import RetrievalConfig
        cfg = RetrievalConfig()
        topology_subgraph = weighted_bfs_fallback(
            driver,
            anchors,
            max_depth=cfg.traversal_fallback_max_depth,
            max_nodes=cfg.traversal_fallback_max_nodes,
            edge_direction=trav_dir,
        )

    trav_method = getattr(topology_subgraph, "method", "none") if topology_subgraph else "none"
    if topology_subgraph and topology_subgraph.has_results():
        try:
            topology_subgraph = expand_traversal_induced_edges(driver, topology_subgraph, edge_direction=trav_dir)
        except Exception as exc:
            log.debug("expand_traversal_induced_edges failed: %s", exc)

    pipeline_steps.append({
        "step": 4,
        "id": "traversal",
        "title": "子图游历（两跳 ∪ 卡 ∪ 路径 / BFS 兜底）",
        "ms": int((time.perf_counter() - t0) * 1000),
        "summary": f"method={trav_method} nodes={subgraph_trace_summary(topology_subgraph).get('nodes', 0)}",
        "detail": {
            **subgraph_trace_summary(topology_subgraph),
            "components": {
                "two_hop": bool(sg_two_hop and sg_two_hop.has_results()),
                "strategy_card": bool(sg_card and sg_card.has_results()),
                "llm_paths": bool(sg_paths and sg_paths.has_results()),
            },
        },
    })

    # Expand top-N metric anchors. Each expand opens its own Neo4j session
    # (neo4j_session), so the 4 lookups are independent — fan them out across
    # threads instead of paying 4 round-trips serially.
    t0 = time.perf_counter()
    expanded_subgraphs: dict[str, dict[str, Any]] = {}
    try:
        from .retrieval import expand_subgraph as _expand_sg
        metric_keys = [
            mk for a in (list(getattr(anchors, "anchors_metric", []) or [])[:4])
            if (mk := getattr(a, "key", ""))
        ]

        def _safe_expand(_mk: str) -> tuple[str, Optional[dict[str, Any]]]:
            try:
                return _mk, _expand_sg(driver, _mk)
            except Exception:
                return _mk, None

        if metric_keys:
            with ThreadPoolExecutor(max_workers=len(metric_keys)) as _ex:
                for _mk, _sg in _ex.map(_safe_expand, metric_keys):
                    if _sg is not None:
                        expanded_subgraphs[_mk] = _sg
    except Exception as exc:
        log.debug("expand_subgraph skipped: %s", exc)

    pipeline_steps.append({
        "step": 5,
        "id": "expand_metrics",
        "title": "Top 指标 expand_subgraph（MG 语义补全）",
        "ms": int((time.perf_counter() - t0) * 1000),
        "summary": f"展开 {len(expanded_subgraphs)} 个 metric 邻域",
        "detail": {"metric_keys": list(expanded_subgraphs.keys())},
    })
    pipeline_steps.append({
        "step": 6,
        "id": "assemble",
        "title": "组装 ContextPack（见响应 operation.assembly）",
        "ms": 0,
        "summary": "锚点 + 子图 + 经验卡 → SemanticsBlock / ExperienceBlock",
        "detail": {"total_pipeline_ms": int((time.perf_counter() - wall_t0) * 1000)},
    })

    return {
        "anchors": anchors,
        "subgraph": topology_subgraph,
        "decision": decision,
        "cards_visible": candidate_cards,
        "cards_blocked": cards_blocked,
        "top_card_gate": card_decision_hint,
        "facets": facets,
        "expanded_subgraphs": expanded_subgraphs,
        "pipeline_steps": pipeline_steps,
        "cards_recalled_n": len(candidate_cards_recalled),
    }


# --------------------------------------------------------------------------- #
# Async generator variant — yields SSE progress per pipeline step
# --------------------------------------------------------------------------- #

async def _run_pipeline_front_sse(
    *,
    driver: Any,
    query: str,
    db_id: str,
    result_holder: Optional[dict[str, Any]] = None,
    domain: str = "",
    governor: BlockingIOGovernor | None = None,
):
    """Async generator version of _run_pipeline_front; yields SSE frames per step.

    Yields SSE-formatted strings. The last event is ``event: result`` with the
    same dict shape as _run_pipeline_front's return value (serialised to JSON).

    If *result_holder* is provided, the raw Python objects (anchors, subgraph,
    decision, …) are stored there **before** the SSE ``event: result`` is
    emitted.  Callers should read from *result_holder* instead of parsing the
    JSON-serialised SSE payload, because ``json.dumps(default=str)`` turns
    non-dict objects into repr strings.
    """
    from .ctx_trace import (
        anchor_bucket_counts,
        cards_trace_summary,
        decision_trace_summary,
        subgraph_trace_summary,
        _anchor_rows,
    )
    from ..embedder import embed_one
    from ..config import CFG, llm_thinking_enabled
    from ..openai_client import resolve_llm_model
    from ..runtime.anchors import resolve_anchors_multi
    from ..runtime.decision_llm import (
        DecisionOutput, decide_with_path, decide_with_path_react, estimate_task_type,
    )
    from ..runtime.path_pick_llm import (
        gather_candidate_edges,
        resolve_paths_steps,
        subgraph_from_candidate_edges,
        subgraph_from_picked_paths,
        union_subgraphs,
        expand_traversal_induced_edges,
    )
    from ..runtime.traversal import subgraph_from_card, weighted_bfs_fallback
    from ..graph.strategy_card import StrategyCardRetriever, gate_strategy_cards_for_llm
    from ..runtime.semantic_split import merge_strategy_card_candidates

    async def _run_step(func, **kwargs):
        return await _run_in_executor(func, governor=governor, **kwargs)

    pipeline_steps: list[dict[str, Any]] = []
    wall_t0 = time.perf_counter()
    total_steps = 7

    def _progress_event(step: int, step_id: str, title: str, ms: int, summary: str, detail: Any = None):
        step_record = {
            "step": step,
            "id": step_id,
            "title": title,
            "ms": ms,
            "summary": summary,
        }
        if detail is not None:
            step_record["detail"] = detail
        pipeline_steps.append({**step_record, **({"detail": detail} if detail is not None else {})})
        return _sse_event("progress", {
            "step": step,
            "total_steps": total_steps,
            "id": step_id,
            "title": title,
            "ms": ms,
            "summary": summary,
        })

    yield _sse_event("start", {"query": query, "db_id": db_id, "total_steps": total_steps})

    # Step 0: semantic split
    facets: list[str] = [query.strip()]
    semantic_meta: dict[str, Any] = {"enabled": bool(CFG.llm_semantic_split)}
    t0 = time.perf_counter()
    if CFG.llm_semantic_split:
        try:
            from ..runtime.semantic_split import semantic_split_for_retrieval
            from ..context_config import RetrievalConfig
            cfg = RetrievalConfig()
            split_facets, semantic_meta = await _run_step(
                semantic_split_for_retrieval, effective_retrieval_q=query, cfg=cfg, model=resolve_llm_model(),
                domain=domain,
            )
            if split_facets:
                facets = split_facets
        except BlockingIOError:
            raise
        except Exception as exc:
            log.debug("semantic_split skipped: %s", exc)
            semantic_meta = {"error": str(exc)}
    ms0 = int((time.perf_counter() - t0) * 1000)
    yield _progress_event(0, "semantic_split", "语义拆分（检索 facet）", ms0,
        f"{len(facets)} 条 facet" + ("（含 LLM 拆分）" if len(facets) > 1 else "（原问句）"),
        {"facets": facets, "meta": semantic_meta})

    # Step 1: anchors (single-turn entity/alias expansion → multi-query recall)
    from ..runtime.retrieval_preprocess import build_anchor_recall_queries
    from ..runtime.rerank import rerank_anchor_set

    anchor_recall_queries, ee_meta = await _run_step(
        build_anchor_recall_queries, query=query, model=resolve_llm_model(),
        domain=domain,
    )
    t0 = time.perf_counter()
    exact_match_terms = ee_meta.get("exact_match_terms") if ee_meta else None
    anchors = await _run_step(
        resolve_anchors_multi,
        driver=driver,
        queries=anchor_recall_queries,
        primary_question=query.strip(),
        db_id=db_id,
        embedder=embed_one,
        k_fulltext=CFG.recall_anchor_k_fulltext,
        k_vector=CFG.recall_anchor_k_vector,
        knowledge_merged_cap=CFG.recall_anchor_knowledge_merge_cap,
        knowledge_score_scale=CFG.recall_anchor_knowledge_score_scale,
        domain=domain,
        exact_match_terms=exact_match_terms,
    )
    rerank_meta = await _run_step(
        rerank_anchor_set, query=query.strip(), anchors=anchors, embedder=embed_one,
    )
    ms1 = int((time.perf_counter() - t0) * 1000)
    yield _progress_event(1, "anchors", "锚点召回（全文 + 向量 RRF）", ms1,
        f"共 {anchor_bucket_counts(anchors)['total']} 个锚点",
        {"buckets": anchor_bucket_counts(anchors),
         "time_hints": list(getattr(anchors, "time_hints", []) or []),
         "top_anchors": _anchor_rows(anchors, limit=16),
         "recall_queries": anchor_recall_queries,
         "entity_expand": ee_meta,
         "rerank": rerank_meta})

    # Step 2: strategy cards
    t0 = time.perf_counter()
    card_retriever = StrategyCardRetriever(driver)
    per_facet_cards: list[list[dict[str, Any]]] = []
    for facet in facets:
        fq = (facet or "").strip()
        if not fq:
            continue
        try:
            qemb = await _run_step(embed_one, text=fq)
        except BlockingIOError:
            raise
        except Exception:
            qemb = []
        per_facet_cards.append(
            await _run_step(
                card_retriever.recall_top_k,
                query_emb=qemb, task_type=None,
                graph_db_id=(db_id or "").strip(),
                k=CFG.recall_strategy_card_top_k, allow_avoid=True,
            )
        )
    if not per_facet_cards:
        try:
            qemb = await _run_step(embed_one, text=query.strip())
        except BlockingIOError:
            raise
        except Exception:
            qemb = []
        per_facet_cards = [
            await _run_step(
                card_retriever.recall_top_k,
                query_emb=qemb, task_type=None,
                graph_db_id=(db_id or "").strip(),
                k=CFG.recall_strategy_card_top_k, allow_avoid=True,
            )
        ]
    candidate_cards_recalled = merge_strategy_card_candidates(per_facet_cards)
    candidate_cards, cards_blocked = gate_strategy_cards_for_llm(
        candidate_cards_recalled,
        accept_threshold=CFG.recall_strategy_card_auto_accept_threshold,
    )
    card_decision_hint = card_retriever.top_card_decision(
        candidate_cards,
        accept_threshold=CFG.recall_strategy_card_auto_accept_threshold,
        accept_gap=CFG.recall_strategy_card_auto_accept_gap,
    )
    ms2 = int((time.perf_counter() - t0) * 1000)
    yield _progress_event(2, "strategy_cards", "策略卡 ANN 召回 + Gate", ms2,
        f"召回 {len(candidate_cards_recalled)} → 可见 {len(candidate_cards)} / 屏蔽 {len(cards_blocked)}",
        cards_trace_summary(len(candidate_cards_recalled), candidate_cards, cards_blocked, card_decision_hint))

    # Step 3: decision + paths
    t0 = time.perf_counter()
    trav_dir = CFG.recall_traversal_edge_direction
    cand_edges = await _run_step(
        gather_candidate_edges,
        driver=driver, anchors=anchors,
        max_anchors=CFG.recall_candidate_edges_max_anchors,
        per_node_limit=CFG.recall_candidate_edges_per_node_limit,
        max_total_edges=CFG.recall_candidate_edges_max_total_edges,
        max_hops=CFG.recall_decision_candidate_edge_hops,
        edge_direction=trav_dir,
    )
    est_tt = estimate_task_type(query)

    decision: Optional[Any] = None
    picked_paths: list[Any] = []

    if CFG.llm_decision:
        try:
            if CFG.recall_decision_react_max_rounds > 1:
                # ReAct runs in an executor thread; bridge its per-round callback
                # to SSE frames via a thread-safe asyncio.Queue drained here.
                _loop = asyncio.get_running_loop()
                _react_q: asyncio.Queue = asyncio.Queue()

                def _react_cb(_rid, _d, _q=_react_q, _l=_loop):
                    _l.call_soon_threadsafe(_q.put_nowait, (_rid, _d))

                _react_task = asyncio.ensure_future(_run_step(
                    decide_with_path_react,
                    question=query,
                    anchors=anchors,
                    candidate_cards=candidate_cards,
                    driver=driver,
                    candidate_edges=cand_edges,
                    estimated_task_type=est_tt,
                    candidate_edge_hops=CFG.recall_decision_candidate_edge_hops,
                    edge_direction=trav_dir,
                    max_path_edges=CFG.recall_decision_max_path_edges,
                    max_rounds=CFG.recall_decision_react_max_rounds,
                    expand_new_edges_cap=CFG.recall_decision_react_expand_new_edges_cap,
                    max_total_edges=CFG.recall_candidate_edges_max_total_edges,
                    per_node_limit=CFG.recall_candidate_edges_per_node_limit,
                    # L1 path-picking only feeds schema_prompt assembly.
                    model=resolve_llm_model(),
                    reuse_confirmed=card_decision_hint["auto_accept"],
                    enable_thinking=llm_thinking_enabled("decision"),
                    react_progress_cb=_react_cb,
                ))
                while not _react_task.done():
                    try:
                        _rid, _d = await asyncio.wait_for(_react_q.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue
                    yield _sse_event("decision_react",
                        {"step": 3, "round": _rid, **(_d if isinstance(_d, dict) else {})})
                while not _react_q.empty():
                    _rid, _d = _react_q.get_nowait()
                    yield _sse_event("decision_react",
                        {"step": 3, "round": _rid, **(_d if isinstance(_d, dict) else {})})
                decision, picked_paths = await _react_task
            else:
                decision, picked_paths = await _run_step(
                    decide_with_path,
                    query=query, anchors=anchors, candidate_cards=candidate_cards,
                    candidate_edges=cand_edges, estimated_task_type=est_tt,
                    candidate_edge_hops=CFG.recall_decision_candidate_edge_hops,
                    edge_direction=trav_dir,
                    max_path_edges=CFG.recall_decision_max_path_edges,
                    # L1 path-picking only feeds schema_prompt assembly.
                    model=resolve_llm_model(),
                    reuse_confirmed=card_decision_hint["auto_accept"],
                    enable_thinking=llm_thinking_enabled("decision"),
                )
        except BlockingIOError:
            raise
        except Exception as exc:
            log.warning("decide_with_path failed: %s", exc)
            decision = DecisionOutput(
                task_type=est_tt, reuse_key=None, card_confidence=0.0,
                card_reason="llm decision failed", negative_hints=[], llm_calls=0,
            )
            picked_paths = resolve_paths_steps(cand_edges, anchors, CFG.recall_decision_max_path_edges, [])
    else:
        decision = DecisionOutput(
            task_type=est_tt, reuse_key=None, card_confidence=0.0,
            card_reason="decision LLM disabled", negative_hints=[], llm_calls=0,
        )
        picked_paths = resolve_paths_steps(cand_edges, anchors, CFG.recall_decision_max_path_edges, [])

    ms3 = int((time.perf_counter() - t0) * 1000)
    yield _progress_event(3, "decision", "决策 LLM（task_type + 路径）", ms3,
        f"task_type={getattr(decision, 'task_type', '')} reuse={getattr(decision, 'reuse_key', None) or '—'}",
        {**decision_trace_summary(decision),
         "candidate_edges": len(cand_edges),
         "picked_paths_n": len(picked_paths or [])})

    # Step 4: traversal
    t0 = time.perf_counter()
    sg_two_hop = subgraph_from_candidate_edges(cand_edges)
    sg_card = None
    if decision and decision.reuse_key:
        matched_card = next((c for c in candidate_cards if c.get("key") == decision.reuse_key), None)
        if matched_card:
            sg_card = subgraph_from_card(matched_card)

    sg_paths = None
    if picked_paths:
        try:
            sg_paths = await _run_step(
                subgraph_from_picked_paths, driver=driver, picked_paths=picked_paths,
            )
        except BlockingIOError:
            raise
        except Exception as exc:
            log.debug("subgraph_from_picked_paths failed: %s", exc)

    topology_subgraph = union_subgraphs([sg_two_hop, sg_card, sg_paths], method="traversal_union")

    if not topology_subgraph or not topology_subgraph.has_results():
        from ..context_config import RetrievalConfig
        cfg = RetrievalConfig()
        topology_subgraph = await _run_step(
            weighted_bfs_fallback,
            driver=driver, anchors=anchors,
            max_depth=cfg.traversal_fallback_max_depth,
            max_nodes=cfg.traversal_fallback_max_nodes,
            edge_direction=trav_dir,
        )

    trav_method = getattr(topology_subgraph, "method", "none") if topology_subgraph else "none"
    if topology_subgraph and topology_subgraph.has_results():
        try:
            topology_subgraph = await _run_step(
                expand_traversal_induced_edges,
                driver=driver, subgraph=topology_subgraph, edge_direction=trav_dir,
            )
        except BlockingIOError:
            raise
        except Exception as exc:
            log.debug("expand_traversal_induced_edges failed: %s", exc)

    ms4 = int((time.perf_counter() - t0) * 1000)
    yield _progress_event(4, "traversal", "子图游历（两跳 ∪ 卡 ∪ 路径 / BFS 兜底）", ms4,
        f"method={trav_method} nodes={subgraph_trace_summary(topology_subgraph).get('nodes', 0)}",
        {**subgraph_trace_summary(topology_subgraph),
         "components": {
             "two_hop": bool(sg_two_hop and sg_two_hop.has_results()),
             "strategy_card": bool(sg_card and sg_card.has_results()),
             "llm_paths": bool(sg_paths and sg_paths.has_results()),
         }})

    # Step 5: expand top-N metric anchors
    t0 = time.perf_counter()
    expanded_subgraphs: dict[str, dict[str, Any]] = {}
    try:
        from .retrieval import expand_subgraph as _expand_sg
        metric_keys = [
            mk for a in (list(getattr(anchors, "anchors_metric", []) or [])[:4])
            if (mk := getattr(a, "key", ""))
        ]

        async def _safe_expand(_mk: str) -> tuple[str, Optional[dict[str, Any]]]:
            try:
                return _mk, await _run_step(
                    _expand_sg, driver=driver, metric_key=_mk,
                )
            except BlockingIOError:
                raise
            except Exception:
                return _mk, None

        if metric_keys:
            for _mk, _sg in await asyncio.gather(*(_safe_expand(k) for k in metric_keys)):
                if _sg is not None:
                    expanded_subgraphs[_mk] = _sg
    except BlockingIOError:
        raise
    except Exception as exc:
        log.debug("expand_subgraph skipped: %s", exc)

    ms5 = int((time.perf_counter() - t0) * 1000)
    yield _progress_event(5, "expand_metrics", "Top 指标 expand_subgraph（MG 语义补全）", ms5,
        f"展开 {len(expanded_subgraphs)} 个 metric 邻域",
        {"metric_keys": list(expanded_subgraphs.keys())})

    # Step 6: assemble (logical — actual assembly is done by the caller)
    total_ms = int((time.perf_counter() - wall_t0) * 1000)
    yield _progress_event(6, "assemble", "组装 ContextPack（见响应 operation.assembly）", 0,
        "锚点 + 子图 + 经验卡 → SemanticsBlock / ExperienceBlock",
        {"total_pipeline_ms": total_ms})

    # Store raw Python objects for callers (bypasses json.dumps(default=str) serialisation)
    raw_result = {
        "anchors": anchors,
        "subgraph": topology_subgraph,
        "decision": decision,
        "cards_visible": candidate_cards,
        "cards_blocked": cards_blocked,
        "top_card_gate": card_decision_hint,
        "facets": facets,
        "expanded_subgraphs": expanded_subgraphs,
        "pipeline_steps": pipeline_steps,
        "cards_recalled_n": len(candidate_cards_recalled),
    }
    if result_holder is not None:
        result_holder.update(raw_result)

    # Emit final result (for SSE consumers; content is lossy due to default=str)
    yield _sse_event("result", raw_result)


# =========================================================================== #

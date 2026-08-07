"""候选边收集与路径校验（具体边由合并决策 LLM 一并产出）。

候选边 = 锚点若干层 **BFS 邻域**（方向由 ``CFG.recall_traversal_edge_direction`` /
``recall.traversal_edge_direction`` 控制：出边 / 入边 / 双向）；不按 task_type 筛关系类型。
层数由 ``max_hops`` 控制。ReAct 决策可在多轮中从指定节点再扩一层，见
:func:`gather_out_edges_from_keys`。
合并决策 LLM 在 prompt 中直接看到这批候选并输出具体边路径，下游
:func:`context_manager.runtime.decision_llm.decide_with_path` 等负责调用与校验。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional

from neo4j import Driver

from ..config import TraversalEdgeDirection, normalize_traversal_edge_direction
from ..utils import get_logger, neo4j_session
from .anchors import AnchorSet
from .traversal import TraversalSubgraph, cypher_relation_priority_case

log = get_logger("runtime.path_pick")


def fair_neighbor_edge_limit(
    remaining: int,
    nodes_left: int,
    per_node_limit: int,
) -> int:
    """Split *remaining* global edge budget evenly across *nodes_left* frontier nodes."""
    if remaining <= 0 or nodes_left <= 0:
        return 0
    return min(per_node_limit, max(1, remaining // nodes_left))


def traversal_edge_direction_label(
    direction: TraversalEdgeDirection,
    *,
    plural: bool = True,
) -> str:
    """人类可读方向短语（用于 pipeline 摘要与决策 prompt）。"""
    if direction == "in":
        return "inward hops" if plural else "inward hop"
    if direction == "both":
        return "outward and inward hops" if plural else "outward and inward hop"
    return "outward hops" if plural else "outward hop"


def candidate_group_header_label(direction: TraversalEdgeDirection) -> str:
    if direction == "in":
        return "inward"
    if direction == "both":
        return "neighbor"
    return "outward"


@dataclass
class CandidateEdge:
    hop: int
    eid: int
    from_key: str
    rel_type: str
    to_key: str
    from_label: str
    to_label: str


def gather_candidate_edges(
    driver: Driver,
    anchors: AnchorSet,
    *,
    max_anchors: int = 16,
    per_node_limit: int = 80,
    max_total_edges: int = 400,
    max_hops: int = 2,
    edge_direction: TraversalEdgeDirection = "out",
) -> list[CandidateEdge]:
    """从 top-N 锚点出发做 **最多 max_hops 层** BFS，收集全部边（不按关系类型筛）。

    ``edge_direction``：``out`` 只走 ``(a)-[r]->(b)``；``in`` 只走入边（存储为图上
    真实 ``from→to``）；``both`` 每波次同时扩两个方向。

    ``CandidateEdge.hop`` 为从锚点出发的 BFS 波次索引（0=锚点直连）。只保留
    ``valid_to`` 时态约束。
    """
    edge_direction = normalize_traversal_edge_direction(edge_direction)
    if not anchors.anchors:
        return []

    sorted_anchors = sorted(anchors.anchors, key=lambda a: -a.score)
    hop0_keys = [a.key for a in sorted_anchors[:max_anchors] if a.key]
    if not hop0_keys:
        return []

    layers = max(1, int(max_hops))
    out: list[CandidateEdge] = []
    seen_edges: set[tuple[str, str, str]] = set()
    labels_by_key: dict[str, str] = {a.key: a.label for a in anchors.anchors}
    eid_counter = 0

    def _emit(hop: int, fk: str, rel: str, tk: str, flab: str, tlab: str) -> bool:
        """Append one edge if unseen; return False once max_total_edges reached."""
        nonlocal eid_counter
        if not (fk and rel and tk):
            return True
        t = (fk, rel, tk)
        if t in seen_edges:
            return True
        seen_edges.add(t)
        out.append(
            CandidateEdge(
                hop=hop,
                eid=eid_counter,
                from_key=fk,
                rel_type=rel,
                to_key=tk,
                from_label=flab,
                to_label=tlab,
            )
        )
        eid_counter += 1
        return len(out) < max_total_edges

    frontier = list(hop0_keys)
    scheduled: set[str] = set(hop0_keys)

    with neo4j_session(driver) as s:
        for wave in range(layers):
            next_frontier: list[str] = []
            for idx, fk in enumerate(frontier):
                remaining = max_total_edges - len(out)
                if remaining <= 0:
                    return out
                node_budget = fair_neighbor_edge_limit(
                    remaining,
                    len(frontier) - idx,
                    per_node_limit,
                )
                rows = _query_neighbor_edges_session(
                    s, fk, node_budget, edge_direction=edge_direction
                )
                for row in rows:
                    ek = row["fk"]
                    tk = row["tk"]
                    rel = row["rel"]
                    tlab = row["tlab"]
                    flab = row["flab"] or labels_by_key.get(ek, "")
                    still_room = _emit(wave, ek, rel, tk, flab, tlab)
                    for nk in (ek, tk):
                        if nk:
                            labels_by_key[nk] = tlab if nk == tk else flab
                    neighbor = row.get("neighbor")
                    if neighbor and neighbor not in scheduled:
                        scheduled.add(neighbor)
                        next_frontier.append(neighbor)
                    if not still_room:
                        return out
            frontier = next_frontier
            if not frontier:
                break

    return out


def renumber_candidate_edge_ids(edges: list[CandidateEdge]) -> None:
    for i, e in enumerate(edges):
        e.eid = i


def merge_edges_dedupe(
    base: list[CandidateEdge],
    extra: list[CandidateEdge],
    *,
    seen: Optional[set[tuple[str, str, str]]] = None,
) -> None:
    """把 ``extra`` 中不在 ``seen`` / ``base`` 里的边追加到 ``base``，并更新 ``seen``。"""
    if seen is None:
        seen = {(e.from_key, e.rel_type, e.to_key) for e in base}
    for e in extra:
        t = (e.from_key, e.rel_type, e.to_key)
        if t in seen:
            continue
        seen.add(t)
        base.append(e)
    renumber_candidate_edge_ids(base)


def gather_out_edges_from_keys(
    driver: Driver,
    from_keys: list[str],
    *,
    labels_by_key: dict[str, str],
    seen_edges: set[tuple[str, str, str]],
    per_node_limit: int = 80,
    max_new_edges: int = 200,
    hop: int = 0,
    edge_direction: TraversalEdgeDirection = "out",
) -> list[CandidateEdge]:
    """从给定节点 key 各走一步邻边（方向同 ``edge_direction``）；跳过 ``seen_edges`` 中已有三元组。"""
    edge_direction = normalize_traversal_edge_direction(edge_direction)
    out: list[CandidateEdge] = []
    eid = 0
    blocked = set(seen_edges)
    keys = [fk for fk in from_keys if fk]
    with neo4j_session(driver) as s:
        for idx, fk in enumerate(keys):
            remaining = max_new_edges - len(out)
            if remaining <= 0:
                return out
            node_budget = fair_neighbor_edge_limit(
                remaining,
                len(keys) - idx,
                per_node_limit,
            )
            rows = _query_neighbor_edges_session(
                s, fk, node_budget, edge_direction=edge_direction
            )
            for row in rows:
                if len(out) >= max_new_edges:
                    return out
                ek = row["fk"]
                tk = row["tk"]
                rel = row["rel"]
                tlab = row["tlab"]
                flab = row["flab"] or labels_by_key.get(ek, "")
                if not (ek and rel and tk):
                    continue
                t = (ek, rel, tk)
                if t in blocked:
                    continue
                blocked.add(t)
                if ek:
                    labels_by_key[ek] = flab
                if tk:
                    labels_by_key[tk] = tlab
                out.append(
                    CandidateEdge(
                        hop=hop,
                        eid=eid,
                        from_key=ek,
                        rel_type=rel,
                        to_key=tk,
                        from_label=flab,
                        to_label=tlab,
                    )
                )
                eid += 1
    return out


def _query_neighbor_edges_session(
    session,
    center_key: str,
    limit: int,
    *,
    edge_direction: TraversalEdgeDirection = "out",
) -> list[dict[str, Any]]:
    """从 ``center_key`` 单步扩展；返回行含图上真实 ``fk→tk`` 与 BFS ``neighbor`` 下一跳 key。"""
    edge_direction = normalize_traversal_edge_direction(edge_direction)
    lim = max(1, int(limit))
    out: list[dict[str, Any]] = []
    if edge_direction == "both":
        out_lim = max(1, (lim + 1) // 2)
        in_lim = max(1, lim // 2)
        out.extend(_query_directed_edges_session(session, center_key, out_lim, outward=True))
        out.extend(_query_directed_edges_session(session, center_key, in_lim, outward=False))
    elif edge_direction == "in":
        out.extend(_query_directed_edges_session(session, center_key, lim, outward=False))
    else:
        out.extend(_query_directed_edges_session(session, center_key, lim, outward=True))
    return out


def _query_directed_edges_session(
    session,
    center_key: str,
    limit: int,
    *,
    outward: bool,
) -> list[dict[str, Any]]:
    rel_prio = cypher_relation_priority_case("type(r)")
    if outward:
        cypher = f"""
        MATCH (a {{key: $fk}})-[r]->(b)
        WHERE (b.valid_to IS NULL OR b.valid_to > datetime())
        WITH type(r) AS rel, a.key AS fk, b.key AS tk,
             labels(a) AS la, labels(b) AS lb,
             {rel_prio} AS rel_prio
        RETURN rel, fk, tk, la, lb
        ORDER BY rel_prio DESC, rel ASC, coalesce(tk, '') ASC
        LIMIT $lim
        """
    else:
        cypher = f"""
        MATCH (b)-[r]->(a {{key: $fk}})
        WHERE (b.valid_to IS NULL OR b.valid_to > datetime())
        WITH type(r) AS rel, b.key AS fk, a.key AS tk,
             labels(b) AS la, labels(a) AS lb,
             {rel_prio} AS rel_prio
        RETURN rel, fk, tk, la, lb
        ORDER BY rel_prio DESC, rel ASC, coalesce(fk, '') ASC
        LIMIT $lim
        """
    lim_i = max(1, int(limit))
    try:
        rows = session.run(cypher, fk=center_key, lim=lim_i).data()
    except Exception as exc:
        log.debug("neighbor expand failed for %s outward=%s: %s", center_key, outward, exc)
        return []
    if len(rows) >= lim_i:
        log.info(
            "neighbor edges capped center=%s outward=%s kept=%d limit=%d (ordered by rel priority)",
            center_key,
            outward,
            len(rows),
            lim_i,
        )
    parsed: list[dict[str, Any]] = []
    for row in rows:
        fk = row.get("fk")
        tk = row.get("tk")
        if not (fk and tk):
            continue
        la = [str(x) for x in (row.get("la") or [])]
        lb = [str(x) for x in (row.get("lb") or [])]
        neighbor = tk if outward else fk
        parsed.append(
            {
                "fk": fk,
                "tk": tk,
                "rel": row.get("rel"),
                "flab": la[0] if la else "",
                "tlab": lb[0] if lb else "",
                "neighbor": neighbor,
            }
        )
    return parsed


def subgraph_from_candidate_edges(
    cand_edges: list[CandidateEdge],
) -> TraversalSubgraph:
    """把候选边集合（锚点 BFS + ReAct 扩边并集）打包成 :class:`TraversalSubgraph`。"""
    keys: list[str] = []
    seen_key: set[str] = set()
    nodes_by_label: dict[str, list[str]] = {}
    edges_out: list[dict[str, str]] = []
    seen_edge: set[tuple[str, str, str]] = set()

    def _add_node(k: str, lab: str) -> None:
        if not k or k in seen_key:
            return
        seen_key.add(k)
        keys.append(k)
        if lab:
            bucket = nodes_by_label.setdefault(lab, [])
            if k not in bucket:
                bucket.append(k)

    for e in cand_edges:
        _add_node(e.from_key, e.from_label)
        _add_node(e.to_key, e.to_label)
        t = (e.from_key, e.rel_type, e.to_key)
        if t in seen_edge:
            continue
        seen_edge.add(t)
        edges_out.append({"from": e.from_key, "rel": e.rel_type, "to": e.to_key})

    return TraversalSubgraph(
        node_keys=keys,
        nodes_by_label=nodes_by_label,
        edges=edges_out,
        method="two_hop_neighborhood",
    )


def union_subgraphs(
    sgs: list[Optional[TraversalSubgraph]],
    *,
    method: str,
) -> Optional[TraversalSubgraph]:
    """节点 / 标签桶 / 边的并集；空输入返回 ``None``。"""
    keys: list[str] = []
    seen_key: set[str] = set()
    nbl: dict[str, list[str]] = {}
    edges: list[dict[str, str]] = []
    seen_edge: set[tuple[str, str, str]] = set()

    for sg in sgs:
        if not sg:
            continue
        for k in sg.node_keys or []:
            if k and k not in seen_key:
                seen_key.add(k)
                keys.append(k)
        for lab, ks in (sg.nodes_by_label or {}).items():
            bucket = nbl.setdefault(lab, [])
            in_bucket = set(bucket)
            for k in ks:
                if k and k not in in_bucket:
                    in_bucket.add(k)
                    bucket.append(k)
        for e in sg.edges or []:
            t = (
                str(e.get("from") or ""),
                str(e.get("rel") or ""),
                str(e.get("to") or ""),
            )
            if t in seen_edge:
                continue
            seen_edge.add(t)
            edges.append(e)

    if not keys:
        return None
    return TraversalSubgraph(
        node_keys=keys,
        nodes_by_label=nbl,
        edges=edges,
        method=method,
    )


def _candidate_key_set(cands: list[CandidateEdge]) -> set[tuple[str, str, str]]:
    return {(e.from_key, e.rel_type, e.to_key) for e in cands}


def _validate_path(
    path: list[dict[str, Any]],
    allowed: set[tuple[str, str, str]],
    *,
    max_len: int,
) -> bool:
    if not path:
        return False
    if len(path) > max_len:
        return False
    prev_to: Optional[str] = None
    for step in path:
        fk = str(step.get("from_key") or "")
        rel = str(step.get("relationship_type") or "")
        tk = str(step.get("to_key") or "")
        if (fk, rel, tk) not in allowed:
            return False
        if prev_to is not None and fk != prev_to:
            return False
        prev_to = tk
    return True


def _fallback_greedy_path(
    cands: list[CandidateEdge],
    max_path_len: int,
    anchor_order: list[str],
) -> list[dict[str, str]]:
    """确定性兜底：优先从高分锚点出发，沿候选出边贪心走一条 ≤ ``max_path_len`` 的链。"""
    by_from: dict[str, list[CandidateEdge]] = defaultdict(list)
    for e in cands:
        by_from[e.from_key].append(e)
    anchor_set = set(anchor_order)

    def walk_from(start_fk: str, depth: int) -> list[dict[str, str]]:
        cur = start_fk
        steps: list[dict[str, str]] = []
        for _ in range(depth):
            choices = by_from.get(cur) or []
            if not choices:
                return []
            pick = choices[0]
            steps.append(
                {
                    "from_key": pick.from_key,
                    "relationship_type": pick.rel_type,
                    "to_key": pick.to_key,
                }
            )
            cur = pick.to_key
        return steps

    for depth in range(max(max_path_len, 1), 0, -1):
        for ak in anchor_order:
            p = walk_from(ak, depth)
            if p:
                return p
        for e in cands:
            if e.from_key in anchor_set:
                p = walk_from(e.from_key, depth)
                if p:
                    return p
    return []


def _fill_nodes_by_label(
    driver: Driver,
    keys: list[str],
) -> dict[str, list[str]]:
    if not keys:
        return {}
    q = """
    UNWIND $keys AS k
    MATCH (n {key: k})
    RETURN n.key AS key, labels(n) AS lbs
    """
    nodes_by_label: dict[str, list[str]] = {}
    with neo4j_session(driver) as s:
        rows = s.run(q, keys=keys[:200]).data()
    for row in rows:
        k = row.get("key")
        lbs = [str(x) for x in (row.get("lbs") or [])]
        lab = lbs[0] if lbs else "Unknown"
        if k:
            nodes_by_label.setdefault(lab, []).append(k)
    return nodes_by_label


def _fmt_anchors(anchors: AnchorSet) -> str:
    parts = []
    for a in sorted(anchors.anchors, key=lambda x: -x.score)[:12]:
        parts.append(f"  {a.key} ({a.label}) score={a.score:.3f} name={a.name!r}")
    return "\n".join(parts) if parts else "  (none)"


def subgraph_from_picked_paths(
    driver: Driver,
    paths_steps: list[list[dict[str, str]]],
) -> TraversalSubgraph:
    """Merge multiple LLM-picked walks into one subgraph (union of nodes and edges)."""
    paths_steps = [p for p in paths_steps if p]
    if not paths_steps:
        return TraversalSubgraph(method="rule_llm_path")

    merged_keys: list[str] = []
    seen_key: set[str] = set()
    merged_nbl: dict[str, list[str]] = {}
    merged_edges: list[dict[str, str]] = []
    seen_edge: set[tuple[str, str, str]] = set()

    for ps in paths_steps:
        sg = subgraph_from_picked_path(driver, ps)
        for k in sg.node_keys:
            if k not in seen_key:
                seen_key.add(k)
                merged_keys.append(k)
        for lab, ks in (sg.nodes_by_label or {}).items():
            bucket = merged_nbl.setdefault(lab, [])
            seen_in_lab = set(bucket)
            for k in ks:
                if k not in seen_in_lab:
                    seen_in_lab.add(k)
                    bucket.append(k)
        for e in sg.edges or []:
            fk = str(e.get("from") or "")
            rel = str(e.get("rel") or "")
            tk = str(e.get("to") or "")
            t = (fk, rel, tk)
            if t not in seen_edge:
                seen_edge.add(t)
                merged_edges.append(e)

    return TraversalSubgraph(
        node_keys=merged_keys,
        nodes_by_label=merged_nbl,
        edges=merged_edges,
        method="rule_llm_path",
    )


def expand_traversal_induced_edges(
    driver: Driver,
    sg: TraversalSubgraph,
    *,
    edge_direction: TraversalEdgeDirection = "out",
) -> TraversalSubgraph:
    """把当前 ``node_keys`` 诱导子图上的有向边并入 ``edges``（方向同 ``edge_direction``）。

    决策 LLM 路径只显式列出走过的少数几条边；图上任意两点若均在 ``node_keys``
    内且存在匹配方向的有向边，也应传给下游（监控 / evidence）。
    """
    edge_direction = normalize_traversal_edge_direction(edge_direction)
    keys_list = list(dict.fromkeys(sg.node_keys or []))
    if len(keys_list) < 2:
        return sg
    key_set = set(keys_list)
    merged_edges = list(sg.edges or [])
    seen_edge: set[tuple[str, str, str]] = {
        (str(e.get("from") or ""), str(e.get("rel") or ""), str(e.get("to") or ""))
        for e in merged_edges
    }
    cyphers: list[str] = []
    if edge_direction in ("out", "both"):
        cyphers.append(
            """
            MATCH (a)-[r]->(b)
            WHERE a.key IN $keys AND b.key IN $keys
            RETURN a.key AS fk, type(r) AS rel, b.key AS tk
            """
        )
    if edge_direction in ("in", "both"):
        cyphers.append(
            """
            MATCH (b)-[r]->(a)
            WHERE a.key IN $keys AND b.key IN $keys
            RETURN b.key AS fk, type(r) AS rel, a.key AS tk
            """
        )
    rows: list[dict[str, Any]] = []
    try:
        with neo4j_session(driver) as s:
            for cypher in cyphers:
                rows.extend(s.run(cypher, keys=list(key_set)).data())
    except Exception as exc:
        log.warning("expand_traversal_induced_edges failed: %s", exc)
        return sg

    for row in rows or []:
        fk = str(row.get("fk") or "")
        rel = str(row.get("rel") or "")
        tk = str(row.get("tk") or "")
        if not (fk and rel and tk):
            continue
        t = (fk, rel, tk)
        if t in seen_edge:
            continue
        seen_edge.add(t)
        merged_edges.append({"from": fk, "rel": rel, "to": tk})

    return TraversalSubgraph(
        node_keys=keys_list,
        nodes_by_label=dict(sg.nodes_by_label or {}),
        edges=merged_edges,
        method=sg.method,
    )


def subgraph_from_picked_path(
    driver: Driver,
    path_steps: list[dict[str, str]],
) -> TraversalSubgraph:
    """由具体边构造 TraversalSubgraph（含节点 key 集合与按标签分组）。"""
    keys: list[str] = []
    edges_out: list[dict[str, str]] = []
    for s in path_steps:
        fk = s.get("from_key") or ""
        tk = s.get("to_key") or ""
        rel = s.get("relationship_type") or ""
        if fk:
            keys.append(fk)
        if tk:
            keys.append(tk)
        edges_out.append({"from": fk, "rel": rel, "to": tk})
    uniq_keys = list(dict.fromkeys(keys))
    nbl = _fill_nodes_by_label(driver, uniq_keys)
    return TraversalSubgraph(
        node_keys=uniq_keys,
        nodes_by_label=nbl,
        edges=edges_out,
        method="rule_llm_path",
    )


def normalize_llm_paths_payload(parsed: dict[str, Any]) -> list[list[Any]]:
    """Read ``paths`` from merged decision JSON; fall back to legacy ``path``."""
    rp = parsed.get("paths")
    if rp is not None:
        if not isinstance(rp, list):
            return []
        out: list[list[Any]] = []
        for p in rp:
            if isinstance(p, list):
                out.append(p)
        return out
    legacy = parsed.get("path")
    if legacy is not None and isinstance(legacy, list):
        return [legacy]
    return []


def resolve_paths_steps(
    candidates: list[CandidateEdge],
    anchors: AnchorSet,
    max_path_len: int,
    raw_paths: Optional[list[list[Any]]],
) -> list[list[dict[str, str]]]:
    """Validate each LLM path against the candidate edge set; drop invalid walks.

    ``max_path_len`` 为允许的路径边数上限。若全部 LLM path 都校验失败，
    用一条贪心链兜底。
    """
    if not candidates:
        return []
    allowed = _candidate_key_set(candidates)
    max_len = max(max_path_len, 1)
    anchor_order = [a.key for a in sorted(anchors.anchors, key=lambda a: -a.score)]
    out: list[list[dict[str, str]]] = []

    for raw_path in raw_paths or []:
        if not isinstance(raw_path, list):
            continue
        steps = [
            {
                "from_key": str(s.get("from_key") or ""),
                "relationship_type": str(s.get("relationship_type") or ""),
                "to_key": str(s.get("to_key") or ""),
            }
            for s in raw_path
            if isinstance(s, dict)
        ]
        if not steps:
            continue
        if _validate_path(steps, allowed, max_len=max_len):
            out.append(steps)
        else:
            log.warning("resolve_paths_steps: invalid path from LLM; skipped")

    if out:
        return out

    fb = _fallback_greedy_path(candidates, max_path_len, anchor_order)
    return [fb] if fb else []


def resolve_path_steps(
    candidates: list[CandidateEdge],
    anchors: AnchorSet,
    max_path_len: int,
    raw_path: Optional[list[Any]],
) -> list[dict[str, str]]:
    """校验合并 LLM 输出的单条 path；失败则用贪心兜底（单链）。"""
    paths = resolve_paths_steps(
        candidates,
        anchors,
        max_path_len,
        [raw_path] if raw_path is not None else [],
    )
    return paths[0] if paths else []


def format_candidate_edge_lines(
    candidates: list[CandidateEdge],
    *,
    limit: int = 10000,
    edge_direction: TraversalEdgeDirection = "out",
) -> str:
    """Render candidates for the path-pick LLM: group by (hop, from_key) to dedupe repeated heads."""
    edge_direction = normalize_traversal_edge_direction(edge_direction)
    group_label = candidate_group_header_label(edge_direction)
    if not candidates or limit <= 0:
        return ""
    by_head: dict[tuple[int, str], list[CandidateEdge]] = defaultdict(list)
    for e in candidates:
        by_head[(e.hop, e.from_key)].append(e)
    ordered_heads = sorted(by_head.keys(), key=lambda x: (x[0], x[1]))

    lines: list[str] = []
    n_out = 0
    total = len(candidates)
    for hop, fk in ordered_heads:
        group = by_head[(hop, fk)]
        flab = group[0].from_label or ""
        header_emitted = False
        for e in group:
            if n_out >= limit:
                lines.append(
                    f"… ({total - n_out} more candidate edges omitted; limit={limit})"
                )
                return "\n".join(lines)
            if not header_emitted:
                lines.append(
                    f"[hop{hop}] {fk} ({flab}) → {len(group)} {group_label}:"
                )
                header_emitted = True
            lines.append(
                f"  [{e.eid}] -[{e.rel_type}]-> {e.to_key} "
                f"({e.from_label}→{e.to_label})"
            )
            n_out += 1
    return "\n".join(lines)


__all__ = [
    "CandidateEdge",
    "candidate_group_header_label",
    "expand_traversal_induced_edges",
    "fair_neighbor_edge_limit",
    "gather_candidate_edges",
    "gather_out_edges_from_keys",
    "traversal_edge_direction_label",
    "merge_edges_dedupe",
    "renumber_candidate_edge_ids",
    "normalize_llm_paths_payload",
    "resolve_path_steps",
    "resolve_paths_steps",
    "format_candidate_edge_lines",
    "subgraph_from_candidate_edges",
    "subgraph_from_picked_path",
    "subgraph_from_picked_paths",
    "union_subgraphs",
]

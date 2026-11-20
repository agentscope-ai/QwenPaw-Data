"""Develop topic clustering, same-name folding and locale projection."""

from typing import Any


def _env_num(name: str, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    import os

    try:
        return max(lo, min(hi, float(os.environ.get(name, "") or default)))
    except ValueError:
        return default


# Split similarity thresholds (env-tunable). Intra-layer pairs share one text
# style (metric definition vs metric definition), so a high bar suppresses the
# near-duplicate noise; cross-layer pairs rarely reach that bar (metric
# definition vs entity prose), so bridging uses per-node top-k linking
# with a floor instead of one absolute threshold. The legacy
# TOPIC_SIM_THRESHOLD env keeps working as an override for the intra bar.
INTRA_SIM_THRESHOLD = _env_num(
    "TOPIC_INTRA_SIM_THRESHOLD", _env_num("TOPIC_SIM_THRESHOLD", 0.85)
)
CROSS_SIM_FLOOR = _env_num("TOPIC_CROSS_SIM_FLOOR", 0.60)
CROSS_TOP_K = int(_env_num("TOPIC_CROSS_TOP_K", 3, lo=0, hi=16))
# Hard structural edges get this weight so structure survives clustering.
_HARD_EDGE_WEIGHT = 2.0
MAX_TOPICS = 12
MIN_TOPIC_SIZE = 2
# Degraded single-layer topics need more mass to be worth showing.
MIN_SINGLE_TOPIC_SIZE = 3


def _cosine_matrix(vecs: list[list[float]]) -> Any:
    import numpy as np

    m = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    m = m / norms
    return m @ m.T


def _cluster(
    nodes: list[dict[str, Any]], hard_edges: list[tuple[str, str, str]]
) -> tuple[list[dict[str, Any]], list[tuple[int, int, float]], dict[str, Any]]:
    """Louvain over (semantic similarity edges ∪ weighted hard edges).

    Semantic edges use split thresholds — intra-layer pairs must clear
    ``INTRA_SIM_THRESHOLD``; cross-layer pairs are linked per node to its
    top-``CROSS_TOP_K`` other-layer neighbours above ``CROSS_SIM_FLOOR``
    (one absolute bar starves the layer bridges: metric definitions and
    entity/task prose rarely exceed ~0.75 cosine).

    Multi-layer communities rank first; single-layer communities are kept as
    degraded topics (``cross_layer=False``) instead of being dropped, so an
    MG-only scope still gets a usable lens.
    Returns (communities [{idx, cross_layer}], semantic edges (i, j, sim),
    stats {cross_semantic_edges, max_cross_sim})."""
    import networkx as nx
    import numpy as np

    stats: dict[str, Any] = {"cross_semantic_edges": 0, "max_cross_sim": 0.0}
    n = len(nodes)
    if n == 0:
        return [], [], stats
    sims = _cosine_matrix([v["vec"] for v in nodes])
    layer_ids = {"MG": 0, "KG": 1, "UG": 2}
    lay = np.asarray([layer_ids.get(v["layer"], -1) for v in nodes])
    same_layer = lay[:, None] == lay[None, :]

    g = nx.Graph()
    g.add_nodes_from(range(n))
    idx_of_key = {v["key"]: i for i, v in enumerate(nodes)}
    sim_edges: list[tuple[int, int, float]] = []

    # Intra-layer semantic edges: high absolute bar.
    iu, ju = np.triu_indices(n, k=1)
    keep = same_layer[iu, ju] & (sims[iu, ju] >= INTRA_SIM_THRESHOLD)
    for i, j in zip(iu[keep].tolist(), ju[keep].tolist()):
        s = float(sims[i, j])
        g.add_edge(i, j, weight=s)
        sim_edges.append((i, j, s))

    # Cross-layer semantic edges: per-node top-k with a floor.
    if n > 1:
        cross = np.where(same_layer, -1.0, sims)
        np.fill_diagonal(cross, -1.0)
        stats["max_cross_sim"] = round(max(0.0, float(cross.max())), 3)
        k = min(CROSS_TOP_K, n - 1)
        if k > 0:
            picked: set[tuple[int, int]] = set()
            top = np.argpartition(-cross, kth=k - 1, axis=1)[:, :k]
            for i in range(n):
                for j in top[i].tolist():
                    if cross[i, j] < CROSS_SIM_FLOOR:
                        continue
                    picked.add((i, j) if i < j else (j, i))
            for i, j in picked:
                s = float(sims[i, j])
                g.add_edge(i, j, weight=s)
                sim_edges.append((i, j, s))
            stats["cross_semantic_edges"] = len(picked)

    for s, t, _rel in hard_edges:
        i, j = idx_of_key.get(s), idx_of_key.get(t)
        if i is None or j is None or i == j:
            continue
        base = g[i][j]["weight"] if g.has_edge(i, j) else 0.0
        g.add_edge(i, j, weight=base + _HARD_EDGE_WEIGHT)

    communities = nx.community.louvain_communities(
        g, weight="weight", resolution=1.0, seed=42
    )
    multi: list[dict[str, Any]] = []
    single: list[dict[str, Any]] = []
    for comm in communities:
        layers = {nodes[i]["layer"] for i in comm}
        idx = sorted(comm, key=lambda i: -len(nodes[i].get("text") or ""))
        if len(layers) >= 2 and len(comm) >= MIN_TOPIC_SIZE:
            multi.append({"idx": idx, "cross_layer": True})
        elif len(comm) >= MIN_SINGLE_TOPIC_SIZE:
            # Single-layer blobs are demoted, not dropped — an MG-only scope
            # (e.g. the admin global viewpoint) still gets domain groupings.
            single.append({"idx": idx, "cross_layer": False})
    multi.sort(key=lambda c: len(c["idx"]), reverse=True)
    single.sort(key=lambda c: len(c["idx"]), reverse=True)
    return (multi + single)[:MAX_TOPICS], sim_edges, stats


def _topic_ui_lang(lang: str | None) -> str:
    """UI locale → naming locale. Page i18n is zh | en."""
    raw = (lang or "zh").strip().lower().replace("_", "-")
    primary = raw.split("-", 1)[0] if raw else "zh"
    return "en" if primary == "en" else "zh"


_NAME_SYSTEM = {
    "zh": (
        "你是数据图谱主题分析师。给定一个主题的成员列表，成员可能跨三个图层"
        "（MG=元数据/指标，KG=知识沉淀，UG=用户记忆），"
        "也可能仅来自单层。用一句中文短语概括这个业务主题（≤12字），"
        "并给一句≤60字的摘要。指标正式名、表名保持原文。"
    ),
    "en": (
        "You are a data-graph topic analyst. Members may span MG (metrics/"
        "metadata), KG (knowledge), and UG (user memory), "
        "or come from a single layer. Write a short English title (≤8 words) "
        "and a one-sentence English summary (≤25 words). Keep official metric "
        "and table names in their original language."
    ),
}


def _topics_view(payload: dict[str, Any], lang: str | None) -> dict[str, Any]:
    """Project cached topics into the requested UI language."""
    loc = _topic_ui_lang(lang)
    view = dict(payload)
    topics = []
    for tp in payload.get("topics") or []:
        bag = (tp.get("i18n") or {}).get(loc) or {}
        item = {k: v for k, v in tp.items() if k != "i18n"}
        if bag.get("title"):
            item["title"] = bag["title"]
        if "summary" in bag:
            item["summary"] = bag.get("summary") or ""
        topics.append(item)
    view["topics"] = topics
    return view


def _merge_duplicates(
    nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Collapse same-typed nodes sharing a display name into one representative.

    Per-dataset repeats (the same dimension name under three datasets) flood
    the similarity graph with near-1.0 intra-layer edges that swamp the
    cross-layer bridges. Aliases re-expand into topic members after
    clustering. Returns (representatives, rep_key → alias nodes).
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for n in nodes:
        name = (n["label"] or "").strip().lower()
        gk = (n["type"], name) if name else (n["type"], f"\x00{n['key']}")
        groups.setdefault(gk, []).append(n)
    reps: list[dict[str, Any]] = []
    alias_of: dict[str, list[dict[str, Any]]] = {}
    for group in groups.values():
        rep = max(
            group,
            key=lambda x: (
                x["vec"] is not None,
                bool(x["text"]),
                len(x.get("text") or ""),
            ),
        )
        reps.append(rep)
        rest = [x for x in group if x is not rep]
        if rest:
            alias_of[rep["key"]] = rest
    return reps, alias_of

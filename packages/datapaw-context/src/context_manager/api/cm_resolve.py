"""Two-stage entity resolution for CM API (domain inference + fuzzy name match)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal, Optional

from fastapi import HTTPException
from neo4j import Driver

from .cm_models import AmbiguityCandidate, AmbiguousResponse
from .ctx_session import ContextSession, SessionStore
from . import semantic_store as store

log = logging.getLogger("api.cm_resolve")

# RRF scores are ~0–0.05; scale to 0–1 for confidence
_SCORE_SCALE = 20.0
_UNIQUE_HIT_THRESHOLD = 0.55
_AMBIGUITY_FLOOR = 0.30
_MIN_GAP = 0.05
_SYNONYM_BOOST = 0.40
_EXACT_NAME_BOOST = 0.50
_DOMAIN_MATCH_THRESHOLD = 0.6


_HOLO_DOMAINS = frozenset({
    "chatapp", "qwen_chat", "qwen", "chat",
    "studio", "Studio",
})

_ODPS_DOMAINS = frozenset({
    "ops", "imagegen",
})


def default_datasource_for_domain(domain: str) -> str:
    """Domain → default datasource_id 兜底。

    - ChatApp / Studio 系列默认走 Holo (appdata)
    - Ops / ImageGen 等默认走 ODPS (analytics_dw)

    当 metadata 里没有显式指定 datasource_id 时使用此映射。
    """
    d = (domain or "").strip().lower()
    if d in _HOLO_DOMAINS:
        return "appdata"
    return "analytics_dw"


def datasource_routing_enabled() -> bool:
    """read 路径是否启用 datasource 路由(env ``CM_DATASOURCE_ROUTING``)。

    默认开启:走「请求显式值 > session 缓存 > domain 默认 > Excel 标注」的解析链。
    显式设 ``CM_DATASOURCE_ROUTING=0/false/off/no`` 时关闭(force-default 模式),
    所有 read 一律走 :func:`default_datasource_for_domain`。
    """
    raw = (os.getenv("CM_DATASOURCE_ROUTING") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def resolve_read_datasource(
    domain: str,  # noqa: ARG001 - kept for call-site compatibility
    *,
    req_datasource_id: str = "",
) -> str:
    """read 接口过滤用的 ``datasource_id``：只认请求显式值，不做任何兜底。

    缺失时返回空串（不按数据源过滤，返回该域全量），不再按 domain 猜 canonical
    ID、也不回退 synced default，避免猜错数据源导致结果为空或串数据源。
    """
    return (req_datasource_id or "").strip()


_CANONICAL_IDS = frozenset({"appdata", "analytics_dw"})

_TYPE_TO_CANONICAL: dict[str, str] = {
    "odps": "analytics_dw",
    "hologres": "appdata",
    "postgres": "appdata",
    "postgresql": "appdata",
}


def _canonicalize_datasource_id(datasource_id: str) -> str:
    """把任意 datasource_id 映射到图中已知的 canonical ID。

    优先级：
    1. 已知 canonical ID（appdata / analytics_dw）原样返回；
    2. registry 中已注册的 ID 原样返回（如 test_db）；
    3. 名字前缀兜底：``odps-*`` → analytics_dw，``holo*`` → appdata；
    4. sync store 类型映射；
    5. 都查不到则原样返回。
    """
    if datasource_id in _CANONICAL_IDS:
        return datasource_id
    # registry 中已注册的 datasource_id 保留原值，避免 test_db 被类型
    # 映射成 appdata 导致写/读 scope 不一致。
    try:
        from ..graph.datasource_registry import try_resolve
        if try_resolve(datasource_id) is not None:
            return datasource_id
    except Exception:
        pass
    # 前缀兜底：动态实例 ID（如 odps-eaeb24390d65、holo-xxx）按类型映射
    low = datasource_id.lower()
    if low.startswith("odps"):
        return "analytics_dw"
    if low.startswith("holo"):
        return "appdata"
    from .datasource_active_api import load_datasource_config
    cfg = load_datasource_config(datasource_id)
    ds_type = (cfg or {}).get("_datasource_type", "")
    if ds_type and ds_type in _TYPE_TO_CANONICAL:
        return _TYPE_TO_CANONICAL[ds_type]
    return datasource_id

EntityKind = Literal["metric", "dimension", "dataset"]


@dataclass
class ResolvedCandidate:
    entity_type: str
    name: str
    domain: str
    description: str
    match_confidence: float
    canonical_name: str = ""
    graph_key: str = ""


@dataclass
class ResolveResult:
    kind: Literal["hit", "ambiguous", "not_found"]
    domain: str = ""
    canonical_name: str = ""
    graph_key: str = ""
    candidates: list[ResolvedCandidate] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.candidates is None:
            self.candidates = []


def _rrf_to_confidence(score: float) -> float:
    return min(1.0, max(0.0, float(score) * _SCORE_SCALE))


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def infer_domain(
    driver: Driver,
    *,
    domain_param: Optional[str],
    session: Optional[ContextSession],
    store_sess: SessionStore,
) -> str:
    """Stage 1: resolve business domain (§7.1.2)."""
    if session:
        anchored = (session.scope or {}).get("domain") or ""
        if anchored:
            return str(anchored).strip()
        # Try metric anchors from current snapshot
        if session.snapshots:
            for a in getattr(session.current.anchors, "anchors", []) or []:
                d = getattr(a, "domain", "") or ""
                if d:
                    session.scope["domain"] = d
                    return d

    if domain_param and domain_param.strip():
        dom = domain_param.strip()
        domains = [d.name for d in store.list_domain_records(driver)]
        if not domains:
            return dom
        # Exact match
        for d in domains:
            if _normalize(d) == _normalize(dom):
                return d
        # Alias / substring
        matches: list[tuple[str, float]] = []
        q = _normalize(dom)
        for d in domains:
            rec = next((x for x in store.list_domain_records(driver) if x.name == d), None)
            aliases = list(rec.aliases) if rec else []
            if q in _normalize(d) or _normalize(d) in q:
                matches.append((d, 0.95))
                continue
            for al in aliases:
                if q in _normalize(al) or _normalize(al) in q:
                    matches.append((d, 0.9))
                    break
            if dom.lower() in d.lower():
                matches.append((d, 0.75))
        if len(matches) == 1:
            return matches[0][0]
        if len(matches) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot uniquely infer domain from '{dom}'. "
                    f"Candidates: {[m[0] for m in matches]}. Specify domain or session_ref."
                ),
            )
        if store.domain_exists(driver, dom):
            return dom
        raise HTTPException(status_code=404, detail=f"Domain not found: {dom}")

    domains = [d.name for d in store.list_domain_records(driver)]
    raise HTTPException(
        status_code=400,
        detail=(
            "Cannot infer domain. Available domains: "
            f"{domains!r}. Specify domain or session_ref."
        ),
    )


def anchor_domain_on_session(session: ContextSession, domain: str) -> None:
    if domain:
        session.scope["domain"] = domain


def _metric_candidates(
    driver: Driver, domain: str, name: str, k: int = 8, datasource_id: str = "",
) -> list[ResolvedCandidate]:
    from .retrieval import resolve_metric

    # resolve_metric/detect_dimensions gain datasource_id in Task 9
    # (retrieval.py); not forwarded yet to avoid a runtime TypeError.
    rows = resolve_metric(driver, name, k=k, domain=domain, datasource_id=datasource_id)
    out: list[ResolvedCandidate] = []
    q = _normalize(name)
    for r in rows:
        nm = str(r.get("name") or "")
        if not nm:
            continue
        conf = _rrf_to_confidence(float(r.get("score") or 0.0))
        synonyms = [str(s).lower() for s in (r.get("synonyms") or [])]
        nm_lower = nm.lower()
        if q == nm_lower:
            conf = min(1.0, conf + _EXACT_NAME_BOOST)
        elif q in synonyms:
            conf = min(1.0, conf + _SYNONYM_BOOST)
        out.append(
            ResolvedCandidate(
                entity_type="Metric",
                name=nm,
                domain=str(r.get("domain") or domain),
                description=str(r.get("definition") or r.get("description") or "")[:200],
                match_confidence=conf,
                canonical_name=nm,
                graph_key=str(r.get("key") or ""),
            )
        )
    return out


def _dimension_candidates(
    driver: Driver, domain: str, name: str, datasource_id: str = "",
) -> list[ResolvedCandidate]:
    from .retrieval import detect_dimensions

    rows = detect_dimensions(driver, domain, name, datasource_id=datasource_id)
    out: list[ResolvedCandidate] = []
    for r in rows:
        nm = str(r.get("name") or "")
        if not nm:
            continue
        out.append(
            ResolvedCandidate(
                entity_type="Dimension",
                name=nm,
                domain=domain,
                description=str(r.get("description") or "")[:200],
                match_confidence=0.75,
                canonical_name=nm,
            )
        )
    if out:
        return out
    # Fallback: list dimensions and fuzzy match
    names = store.list_dimension_names(driver, domain, datasource_id=datasource_id)
    q = _normalize(name)
    for dn in names:
        if q == _normalize(dn) or q in _normalize(dn):
            out.append(
                ResolvedCandidate(
                    entity_type="Dimension",
                    name=dn,
                    domain=domain,
                    description="",
                    match_confidence=0.85 if q == _normalize(dn) else 0.55,
                    canonical_name=dn,
                )
            )
    return out[:8]


def _dataset_candidates(
    driver: Driver, domain: str, name: str, datasource_id: str = "",
) -> list[ResolvedCandidate]:
    from .datasource_active_api import resolve_datasource_id
    ds_id = (datasource_id or "").strip() or resolve_datasource_id(
        default_datasource_for_domain(domain)
    )
    datasets = store.list_datasets(driver, domain, datasource_id=ds_id)
    q = _normalize(name)
    out: list[ResolvedCandidate] = []
    for ds in datasets:
        dn = ds.dataset_name
        tbl = (ds.parents or dn.removeprefix("view_"))
        score = 0.0
        if q == _normalize(dn):
            score = 0.95
        elif q == _normalize(tbl):
            score = 0.9
        elif q in _normalize(dn) or q in _normalize(tbl):
            score = 0.7
        elif _normalize(dn) in q:
            score = 0.65
        if score > 0:
            out.append(
                ResolvedCandidate(
                    entity_type="Dataset",
                    name=dn,
                    domain=domain,
                    description=str(ds.description or "")[:200],
                    match_confidence=score,
                    canonical_name=dn,
                )
            )
    out.sort(key=lambda c: -c.match_confidence)
    return out[:8]


def _entity_candidates(
    driver: Driver,
    domain: str,
    name: str,
    kind: EntityKind,
    datasource_id: str = "",
) -> list[ResolvedCandidate]:
    if kind == "metric":
        return _metric_candidates(driver, domain, name, datasource_id=datasource_id)
    if kind == "dimension":
        return _dimension_candidates(driver, domain, name, datasource_id=datasource_id)
    # metric/dimension/dataset 均按 datasource_id 过滤（Tasks 7-9 后语义层已隔离）。
    return _dataset_candidates(driver, domain, name, datasource_id=datasource_id)


def _exact_hit(
    candidates: list[ResolvedCandidate],
    query_name: str,
    target_domain: str,
) -> Optional[ResolvedCandidate]:
    """Short-circuit: if a single candidate's name == query (case-insensitive)
    and its domain matches (or no domain filter), return it as a definitive hit.

    Why: RRF scores are tiny (~0.05) and _SCORE_SCALE=20 pushes every top-k
    candidate to confidence=1.0, making _MIN_GAP impossible to satisfy via
    boost alone. Without this short-circuit, every "DAU" / "国家" / etc. query
    falls into ambiguous even when the answer is unambiguously named.
    Returns None if zero or multiple candidates satisfy exact + domain match.
    """
    q = _normalize(query_name)
    if not q:
        return None
    dom = _normalize(target_domain)
    hits = [
        c for c in candidates
        if _normalize(c.name) == q
        and (not dom or _normalize(c.domain) == dom)
    ]
    return hits[0] if len(hits) == 1 else None


def _classify_candidates(candidates: list[ResolvedCandidate]) -> ResolveResult:
    if not candidates:
        return ResolveResult(kind="not_found", candidates=[])

    ranked = sorted(candidates, key=lambda c: -c.match_confidence)
    top = ranked[0]
    second_conf = ranked[1].match_confidence if len(ranked) > 1 else 0.0

    if (
        top.match_confidence >= _UNIQUE_HIT_THRESHOLD
        and (top.match_confidence - second_conf) >= _MIN_GAP
    ):
        return ResolveResult(
            kind="hit",
            domain=top.domain,
            canonical_name=top.canonical_name or top.name,
            graph_key=top.graph_key,
            candidates=[top],
        )

    above_floor = [c for c in ranked if c.match_confidence >= _AMBIGUITY_FLOOR]
    if len(above_floor) >= 2:
        return ResolveResult(
            kind="ambiguous",
            domain=top.domain,
            candidates=above_floor[:10],
        )

    if top.match_confidence >= _AMBIGUITY_FLOOR:
        return ResolveResult(
            kind="ambiguous",
            domain=top.domain,
            candidates=[top],
        )
    return ResolveResult(kind="not_found", candidates=[])


def _hit_from(c: ResolvedCandidate, fallback_domain: str) -> ResolveResult:
    return ResolveResult(
        kind="hit",
        domain=c.domain or fallback_domain,
        canonical_name=c.canonical_name or c.name,
        graph_key=c.graph_key,
        candidates=[c],
    )


def resolve_entity(
    driver: Driver,
    *,
    domain: str,
    name: str,
    kind: EntityKind,
    datasource_id: str = "",
) -> ResolveResult:
    candidates = _entity_candidates(driver, domain, name.strip(), kind, datasource_id)
    exact = _exact_hit(candidates, name, domain)
    if exact is not None:
        return _hit_from(exact, domain)
    result = _classify_candidates(candidates)
    result.domain = result.domain or domain
    return result


def resolve_entity_any(
    driver: Driver,
    *,
    domain: str,
    name: str,
    datasource_id: str = "",
) -> ResolveResult:
    """Explore-entity: try metric, dimension, dataset and merge."""
    all_c: list[ResolvedCandidate] = []
    for kind in ("metric", "dimension", "dataset"):
        all_c.extend(_entity_candidates(driver, domain, name, kind, datasource_id))  # type: ignore[arg-type]
    # Dedupe by name+type
    seen: set[str] = set()
    merged: list[ResolvedCandidate] = []
    for c in sorted(all_c, key=lambda x: -x.match_confidence):
        key = f"{c.entity_type}:{c.name}"
        if key not in seen:
            seen.add(key)
            merged.append(c)
    exact = _exact_hit(merged, name, domain)
    if exact is not None:
        return _hit_from(exact, domain)
    return _classify_candidates(merged)


def to_ambiguity_candidates(candidates: list[ResolvedCandidate]) -> list[AmbiguityCandidate]:
    return [
        AmbiguityCandidate(
            entity_type=c.entity_type,
            name=c.name,
            domain=c.domain,
            description=c.description,
            match_confidence=c.match_confidence,
        )
        for c in candidates
    ]


def ambiguous_payload(
    candidates: list[ResolvedCandidate],
    *,
    hint: str = "",
    entity_type: str = "Metric",
) -> dict[str, Any]:
    return AmbiguousResponse(
        ambiguous=True,
        ambiguity_candidates=to_ambiguity_candidates(candidates),
        hint=hint or "请用更精确的名称或 domain 缩小范围",
    ).model_dump()


def not_found_http(kind: EntityKind, domain: str, name: str) -> HTTPException:
    labels = {"metric": "Metric", "dimension": "Dimension", "dataset": "Dataset"}
    return HTTPException(
        status_code=404,
        detail=f"{labels[kind]} not found: {domain}::{name}",
    )

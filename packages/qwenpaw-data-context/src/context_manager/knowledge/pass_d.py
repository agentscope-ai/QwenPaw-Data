"""Pass D: after ingest, survey MG/TG/KG in Neo4j and propose v4 cross-graph edges."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from qwenpaw_data.context.paths import knowledge_ingest_cache_dir

from ..config import CFG, knowledge_ingest_heavy_llm_timeout_sec
from ..openai_client import complete_json, resolve_llm_model
from ..graph.knowledge import _write_entity_links
from ..utils import get_logger, neo4j_session
from .prompts import (
    PASS_D_JSON_SCHEMA,
    pass_d_system,
    pass_d_user,
)
from .writer import IngestReport

log = get_logger("knowledge.pass_d")

DEFAULT_CACHE_DIR = knowledge_ingest_cache_dir()

_PASS_D_REL = frozenset(
    {
        "SURFACE_METRIC",
        "SURFACE_DIMENSION",
        "SURFACE_DOMAIN",
        "SURFACE_OPERATOR",
        "ABOUT",
        "CONCERNS",
        "RELATED_TO",
        "HAS_INSTANCE",
    }
)
_PASS_D_MET_ROLES = frozenset({"primary", "alias_of", "partial_view"})
_PASS_D_DIM_ROLES = frozenset({"primary", "alias_of"})
_PASS_D_CONCERNS_ROLES = frozenset({"subject", "context", "filter"})
_PASS_D_RELATED_SUB = frozenset(
    {"synonym", "antonym", "competitor", "complement", "correlates", "see_also"}
)


def fetch_mg_catalog(
    driver: Any,
    *,
    max_metrics: int = 450,
    max_dims: int = 450,
    max_domains: int = 150,
    max_ops: int = 150,
) -> dict[str, list[dict[str, str]]]:
    """Read compact Metric/Dimension/Domain lists from Neo4j (+ static operator reference)."""
    out: dict[str, list[dict[str, str]]] = {
        "metrics": [],
        "dimensions": [],
        "domains": [],
        "operators": [
            {"key": f"op:_global:{n}", "name": n}
            for n in ("dod", "wow", "mom", "yoy", "contribution", "impact",
                      "share_impact", "rate_impact", "total_impact")
        ],
    }
    try:
        with neo4j_session(driver) as s:
            out["metrics"] = [
                {"key": str(r.get("key") or ""), "name": str(r.get("name") or "")[:200]}
                for r in (
                    s.run(
                        """
                        MATCH (m:Metric)
                        RETURN m.key AS key, coalesce(m.name, '') AS name
                        ORDER BY m.key
                        LIMIT $lim
                        """,
                        lim=int(max_metrics),
                    ).data()
                )
                if str(r.get("key") or "").strip()
            ]
            out["dimensions"] = [
                {"key": str(r.get("key") or ""), "name": str(r.get("name") or "")[:200]}
                for r in (
                    s.run(
                        """
                        MATCH (d:Dimension)
                        RETURN d.key AS key, coalesce(d.name, '') AS name
                        ORDER BY d.key
                        LIMIT $lim
                        """,
                        lim=int(max_dims),
                    ).data()
                )
                if str(r.get("key") or "").strip()
            ]
            out["domains"] = [
                {"key": str(r.get("key") or ""), "name": str(r.get("name") or "")[:200]}
                for r in (
                    s.run(
                        """
                        MATCH (dom:Domain)
                        RETURN dom.key AS key, coalesce(dom.name, '') AS name
                        ORDER BY dom.key
                        LIMIT $lim
                        """,
                        lim=int(max_domains),
                    ).data()
                )
                if str(r.get("key") or "").strip()
            ]
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_mg_catalog failed: %s", exc)
    return out


def fetch_trace_graph_catalog(
    driver: Any,
    *,
    max_tasks: int = 48,
    max_plans: int = 72,
) -> dict[str, list[dict[str, str]]]:
    """Sample TG :Task / :Step rows (keys + short text) for Pass D."""
    out: dict[str, list[dict[str, str]]] = {"tasks": [], "plans": []}
    try:
        with neo4j_session(driver) as s:
            out["tasks"] = [
                {"key": str(r.get("key") or ""), "goal": str(r.get("goal") or "")[:240]}
                for r in (
                    s.run(
                        """
                        MATCH (t:Task)
                        RETURN t.key AS key, coalesce(t.goal, '') AS goal
                        ORDER BY t.key DESC
                        LIMIT $lim
                        """,
                        lim=int(max_tasks),
                    ).data()
                )
                if str(r.get("key") or "").strip()
            ]
            out["plans"] = [
                {"key": str(r.get("key") or ""), "intent": str(r.get("intent") or "")[:240]}
                for r in (
                    s.run(
                        """
                        MATCH (p:Step)
                        RETURN p.key AS key, coalesce(p.intent, '') AS intent
                        ORDER BY p.key DESC
                        LIMIT $lim
                        """,
                        lim=int(max_plans),
                    ).data()
                )
                if str(r.get("key") or "").strip()
            ]
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_trace_graph_catalog failed: %s", exc)
    return out


def fetch_knowledge_graph_catalog(
    driver: Any,
    *,
    max_entities: int = 220,
) -> list[dict[str, str]]:
    """Sample existing :Entity keys in Neo4j (includes freshly written doc entities)."""
    try:
        with neo4j_session(driver) as s:
            return [
                {"key": str(r.get("key") or ""), "name": str(r.get("name") or "")[:200]}
                for r in (
                    s.run(
                        """
                        MATCH (e:Entity)
                        RETURN e.key AS key, coalesce(e.name, '') AS name
                        ORDER BY e.key
                        LIMIT $lim
                        """,
                        lim=int(max_entities),
                    ).data()
                )
                if str(r.get("key") or "").strip()
            ]
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_knowledge_graph_catalog failed: %s", exc)
    return []


def fetch_zone_counts(driver: Any) -> list[dict[str, Any]]:
    """Coarse counts by ``zone`` across all nodes (MG / trace / knowledge / unset)."""
    try:
        with neo4j_session(driver) as s:
            rows = s.run(
                """
                MATCH (n)
                RETURN coalesce(n.zone, '_unset') AS zone, count(*) AS count
                ORDER BY zone
                """
            ).data()
            return [{"zone": str(r.get("zone")), "count": int(r.get("count") or 0)} for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_zone_counts failed: %s", exc)
    return []


def ent_cluster_batches(
    clusters: list[dict[str, Any]],
    *,
    batch_size: int,
) -> list[list[dict[str, Any]]]:
    """Split ``clusters`` to ``ent:``-only lists for incremental Pass D."""
    rows = [
        c
        for c in clusters
        if isinstance(c, dict) and str(c.get("canonical_key") or "").strip().startswith("ent:")
    ]
    if not rows:
        return []
    if batch_size <= 0 or batch_size >= len(rows):
        return [rows]
    return [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]


def filter_pass_d_edges_for_ent_batch(
    edges: list[dict[str, Any]],
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop edges whose KG ``ent:`` endpoint is outside this batch (incremental mode)."""
    keys = {str(c.get("canonical_key") or "").strip() for c in batch if isinstance(c, dict)}
    if not keys:
        return edges
    out: list[dict[str, Any]] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        rt = str(e.get("rel_type") or "").strip().upper()
        fk = str(e.get("from_key") or "").strip()
        tk = str(e.get("to_key") or "").strip()
        if rt in ("SURFACE_METRIC", "SURFACE_DIMENSION", "SURFACE_DOMAIN", "SURFACE_OPERATOR"):
            if fk.startswith("ent:") and fk not in keys:
                continue
        elif rt == "ABOUT":
            if tk.startswith("ent:") and tk not in keys:
                continue
        elif rt == "CONCERNS":
            if tk.startswith("ent:") and tk not in keys:
                continue
        elif rt == "RELATED_TO":
            if fk.startswith("ent:") and tk.startswith("ent:"):
                if fk not in keys and tk not in keys:
                    continue
        elif rt == "HAS_INSTANCE":
            if fk.startswith("ent:") and tk.startswith("ent:"):
                if fk not in keys and tk not in keys:
                    continue
        out.append(e)
    return out


def build_pass_d_payload(
    mg_catalog: dict[str, list[dict[str, str]]],
    *,
    trace_catalog: dict[str, list[dict[str, str]]],
    kg_catalog: list[dict[str, str]],
    zone_counts: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    incremental: Optional[dict[str, Any]] = None,
) -> str:
    kg_entities: list[dict[str, Any]] = []
    for c in clusters:
        if not isinstance(c, dict):
            continue
        ck = str(c.get("canonical_key") or "").strip()
        if not ck.startswith("ent:"):
            continue
        surfaces = [str(x).strip() for x in (c.get("surfaces") or []) if str(x).strip()][:16]
        row: dict[str, Any] = {
            "canonical_key": ck,
            "canonical_name": str(c.get("canonical_name") or ""),
            "entity_type": str(c.get("entity_type") or ""),
            "surfaces": surfaces,
        }
        ls = str(c.get("lifecycle_state") or "").strip()
        if ls:
            row["lifecycle_state"] = ls
        kg_entities.append(row)

    body = {
        "metadata_graph_catalog": mg_catalog,
        "trace_graph_catalog": trace_catalog,
        "knowledge_graph_entity_sample": kg_catalog[:220],
        "nodes_by_zone_counts": zone_counts,
        "doc_ingest_entities": kg_entities[:220],
        "pass_d_prompt_version": 4,
    }
    if incremental:
        body["pass_d_incremental"] = incremental
    return json.dumps(body, ensure_ascii=False)


def coerce_pass_d_raw(parsed: Any) -> Any:
    if not isinstance(parsed, dict):
        return parsed
    out: dict[str, Any] = dict(parsed)
    sc = out.get("self_confidence")
    try:
        out["self_confidence"] = float(sc) if sc is not None else 0.7
    except (TypeError, ValueError):
        out["self_confidence"] = 0.7
    out["self_confidence"] = max(0.0, min(1.0, float(out["self_confidence"])))

    raw = out.get("edges")
    if not isinstance(raw, list):
        out["edges"] = []
        ca = str(out.get("connection_assessment") or "").strip()
        if ca:
            out["connection_assessment"] = ca[:8000]
        return out

    fixed: list[dict[str, Any]] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        rt = str(e.get("rel_type") or "").strip().upper()
        if rt not in _PASS_D_REL:
            continue
        fk = str(e.get("from_key") or "").strip()
        tk = str(e.get("to_key") or "").strip()
        if not fk or not tk:
            continue
        if rt in ("SURFACE_METRIC", "SURFACE_DIMENSION", "SURFACE_DOMAIN", "SURFACE_OPERATOR"):
            if not fk.startswith("ent:"):
                continue
            if rt == "SURFACE_METRIC" and not tk.startswith("met:"):
                continue
            if rt == "SURFACE_DIMENSION" and not tk.startswith("dim:"):
                continue
            if rt == "SURFACE_DOMAIN" and not tk.startswith("dom:"):
                continue
            if rt == "SURFACE_OPERATOR" and not tk.startswith("op:"):
                continue
        elif rt == "ABOUT":
            if not (fk.startswith("task:") and tk.startswith("ent:")):
                continue
        elif rt == "CONCERNS":
            if not (fk.startswith("plan:") and tk.startswith("ent:")):
                continue
        elif rt == "RELATED_TO":
            if not (fk.startswith("ent:") and tk.startswith("ent:")) or fk == tk:
                continue
        elif rt == "HAS_INSTANCE":
            if not (fk.startswith("ent:") and tk.startswith("ent:")) or fk == tk:
                continue
        else:
            continue
        row: dict[str, Any] = {"rel_type": rt, "from_key": fk, "to_key": tk}
        role = str(e.get("role") or "").strip()[:64]
        if rt == "CONCERNS":
            r0 = role or "subject"
            row["role"] = r0 if r0 in _PASS_D_CONCERNS_ROLES else "subject"
        elif rt in (
            "SURFACE_METRIC",
            "SURFACE_DIMENSION",
            "SURFACE_DOMAIN",
            "SURFACE_OPERATOR",
        ) and role:
            row["role"] = role
        elif role and rt not in ("RELATED_TO", "HAS_INSTANCE", "ABOUT"):
            row["role"] = role
        rat = str(e.get("rationale") or "").strip()
        if rat:
            row["rationale"] = rat[:500]
        if rt == "RELATED_TO":
            st = str(e.get("relation_subtype") or e.get("subtype") or "see_also").strip()[:64]
            if st not in _PASS_D_RELATED_SUB:
                st = "see_also"
            row["relation_subtype"] = st
            scp = str(e.get("scope") or "").strip()[:200]
            if scp:
                row["scope"] = scp
            sim = e.get("sim_score")
            if sim is not None and str(sim).strip() != "":
                try:
                    row["sim_score"] = float(sim)
                except (TypeError, ValueError):
                    pass
            dsc = str(e.get("description") or "").strip()[:500]
            if not dsc and rat:
                dsc = rat[:500]
            if dsc:
                row["description"] = dsc
        fixed.append(row)
    out["edges"] = fixed[:200]
    ca2 = str(out.get("connection_assessment") or "").strip()
    if ca2:
        out["connection_assessment"] = ca2[:8000]
    else:
        out.pop("connection_assessment", None)
    return out


def _extract_all_ent_keys(payload_json: str) -> frozenset[str]:
    """Extract every valid ent: key from the Pass D payload.

    Combines ``doc_ingest_entities`` (current run) and ``knowledge_graph_entity_sample``
    (entities already in Neo4j).  The union is the only set of ent: keys the LLM may
    legitimately reference; anything else is a hallucinated key.
    Returns an empty frozenset on parse error (disables validation).
    """
    try:
        body = json.loads(payload_json)
    except Exception:  # noqa: BLE001
        return frozenset()
    if not isinstance(body, dict):
        return frozenset()
    keys: set[str] = set()
    for e in body.get("doc_ingest_entities") or []:
        if isinstance(e, dict):
            k = str(e.get("canonical_key") or "").strip()
            if k.startswith("ent:"):
                keys.add(k)
    for e in body.get("knowledge_graph_entity_sample") or []:
        if isinstance(e, dict):
            k = str(e.get("key") or "").strip()
            if k.startswith("ent:"):
                keys.add(k)
    return frozenset(keys)


def _filter_edges_by_ent_keys(
    edges: list[dict[str, Any]],
    valid_ent_keys: frozenset[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop edges whose ent: endpoint is not in ``valid_ent_keys``.

    Side mapping:
    • SURFACE_* : from_key is ent:
    • ABOUT / CONCERNS : to_key is ent:
    • RELATED_TO / HAS_INSTANCE : both from_key and to_key are ent:

    Returns (kept_edges, skipped_reasons).
    When ``valid_ent_keys`` is empty (parse error), all edges are kept (fail-open).
    """
    if not valid_ent_keys:
        return edges, []
    kept: list[dict[str, Any]] = []
    skipped: list[str] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        rt = str(e.get("rel_type") or "").strip().upper()
        fk = str(e.get("from_key") or "").strip()
        tk = str(e.get("to_key") or "").strip()
        ent_keys_to_check: list[str] = []
        if rt in ("SURFACE_METRIC", "SURFACE_DIMENSION", "SURFACE_DOMAIN", "SURFACE_OPERATOR"):
            if fk.startswith("ent:"):
                ent_keys_to_check.append(fk)
        elif rt == "ABOUT":
            if tk.startswith("ent:"):
                ent_keys_to_check.append(tk)
        elif rt == "CONCERNS":
            if tk.startswith("ent:"):
                ent_keys_to_check.append(tk)
        elif rt in ("RELATED_TO", "HAS_INSTANCE"):
            if fk.startswith("ent:"):
                ent_keys_to_check.append(fk)
            if tk.startswith("ent:"):
                ent_keys_to_check.append(tk)
        bad = [k for k in ent_keys_to_check if k not in valid_ent_keys]
        if bad:
            skipped.append(
                f"pass_d_invalid_ent_key [{rt}] {fk} → {tk} — not in payload: {bad}"
            )
            continue
        kept.append(e)
    return kept, skipped


def _d_key(payload: str, model: str) -> str:
    return hashlib.sha1((payload + "\n" + model).encode("utf-8")).hexdigest()


def load_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_pass_d(
    payload: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    model: Optional[str] = None,
    skip_llm: bool = False,
    progress: Any = None,
) -> dict[str, Any]:
    """LLM proposes cross-graph edges after surveying MG/TG/KG; validated keys only (coerce + apply)."""
    m = resolve_llm_model(model)
    dk = _d_key(payload, m or "")
    path = cache_dir / f"pass_d_{dk}.json"

    # Pre-compute the valid ent: key set from this payload (used to reject fabricated keys).
    valid_ent_keys = _extract_all_ent_keys(payload)

    hit = load_json(path)
    if hit and "edges" in hit:
        edges_raw = list(hit["edges"])
        edges, dropped = _filter_edges_by_ent_keys(edges_raw, valid_ent_keys)
        if dropped:
            log.warning(
                "pass_d cache: dropped %d edge(s) with fabricated ent: keys:\n  %s",
                len(dropped),
                "\n  ".join(dropped[:20]),
            )
        if progress is not None and hasattr(progress, "record_llm_exchange"):
            ek = edges[:120]
            resp: dict[str, Any] = {
                "self_confidence": float(hit.get("self_confidence") or 0.7),
                "edges": ek,
                "_from_disk_cache": True,
                "_edge_total": len(edges),
            }
            if dropped:
                resp["_dropped_invalid_ent_keys"] = len(dropped)
            ca_hit = str(hit.get("connection_assessment") or "").strip()
            if ca_hit:
                resp["connection_assessment"] = ca_hit[:2000]
            if len(edges) > 120:
                resp["edges"] = resp["edges"] + [{"_note": f"… 共 {len(edges)} 条边，此处仅预览前 120 条"}]
            progress.record_llm_exchange(
                phase="pass_d",
                step="Pass D（磁盘缓存）",
                model=m or "",
                messages=[
                    {"role": "system", "content": pass_d_system()},
                    {"role": "user", "content": pass_d_user(payload)},
                ],
                response=resp,
                from_cache=True,
                meta={"payload_chars": len(payload), "cache_file": path.name, "n_edges": len(edges)},
            )
        out_hit: dict[str, Any] = {
            "edges": edges,
            "self_confidence": float(hit.get("self_confidence") or 0.7),
        }
        ca0 = str(hit.get("connection_assessment") or "").strip()
        if ca0:
            out_hit["connection_assessment"] = ca0[:8000]
        return out_hit
    if skip_llm:
        if progress is not None and hasattr(progress, "record_llm_exchange"):
            progress.record_llm_exchange(
                phase="pass_d",
                step="Pass D（skip_llm）",
                model=m or "",
                messages=[
                    {"role": "system", "content": pass_d_system()},
                    {"role": "user", "content": pass_d_user(payload)},
                ],
                response={"edges": [], "skip_llm": True},
                from_cache=False,
                meta={"payload_chars": len(payload)},
            )
        return {"edges": [], "self_confidence": 0.75}

    messages = [
        {"role": "system", "content": pass_d_system()},
        {"role": "user", "content": pass_d_user(payload)},
    ]
    parsed = complete_json(
        messages,
        json_schema=PASS_D_JSON_SCHEMA,
        model=m,
        max_retries=4,
        temperature=0.0,
        enable_thinking=False,
        raw_coerce=coerce_pass_d_raw,
        http_timeout=knowledge_ingest_heavy_llm_timeout_sec(),
    )
    edges_pre = list(parsed.get("edges") or [])
    edges, dropped = _filter_edges_by_ent_keys(edges_pre, valid_ent_keys)
    if dropped:
        log.warning(
            "pass_d: dropped %d edge(s) with fabricated ent: keys:\n  %s",
            len(dropped),
            "\n  ".join(dropped[:20]),
        )
    sc_out = float(parsed.get("self_confidence") or 0.7)
    if progress is not None and hasattr(progress, "record_llm_exchange"):
        meta_live: dict[str, Any] = {
            "payload_chars": len(payload),
            "n_edges": len(edges),
        }
        if dropped:
            meta_live["n_dropped_invalid_ent_keys"] = len(dropped)
        progress.record_llm_exchange(
            phase="pass_d",
            step="Pass D",
            model=m or "",
            messages=[{"role": x["role"], "content": x["content"]} for x in messages],
            response=parsed,
            from_cache=False,
            meta=meta_live,
        )
    out_d: dict[str, Any] = {"edges": edges, "self_confidence": sc_out}
    ca = str(parsed.get("connection_assessment") or "").strip()
    if ca:
        out_d["connection_assessment"] = ca[:8000]
    save_json(path, out_d)
    return out_d


def _merge_surface_domain(tx: Any, *, entity_key: str, domain_key: str) -> bool:
    rec = tx.run(
        """
        MATCH (e:Entity {key: $ek})
        MATCH (dom:Domain {key: $dk})
        MERGE (e)-[r:SURFACE_DOMAIN]->(dom)
          ON CREATE SET r.zone = 'knowledge'
          ON MATCH  SET r.zone = coalesce(r.zone, 'knowledge')
        RETURN 1 AS ok LIMIT 1
        """,
        ek=entity_key,
        dk=domain_key,
    ).single()
    return rec is not None


def _merge_surface_operator(tx: Any, *, entity_key: str, operator_key: str) -> bool:
    rec = tx.run(
        """
        MATCH (e:Entity {key: $ek})
        MATCH (op:Operator {key: $ok})
        MERGE (e)-[r:SURFACE_OPERATOR]->(op)
          ON CREATE SET r.zone = 'knowledge'
          ON MATCH  SET r.zone = coalesce(r.zone, 'knowledge')
        RETURN 1 AS ok LIMIT 1
        """,
        ek=entity_key,
        ok=operator_key,
    ).single()
    return rec is not None


def _merge_surface_metric_pass_d(
    tx: Any, *, entity_key: str, metric_key: str, role: str, notes: str
) -> bool:
    rec = tx.run(
        """
        MATCH (e:Entity {key: $ek})
        MATCH (m:Metric {key: $mk})
        MERGE (e)-[r:SURFACE_METRIC]->(m)
          ON CREATE SET r.role = $role, r.notes = $notes, r.zone = 'knowledge'
          ON MATCH  SET r.role = $role, r.notes = $notes, r.zone = 'knowledge'
        RETURN 1 AS ok LIMIT 1
        """,
        ek=entity_key,
        mk=metric_key,
        role=role[:64],
        notes=notes[:500],
    ).single()
    return rec is not None


def _merge_surface_dimension_pass_d(
    tx: Any, *, entity_key: str, dim_key: str, role: str
) -> bool:
    rec = tx.run(
        """
        MATCH (e:Entity {key: $ek})
        MATCH (d:Dimension {key: $dk})
        MERGE (e)-[r:SURFACE_DIMENSION]->(d)
          ON CREATE SET r.role = $role, r.zone = 'knowledge'
          ON MATCH  SET r.role = $role, r.zone = 'knowledge'
        """,
        ek=entity_key,
        dk=dim_key,
        role=role[:64],
    ).single()
    return rec is not None


def _merge_task_about_entity(
    tx: Any, *, task_key: str, entity_key: str, notes: str
) -> bool:
    rec = tx.run(
        """
        MATCH (t:Task {key: $tk})
        MATCH (e:Entity {key: $ek})
        MERGE (t)-[r:ABOUT]->(e)
          ON CREATE SET r.zone = 'knowledge', r.notes = $notes
          ON MATCH SET r.zone = coalesce(r.zone, 'knowledge'), r.notes = $notes
        RETURN 1 AS ok LIMIT 1
        """,
        tk=task_key,
        ek=entity_key,
        notes=notes[:500],
    ).single()
    return rec is not None


def _merge_plan_concerns_entity(
    tx: Any, *, plan_key: str, entity_key: str, role: str
) -> bool:
    rec = tx.run(
        """
        MATCH (p:Step {key: $pk})
        MATCH (e:Entity {key: $ek})
        MERGE (p)-[r:CONCERNS]->(e)
          ON CREATE SET r.role = $role, r.zone = 'knowledge'
          ON MATCH SET r.role = $role, r.zone = coalesce(r.zone, 'knowledge')
        RETURN 1 AS ok LIMIT 1
        """,
        pk=plan_key,
        ek=entity_key,
        role=role[:64],
    ).single()
    return rec is not None


def _merge_has_instance_pass_d(tx: Any, *, fk: str, tk: str) -> bool:
    rec = tx.run(
        """
        MATCH (a:Entity {key: $fk})
        MATCH (b:Entity {key: $tk})
        MERGE (a)-[r:HAS_INSTANCE]->(b)
          ON CREATE SET r.zone = 'knowledge'
          ON MATCH SET r.zone = coalesce(r.zone, 'knowledge')
        RETURN 1 AS ok LIMIT 1
        """,
        fk=fk,
        tk=tk,
    ).single()
    return rec is not None


def apply_pass_d_edges(
    driver: Any,
    edges: list[dict[str, Any]],
    *,
    rep: IngestReport,
) -> int:
    """MERGE cross-graph (MG/TG↔KG) and KG-internal (§8) edges; skip rows where endpoints are missing."""
    n_ok = 0
    for e in edges:
        if not isinstance(e, dict):
            continue
        rt = str(e.get("rel_type") or "").strip().upper()
        fk = str(e.get("from_key") or "").strip()
        tk = str(e.get("to_key") or "").strip()
        if fk.startswith("ev:") or tk.startswith("ev:"):
            continue
        props = e if isinstance(e, dict) else {}
        notes = str(props.get("rationale") or "pass_d").strip()[:500]

        if rt in ("SURFACE_METRIC", "SURFACE_DIMENSION"):
            role = str(props.get("role") or "primary").strip()[:64] or "primary"
            if rt == "SURFACE_METRIC" and role not in _PASS_D_MET_ROLES:
                role = "primary"
            if rt == "SURFACE_DIMENSION" and role not in _PASS_D_DIM_ROLES:
                role = "primary"
        elif rt == "CONCERNS":
            role = str(props.get("role") or "subject").strip()[:64] or "subject"
            if role not in _PASS_D_CONCERNS_ROLES:
                role = "subject"
        else:
            role = ""

        def _one(tx: Any) -> bool:
            if rt == "SURFACE_METRIC":
                return _merge_surface_metric_pass_d(
                    tx, entity_key=fk, metric_key=tk, role=role, notes=notes
                )
            if rt == "SURFACE_DIMENSION":
                return _merge_surface_dimension_pass_d(
                    tx, entity_key=fk, dim_key=tk, role=role
                )
            if rt == "SURFACE_DOMAIN":
                return _merge_surface_domain(tx, entity_key=fk, domain_key=tk)
            if rt == "SURFACE_OPERATOR":
                return _merge_surface_operator(tx, entity_key=fk, operator_key=tk)
            if rt == "ABOUT":
                if fk.startswith("task:"):
                    return _merge_task_about_entity(
                        tx, task_key=fk, entity_key=tk, notes=notes
                    )
                return False
            if rt == "CONCERNS":
                return _merge_plan_concerns_entity(
                    tx, plan_key=fk, entity_key=tk, role=role
                )
            if rt == "RELATED_TO":
                st = str(props.get("relation_subtype") or "see_also").strip()[:64]
                if st not in _PASS_D_RELATED_SUB:
                    st = "see_also"
                link_ln = {
                    "from_key": fk,
                    "to_key": tk,
                    "description": str(props.get("description") or notes)[:500],
                    "relation_subtype": st,
                    "scope": str(props.get("scope") or "")[:200],
                    "sim_score": props.get("sim_score"),
                }
                _write_entity_links(tx, links=[link_ln])
                return (
                    tx.run(
                        """
                        MATCH (:Entity {key: $fk})-[:RELATED_TO]->(:Entity {key: $tk})
                        RETURN 1 AS ok LIMIT 1
                        """,
                        fk=fk,
                        tk=tk,
                    ).single()
                    is not None
                )
            if rt == "HAS_INSTANCE":
                return _merge_has_instance_pass_d(tx, fk=fk, tk=tk)
            return False

        try:
            with neo4j_session(driver) as s:
                ok = s.execute_write(lambda tx: _one(tx))
            if ok:
                n_ok += 1
                rep.decisions.append((f"{fk}->{tk}", rt, "MERGE_pass_d"))
            else:
                rep.skipped.append(f"pass_d_skip:{rt}:{fk}->{tk}")
        except Exception as exc:  # noqa: BLE001
            log.warning("pass_d edge failed %s %s→%s: %s", rt, fk, tk, exc)
            rep.skipped.append(f"pass_d_error:{rt}:{fk}:{exc}")
    return n_ok

"""Pass B (cluster surfaces) and Pass C (knowledge-layer subgraph only) with cache."""
from __future__ import annotations

import hashlib
import json
import re as _re
import unicodedata as _unicodedata
from pathlib import Path
from typing import Any, Optional

from qwenpaw_data.context.paths import knowledge_ingest_cache_dir

from ..config import CFG, knowledge_ingest_heavy_llm_timeout_sec
from ..openai_client import complete_json, resolve_llm_model
from ..utils import get_logger
from .prompts import (
    PASS_A_ENTITY_TYPES,
    PASS_B_CLUSTER_SCHEMA,
    PASS_B_JSON_SCHEMA,
    PASS_C_EDGES_SCHEMA,
    PASS_C_JSON_SCHEMA,
    pass_b_system,
    pass_b_user,
    pass_c_system,
    pass_c_user,
)

log = get_logger("knowledge.normalize")

DEFAULT_CACHE_DIR = knowledge_ingest_cache_dir()

_PASS_B_LIFECYCLE = frozenset(
    {
        "active",
        "archived",
        "frozen",
        "invalidated",
        "superseded",
        "needs_revalidation",
    }
)

_PASS_C_KG_REL_TYPES = frozenset(
    {
        "RELATED_TO",
        "HAS_INSTANCE",
    }
)

# ── Pass A entity type set (for type resolution in Pass B) ────────────────────

_PASS_A_ENT_TYPES: frozenset[str] = frozenset(PASS_A_ENTITY_TYPES)

# Minimal alias map: normalise LLM-invented synonyms back to the 12-enum.
# Mirrors the more complete table in extractor.py but kept local to avoid
# circular imports.
_ENT_TYPE_COERCE: dict[str, str] = {
    "model": "model_family",
    "ai_model": "model_family",
    "model_series": "model_family",
    "model_name": "model_version",
    "model_endpoint": "model_version",
    "model_snapshot": "model_version",
    "tool": "product",
    "ai_tool": "product",
    "software": "product",
    "competitor_product": "product",
    "product_feature": "feature",
    "capability": "feature",
    "framework": "concept",
    "protocol": "concept",
    "technology_concept": "concept",
    "organization": "company",
    "competitor": "company",
    "business_unit": "team",
    "organization_unit": "team",
    "user_segment": "customer",
    "pricing_plan": "subscription_plan",
}

# ── MG-surface heuristic filter ───────────────────────────────────────────────

_SQL_FUNC_RE = _re.compile(
    r"^\s*(SUM|COUNT|AVG|MAX|MIN|COALESCE|CAST|NULLIF|NVL|IFNULL|IIF)\s*\(",
    _re.IGNORECASE,
)
# snake_case identifiers that strongly suggest a metrics/dimensions column name
_MG_SLUG_SUFFIXES = (
    "_cnt", "_count", "_sum", "_avg", "_rate", "_ratio", "_pct",
    "_dim", "_id", "_dt", "_ds", "_key",
)


def _looks_like_mg_surface(surface: str) -> bool:
    """Heuristic: True when the surface is almost certainly a metadata-graph artefact
    (SQL aggregate, raw column/metric slug) rather than a knowledge-layer entity."""
    if _SQL_FUNC_RE.match(surface):
        return True
    # Pure lowercase snake_case identifier with MG-typical suffix
    if _re.match(r"^[a-z][a-z0-9_]*$", surface):
        for sfx in _MG_SLUG_SUFFIXES:
            if surface.endswith(sfx):
                return True
    # table.column pattern
    if _re.match(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$", surface):
        return True
    return False


# ── Key generation ────────────────────────────────────────────────────────────

def _slugify(name: str, max_len: int = 48) -> str:
    """Convert a canonical name to a stable ASCII slug.

    Pure-ASCII part of the NFKD-normalised name is lowercased and
    punctuation-collapsed.  Non-ASCII names (e.g. pure Chinese) fall back to a
    12-char SHA-1 prefix so the key is still deterministic and collision-resistant.
    """
    nfkd = _unicodedata.normalize("NFKD", name)
    ascii_part = nfkd.encode("ascii", "ignore").decode("ascii").strip()
    if ascii_part:
        slug = ascii_part.lower()
        slug = _re.sub(r"[\s_\.\+\/\\]+", "-", slug)
        slug = _re.sub(r"[^a-z0-9-]", "", slug)
        slug = _re.sub(r"-+", "-", slug).strip("-")
        return slug[:max_len] if slug else ""
    # Non-ASCII fallback: use SHA-1 prefix
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def _generate_cluster_key(entity_type: str, canonical_name: str) -> str:
    """Produce a deterministic ``ent:<type>:<slug>`` key.

    This is the ONLY place that creates canonical_key values in Pass B;
    the LLM is never asked to generate keys.
    """
    type_slug = _re.sub(r"[^a-z0-9]+", "_", entity_type.lower()).strip("_") or "entity"
    name_slug = _slugify(canonical_name)
    if not name_slug:
        name_slug = hashlib.sha1(canonical_name.encode("utf-8")).hexdigest()[:10]
    return f"ent:{type_slug}:{name_slug}"


# ── Type resolution ───────────────────────────────────────────────────────────

def _resolve_majority_type(type_votes: dict[str, int]) -> str:
    """Return the normalised entity_type with the most Pass A votes."""
    if not type_votes:
        return "other"
    best_raw = max(type_votes, key=lambda k: type_votes[k])
    t = best_raw.strip().lower().replace(" ", "_").replace("-", "_")
    if t in _PASS_A_ENT_TYPES:
        return t
    return _ENT_TYPE_COERCE.get(t, "other")


# ── Surface record helpers ────────────────────────────────────────────────────

def _build_llm_record(surface: str, record: dict[str, Any]) -> dict[str, Any]:
    """Build the per-surface JSON item sent to the LLM inside a Pass B batch."""
    evidence_quotes: list[str] = list(record.get("evidence_quotes") or [])
    # Pick the shortest non-empty evidence quote
    best_ev = min((q for q in evidence_quotes if q), key=len, default="")
    row: dict[str, Any] = {
        "surface": surface,
        "evidence": best_ev,
        "aliases": list(record.get("aliases") or []),
        "chunk_count": int(record.get("chunk_count") or 1),
    }
    defn = record.get("definition")
    if defn:
        row["definition"] = str(defn)[:200]
    return row


def _post_process_clusters(
    raw_clusters: list[dict[str, Any]],
    entity_type: str,
    batch_surfaces: list[str],
    surface_records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assign ``canonical_key`` + ``entity_type``; recover any surfaces the LLM dropped."""
    result: list[dict[str, Any]] = []
    covered: set[str] = set()

    for c in raw_clusters:
        name = str(c.get("canonical_name") or "").strip()
        if not name:
            continue
        surfaces = [str(s).strip() for s in (c.get("surfaces") or []) if str(s).strip()]
        if not surfaces:
            surfaces = [name]
        ck = _generate_cluster_key(entity_type, name)
        row: dict[str, Any] = {
            "canonical_key": ck,
            "canonical_name": name,
            "entity_type": entity_type,
            "surfaces": surfaces,
            "aliases": [str(a).strip() for a in (c.get("aliases") or []) if str(a).strip()],
            "best_evidence_quote": str(c.get("best_evidence_quote") or "").strip(),
        }
        ls_raw = str(c.get("lifecycle_state") or "").strip()
        if ls_raw in _PASS_B_LIFECYCLE:
            row["lifecycle_state"] = ls_raw
        # Carry best definition from surface records
        best_def = ""
        for s in surfaces:
            rec = surface_records.get(s, {})
            d = str(rec.get("definition") or "").strip()
            if d and (not best_def or len(d) > len(best_def)):
                best_def = d
        if best_def:
            row["definition"] = best_def
        covered.update(surfaces)
        result.append(row)

    # Recovery pass: surfaces the LLM silently dropped → one-to-one fallback cluster
    for s in batch_surfaces:
        if s not in covered:
            rec = surface_records.get(s, {})
            ck = _generate_cluster_key(entity_type, s)
            if not ck:
                ck = f"ent:{entity_type}:{hashlib.sha1(s.encode()).hexdigest()[:10]}"
            fallback: dict[str, Any] = {
                "canonical_key": ck,
                "canonical_name": s,
                "entity_type": entity_type,
                "surfaces": [s],
                "aliases": list(rec.get("aliases") or []),
                "best_evidence_quote": (list(rec.get("evidence_quotes") or []) or [""])[0],
            }
            defn = str(rec.get("definition") or "").strip()
            if defn:
                fallback["definition"] = defn
            result.append(fallback)
            log.warning(
                "pass_b: surface %r not covered by LLM for type=%r; created 1:1 fallback cluster %s",
                s,
                entity_type,
                ck,
            )
    return result


def _dedup_clusters(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge clusters that received the same ``canonical_key`` (union of surfaces/aliases/source_docs)."""
    by_key: dict[str, dict[str, Any]] = {}
    for c in clusters:
        ck = str(c.get("canonical_key") or "").strip()
        if not ck:
            continue
        if ck not in by_key:
            by_key[ck] = dict(c)
            by_key[ck]["surfaces"] = list(c.get("surfaces") or [])
            by_key[ck]["aliases"] = list(c.get("aliases") or [])
            by_key[ck]["source_docs"] = list(c.get("source_docs") or [])
        else:
            existing = by_key[ck]
            surf_set = set(existing["surfaces"])
            for s in c.get("surfaces") or []:
                if s not in surf_set:
                    existing["surfaces"].append(s)
                    surf_set.add(s)
            alias_set = set(existing["aliases"])
            for a in c.get("aliases") or []:
                if a not in alias_set:
                    existing["aliases"].append(a)
                    alias_set.add(a)
            doc_set = set(existing["source_docs"])
            for d in c.get("source_docs") or []:
                if d not in doc_set:
                    existing["source_docs"].append(d)
                    doc_set.add(d)
    return list(by_key.values())


# ── Cache key ─────────────────────────────────────────────────────────────────

def _b_key_v2(surfaces: list[str], entity_type: str, model: str) -> str:
    """Cache key for type-grouped enriched Pass B calls.

    Uses a version suffix to avoid collisions with earlier Pass B cache keys.
    """
    payload = (
        json.dumps(sorted(set(surfaces)), ensure_ascii=False)
        + f"\n{entity_type}\n{model}\nv2"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def coerce_pass_b_raw(parsed: Any) -> Any:
    """Fill Pass B schema gaps before jsonschema validation (models often omit self_confidence / cluster fields)."""
    if not isinstance(parsed, dict):
        return parsed
    out: dict[str, Any] = dict(parsed)
    sc = out.get("self_confidence")
    try:
        out["self_confidence"] = float(sc) if sc is not None else 0.7
    except (TypeError, ValueError):
        out["self_confidence"] = 0.7
    out["self_confidence"] = max(0.0, min(1.0, float(out["self_confidence"])))

    raw_clusters = out.get("clusters")
    if not isinstance(raw_clusters, list):
        out["clusters"] = []
        return out

    fixed: list[dict[str, Any]] = []
    for c in raw_clusters:
        if not isinstance(c, dict):
            continue
        ck = str(c.get("canonical_key") or "").strip()
        if not ck:
            continue
        surfaces_raw = c.get("surfaces")
        if not isinstance(surfaces_raw, list):
            surfaces_raw = []
        surfaces = [str(x).strip() for x in surfaces_raw if str(x).strip()]
        aliases_raw = c.get("aliases")
        if not isinstance(aliases_raw, list):
            aliases_raw = []
        aliases = [str(x).strip() for x in aliases_raw if str(x).strip()]
        name = str(c.get("canonical_name") or "").strip()
        if not name:
            name = surfaces[0] if surfaces else ck.rsplit(":", 1)[-1].replace("-", " ") or ck
        et = str(c.get("entity_type") or "").strip() or "unknown"
        quote = str(c.get("best_evidence_quote") or "").strip()
        if not surfaces:
            surfaces = [name] if name else [ck]
        row: dict[str, Any] = {
            "canonical_key": ck,
            "canonical_name": name,
            "entity_type": et,
            "surfaces": surfaces,
            "aliases": aliases,
            "best_evidence_quote": quote,
        }
        ls_raw = str(c.get("lifecycle_state") or "").strip()
        if ls_raw in _PASS_B_LIFECYCLE:
            row["lifecycle_state"] = ls_raw
        fixed.append(row)
    out["clusters"] = fixed
    return out


def coerce_pass_b_cluster_raw(parsed: Any) -> Any:
    """Coerce new-style Pass B LLM output (no ``canonical_key`` / ``entity_type`` from model)."""
    if not isinstance(parsed, dict):
        return parsed
    out: dict[str, Any] = dict(parsed)
    sc = out.get("self_confidence")
    try:
        out["self_confidence"] = max(0.0, min(1.0, float(sc) if sc is not None else 0.7))
    except (TypeError, ValueError):
        out["self_confidence"] = 0.7

    raw_clusters = out.get("clusters")
    if not isinstance(raw_clusters, list):
        out["clusters"] = []
        return out

    fixed: list[dict[str, Any]] = []
    for c in raw_clusters:
        if not isinstance(c, dict):
            continue
        name = str(c.get("canonical_name") or "").strip()
        if not name:
            continue
        surfaces_raw = c.get("surfaces")
        surfaces = [str(x).strip() for x in (surfaces_raw or []) if str(x).strip()]
        if not surfaces:
            surfaces = [name]
        aliases_raw = c.get("aliases")
        aliases = [str(x).strip() for x in (aliases_raw or []) if str(x).strip()]
        quote = str(c.get("best_evidence_quote") or "").strip()
        row: dict[str, Any] = {
            "canonical_name": name,
            "surfaces": surfaces,
            "aliases": aliases,
            "best_evidence_quote": quote,
        }
        ls_raw = str(c.get("lifecycle_state") or "").strip()
        if ls_raw in _PASS_B_LIFECYCLE:
            row["lifecycle_state"] = ls_raw
        fixed.append(row)

    out["clusters"] = fixed
    return out


def collect_surface_records(
    pass_a_results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Collect enriched surface records from all Pass A rows.

    Returns a mapping ``surface → record`` where each record contains:
    - ``types``: dict[str, int] — Pass A type → vote count across chunks
    - ``aliases``: list[str] — aliases seen in Pass A (other surfaces linked to this one)
    - ``evidence_quotes``: list[str] — up to 3 shortest evidence quotes
    - ``definition``: str | None — first non-empty definition seen
    - ``chunk_count``: int — total number of Pass A extractions mentioning this surface
    """
    records: dict[str, dict[str, Any]] = {}

    def _ensure(surface: str) -> dict[str, Any]:
        if surface not in records:
            records[surface] = {
                "surface": surface,
                "types": {},
                "aliases": [],
                "evidence_quotes": [],
                "definition": None,
                "chunk_count": 0,
                "source_docs": set(),
            }
        return records[surface]

    for row in pass_a_results:
        row_doc = str(row.get("_source_doc") or "").strip()
        for ent in row.get("entities") or []:
            if not isinstance(ent, dict):
                continue
            surface = str(ent.get("surface") or "").strip()
            if not surface:
                continue
            etype = str(ent.get("type") or "other").strip()
            evidence = str(ent.get("evidence_quote") or "").strip()
            definition = str(ent.get("definition") or "").strip() or None

            rec = _ensure(surface)
            rec["types"][etype] = rec["types"].get(etype, 0) + 1
            rec["chunk_count"] += 1
            if row_doc:
                rec["source_docs"].add(row_doc)

            if evidence and evidence not in rec["evidence_quotes"]:
                # Keep up to 3; prefer shorter ones
                rec["evidence_quotes"].append(evidence)
                rec["evidence_quotes"].sort(key=len)
                if len(rec["evidence_quotes"]) > 3:
                    rec["evidence_quotes"] = rec["evidence_quotes"][:3]

            if definition and not rec["definition"]:
                rec["definition"] = definition

            # Register aliases: give them a type vote and link back
            aliases_raw = ent.get("aliases") or []
            for alias_raw in aliases_raw:
                alias = str(alias_raw).strip()
                if not alias or alias == surface:
                    continue
                alias_rec = _ensure(alias)
                alias_rec["types"][etype] = alias_rec["types"].get(etype, 0) + 1
                # Don't increment chunk_count for aliases — they weren't the primary surface
                # but DO propagate source_docs so aliases inherit doc provenance.
                if row_doc:
                    alias_rec["source_docs"].add(row_doc)
                if surface not in alias_rec["aliases"]:
                    alias_rec["aliases"].append(surface)
                if alias not in rec["aliases"]:
                    rec["aliases"].append(alias)

    return records


def coerce_pass_c_raw(parsed: Any) -> Any:
    """Fill Pass C schema gaps; drop :Event nodes and non-entity-internal rels (ABOUT, CAUSES)."""
    if not isinstance(parsed, dict):
        return parsed
    out: dict[str, Any] = dict(parsed)
    sc = out.get("self_confidence")
    try:
        out["self_confidence"] = float(sc) if sc is not None else 0.7
    except (TypeError, ValueError):
        out["self_confidence"] = 0.7
    out["self_confidence"] = max(0.0, min(1.0, float(out["self_confidence"])))
    out.pop("links", None)

    kt = out.get("kg_topology")
    if not isinstance(kt, dict):
        kt = {}
    nodes_raw = kt.get("nodes")
    if not isinstance(nodes_raw, list):
        nodes_raw = []
    nodes_out: list[dict[str, Any]] = []
    for n in nodes_raw:
        if not isinstance(n, dict):
            continue
        label_raw = str(n.get("label") or "").strip()
        if label_raw.lower() != "entity":
            continue
        key = str(n.get("key") or n.get("canonical_key") or "").strip()
        if not key:
            continue
        if not key.startswith("ent:"):
            key = f"ent:{key}"
        props = n.get("properties")
        if not isinstance(props, dict):
            props = {}
        name = str(props.get("name") or key).strip()[:2000]
        al = props.get("aliases")
        if not isinstance(al, list):
            al = []
        aliases = [str(x).strip() for x in al if str(x).strip()][:64]
        if not aliases:
            aliases = [name] if name else [key]
        desc = str(props.get("description") or "")[:4000]
        et = str(props.get("type") or "concept")[:120]
        ls = str(props.get("lifecycle_state") or "").strip()
        if ls not in _PASS_B_LIFECYCLE:
            ls = "active"
        nodes_out.append(
            {
                "label": "Entity",
                "key": key,
                "properties": {
                    "name": name,
                    "aliases": aliases,
                    "type": et,
                    "description": desc,
                    "lifecycle_state": ls,
                },
            }
        )

    rels_raw = kt.get("relationships")
    if not isinstance(rels_raw, list):
        rels_raw = []
    rels_out: list[dict[str, Any]] = []
    for r in rels_raw:
        if not isinstance(r, dict):
            continue
        rt = str(r.get("rel_type") or "").strip().upper()
        if rt not in _PASS_C_KG_REL_TYPES:
            continue
        fk = str(r.get("from_key") or "").strip()
        tk = str(r.get("to_key") or "").strip()
        if fk and not fk.startswith("ent:"):
            fk = f"ent:{fk}"
        if tk and not tk.startswith("ent:"):
            tk = f"ent:{tk}"
        if not fk.startswith("ent:") or not tk.startswith("ent:"):
            continue
        rp = r.get("properties")
        if not isinstance(rp, dict):
            rp = {}
        rels_out.append({"rel_type": rt, "from_key": fk, "to_key": tk, "properties": dict(rp)})

    out["kg_topology"] = {"nodes": nodes_out, "relationships": rels_out}
    return out


def _c_key(payload: str, model: str) -> str:
    return hashlib.sha1((payload + "\n" + model).encode("utf-8")).hexdigest()


# Bumped when Pass C prompts / coercion change meaningfully (cache key does not include full messages).
# 20260514-edges-only: nodes are now generated mechanically; LLM outputs edges only.
_PASS_C_CACHE_VERSION = "20260514-edges-only"


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


def run_pass_b(
    surface_records: dict[str, dict[str, Any]],
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    model: Optional[str] = None,
    batch_size: int = 100,
    skip_llm: bool = False,
    progress: Any = None,
    filter_mg: bool = True,
) -> list[dict[str, Any]]:
    """Cluster enriched surface records → canonical ``ent:`` clusters.

    Key differences from the legacy approach:

    1. **MG filter** — surfaces that look like SQL aggregates or column identifiers are
       dropped before the LLM sees them (``filter_mg=True`` by default).
    2. **Type grouping** — surfaces are grouped by their majority Pass A entity type so
       each LLM call only sees entities of the same semantic class.  This prevents
       cross-type conflation (model_family ≠ model_version) and simplifies the model's task.
    3. **Code-owned key generation** — ``canonical_key`` and ``entity_type`` are assigned
       deterministically by ``_generate_cluster_key``; the LLM is never asked to produce them.
    4. **Coverage guarantee** — any surface the LLM silently drops is recovered by
       ``_post_process_clusters`` as a 1:1 fallback cluster.
    5. **Within-type deduplication** — clusters that resolve to the same key (across
       sub-batches of a type group) are merged by ``_dedup_clusters``.
    """
    m = resolve_llm_model(model)

    # ── 1. Optional MG pre-filter ─────────────────────────────────────────────
    if filter_mg:
        surface_records = {
            s: r for s, r in surface_records.items() if not _looks_like_mg_surface(s)
        }

    if not surface_records:
        return []

    # ── 2. Group surfaces by majority Pass A entity type ──────────────────────
    type_groups: dict[str, list[str]] = {}
    for surface, record in surface_records.items():
        et = _resolve_majority_type(record.get("types") or {})
        type_groups.setdefault(et, []).append(surface)

    all_clusters: list[dict[str, Any]] = []

    # ── 3. Per-type LLM passes ────────────────────────────────────────────────
    for entity_type, type_surfaces in sorted(type_groups.items()):
        type_surfaces_sorted = sorted(set(type_surfaces))
        n_type_surfaces = len(type_surfaces_sorted)
        n_type_batches = max(1, (n_type_surfaces + batch_size - 1) // batch_size)

        type_clusters: list[dict[str, Any]] = []

        for bi, batch_start in enumerate(range(0, n_type_surfaces, batch_size)):
            batch_surfaces = type_surfaces_sorted[batch_start : batch_start + batch_size]
            batch_num = bi + 1
            step_label = (
                f"Pass B [{entity_type}] 批次 {batch_num}/{n_type_batches}"
                if n_type_batches > 1
                else f"Pass B [{entity_type}]"
            )

            # Build enriched record list for this batch
            batch_llm_records = [
                _build_llm_record(s, surface_records[s])
                for s in batch_surfaces
                if s in surface_records
            ]

            bk = _b_key_v2(batch_surfaces, entity_type, m or "")
            path = cache_dir / f"pass_b_{bk}.json"

            # ── Cache hit ────────────────────────────────────────────────────
            hit = load_json(path)
            if hit and "clusters" in hit:
                clist = list(hit["clusters"] or [])
                type_clusters.extend(clist)
                if progress is not None and hasattr(progress, "record_llm_exchange"):
                    preview: dict[str, Any] = {
                        "self_confidence": float(hit.get("self_confidence") or 0.7),
                        "clusters": clist[:80],
                        "_from_disk_cache": True,
                        "_cluster_total": len(clist),
                    }
                    if len(clist) > 80:
                        preview["clusters"] = preview["clusters"] + [
                            {"_note": f"… 共 {len(clist)} 个 cluster，此处仅预览前 80 个"}
                        ]
                    progress.record_llm_exchange(
                        phase="pass_b",
                        step=f"{step_label}（磁盘缓存）",
                        model=m or "",
                        messages=[
                            {"role": "system", "content": pass_b_system(entity_type)},
                            {
                                "role": "user",
                                "content": pass_b_user(
                                    json.dumps(batch_llm_records, ensure_ascii=False),
                                    entity_type,
                                ),
                            },
                        ],
                        response=preview,
                        from_cache=True,
                        meta={
                            "n_surfaces": len(batch_surfaces),
                            "entity_type": entity_type,
                            "cache_file": path.name,
                        },
                    )
                continue

            # ── skip_llm: synthetic 1:1 clusters ─────────────────────────────
            if skip_llm:
                for s in batch_surfaces:
                    rec = surface_records.get(s, {})
                    ck = _generate_cluster_key(entity_type, s)
                    type_clusters.append(
                        {
                            "canonical_key": ck,
                            "canonical_name": s,
                            "entity_type": entity_type,
                            "surfaces": [s],
                            "aliases": list(rec.get("aliases") or []),
                            "best_evidence_quote": (
                                list(rec.get("evidence_quotes") or []) or [""]
                            )[0],
                        }
                    )
                if progress is not None and hasattr(progress, "record_llm_exchange"):
                    progress.record_llm_exchange(
                        phase="pass_b",
                        step=f"{step_label}（skip_llm）",
                        model=m or "",
                        messages=[
                            {"role": "system", "content": pass_b_system(entity_type)},
                            {
                                "role": "user",
                                "content": pass_b_user(
                                    json.dumps(batch_llm_records, ensure_ascii=False),
                                    entity_type,
                                ),
                            },
                        ],
                        response={"skip_llm": True, "n_synthetic_clusters": len(batch_surfaces)},
                        from_cache=False,
                        meta={"n_surfaces": len(batch_surfaces), "entity_type": entity_type},
                    )
                continue

            # ── Live LLM call ─────────────────────────────────────────────────
            messages = [
                {"role": "system", "content": pass_b_system(entity_type)},
                {
                    "role": "user",
                    "content": pass_b_user(
                        json.dumps(batch_llm_records, ensure_ascii=False),
                        entity_type,
                    ),
                },
            ]
            parsed = complete_json(
                messages,
                json_schema=PASS_B_CLUSTER_SCHEMA,
                model=m,
                max_retries=4,
                temperature=0.0,
                enable_thinking=False,
                raw_coerce=coerce_pass_b_cluster_raw,
                http_timeout=knowledge_ingest_heavy_llm_timeout_sec(),
            )
            raw_clusters = list(parsed.get("clusters") or [])
            sc_val = float(parsed.get("self_confidence") or 0.7)

            # Post-process: assign keys, recover dropped surfaces
            final_clusters = _post_process_clusters(
                raw_clusters, entity_type, batch_surfaces, surface_records
            )
            type_clusters.extend(final_clusters)

            if progress is not None and hasattr(progress, "record_llm_exchange"):
                progress.record_llm_exchange(
                    phase="pass_b",
                    step=step_label,
                    model=m or "",
                    messages=[{"role": x["role"], "content": x["content"]} for x in messages],
                    response=parsed,
                    from_cache=False,
                    meta={
                        "n_surfaces": len(batch_surfaces),
                        "entity_type": entity_type,
                        "n_clusters": len(final_clusters),
                    },
                )

            # Cache the post-processed clusters (already have canonical_key / entity_type)
            save_json(
                path,
                {
                    "clusters": final_clusters,
                    "batch": batch_surfaces,
                    "entity_type": entity_type,
                    "self_confidence": sc_val,
                },
            )

        # Within-type deduplication (merges clusters that got the same key)
        type_clusters = _dedup_clusters(type_clusters)
        all_clusters.extend(type_clusters)

    # Attach / refresh source_docs from surface_records for every cluster.
    # This covers LLM-live, disk-cached, and skip_llm paths uniformly so
    # downstream writers always have accurate per-document provenance.
    for cluster in all_clusters:
        docs: set[str] = set()
        for surface in cluster.get("surfaces") or []:
            docs.update(surface_records.get(surface, {}).get("source_docs") or set())
        cluster["source_docs"] = sorted(docs)

    return all_clusters


def _clusters_to_nodes(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mechanically convert Pass B clusters to :Entity node specs (no LLM involvement).

    This is the authoritative source for kg_topology.nodes, eliminating the
    0-nodes failure mode where the LLM returned an empty nodes array.
    Copies: canonical_key → key, canonical_name → name, entity_type → type,
    aliases + surfaces → aliases, lifecycle_state.
    """
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in clusters:
        if not isinstance(c, dict):
            continue
        key = str(c.get("canonical_key") or "").strip()
        if not key.startswith("ent:") or key in seen:
            continue
        seen.add(key)
        name = str(c.get("canonical_name") or "").strip()[:2000] or key
        entity_type = str(c.get("entity_type") or "concept").strip()[:120]
        surfaces = [str(s).strip() for s in (c.get("surfaces") or []) if str(s).strip()]
        aliases = [str(a).strip() for a in (c.get("aliases") or []) if str(a).strip()]
        # Name first, then declared aliases, then surfaces — deduplicated, capped
        all_aliases = list(dict.fromkeys([name] + aliases + surfaces))[:64]
        ls = str(c.get("lifecycle_state") or "").strip()
        if ls not in _PASS_B_LIFECYCLE:
            ls = "active"
        desc = str(c.get("definition") or c.get("best_evidence_quote") or "").strip()[:4000]
        source_docs = [str(d).strip() for d in (c.get("source_docs") or []) if str(d).strip()]
        nodes.append(
            {
                "label": "Entity",
                "key": key,
                "source_docs": source_docs,
                "properties": {
                    "name": name,
                    "aliases": all_aliases,
                    "type": entity_type,
                    "description": desc,
                    "lifecycle_state": ls,
                },
            }
        )
    return nodes


def _build_pass_c_entity_rows(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the compact entity rows sent to the LLM for edge inference.

    Only key + name + type — the LLM does not need full cluster details for
    structural relationship inference.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in clusters:
        if not isinstance(c, dict):
            continue
        key = str(c.get("canonical_key") or "").strip()
        if not key.startswith("ent:") or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "key": key,
                "name": str(c.get("canonical_name") or "").strip()[:200],
                "type": str(c.get("entity_type") or "concept").strip()[:80],
            }
        )
    return rows


def coerce_pass_c_edges_raw(parsed: Any) -> Any:
    """Coerce edges-only Pass C LLM output.

    Accepts both the new {relationships, self_confidence} format and the legacy
    {kg_topology: {nodes, relationships}} format (for cache backward compat).
    Enforces ent:→ent: key discipline; drops non-KG edge types.
    """
    if not isinstance(parsed, dict):
        return parsed
    out: dict[str, Any] = dict(parsed)
    sc = out.get("self_confidence")
    try:
        out["self_confidence"] = max(0.0, min(1.0, float(sc) if sc is not None else 0.7))
    except (TypeError, ValueError):
        out["self_confidence"] = 0.7

    # Support both new format (relationships key) and old format (kg_topology.relationships)
    rels_raw = out.get("relationships")
    if not isinstance(rels_raw, list):
        kt = out.get("kg_topology")
        rels_raw = kt.get("relationships") if isinstance(kt, dict) else None
        if not isinstance(rels_raw, list):
            rels_raw = []

    rels_out: list[dict[str, Any]] = []
    for r in rels_raw:
        if not isinstance(r, dict):
            continue
        rt = str(r.get("rel_type") or "").strip().upper()
        if rt not in _PASS_C_KG_REL_TYPES:
            continue
        fk = str(r.get("from_key") or "").strip()
        tk = str(r.get("to_key") or "").strip()
        if fk and not fk.startswith("ent:"):
            fk = f"ent:{fk}"
        if tk and not tk.startswith("ent:"):
            tk = f"ent:{tk}"
        if not fk.startswith("ent:") or not tk.startswith("ent:"):
            continue
        rp = r.get("properties")
        if not isinstance(rp, dict):
            rp = {}
        rels_out.append({"rel_type": rt, "from_key": fk, "to_key": tk, "properties": dict(rp)})

    out["relationships"] = rels_out
    out.pop("kg_topology", None)
    out.pop("nodes", None)
    out.pop("links", None)
    return out


def _pass_c_dedup_rels(rels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate Pass C relationships by (rel_type, from_key, to_key)."""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for r in rels:
        if not isinstance(r, dict):
            continue
        trip: tuple[str, str, str] = (
            str(r.get("rel_type") or "").upper(),
            str(r.get("from_key") or ""),
            str(r.get("to_key") or ""),
        )
        if trip in seen:
            continue
        seen.add(trip)
        out.append(r)
    return out


def _pass_c_cluster_rows_from_clusters(
    clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Legacy helper retained for backward compatibility. New code uses _build_pass_c_entity_rows."""
    rows: list[dict[str, Any]] = []
    for c in clusters:
        if not isinstance(c, dict):
            continue
        ck = str(c.get("canonical_key") or "").strip()
        if not ck:
            continue
        row_c: dict[str, Any] = {
            "canonical_key": ck,
            "canonical_name": str(c.get("canonical_name") or ""),
            "entity_type": str(c.get("entity_type") or ""),
        }
        ls_c = str(c.get("lifecycle_state") or "").strip()
        if ls_c in _PASS_B_LIFECYCLE:
            row_c["lifecycle_state"] = ls_c
        rows.append(row_c)
    return rows


def build_pass_c_payload(clusters: list[dict[str, Any]]) -> str:
    """Build compact {key, name, type} entity rows payload for the LLM (edges-only task)."""
    rows = _build_pass_c_entity_rows(clusters)
    return json.dumps(rows, ensure_ascii=False)


def build_pass_c_prior_topology_block(
    entity_keys: list[str],
    prior_rels: list[dict[str, Any]],
    *,
    max_entity_keys: int = 280,
    max_rels: int = 400,
) -> dict[str, Any]:
    """Compact prior graph state for Pass C outer refinement rounds.

    Provides entity keys from all batches + relationships already proposed,
    so the LLM can add cross-batch edges without duplicating existing ones.
    """
    trimmed_keys = sorted(set(entity_keys))[: int(max_entity_keys)]
    rels_out: list[dict[str, Any]] = []
    for r in prior_rels:
        if not isinstance(r, dict):
            continue
        if len(rels_out) >= int(max_rels):
            break
        rt = str(r.get("rel_type") or "").strip().upper()
        fk = str(r.get("from_key") or "").strip()
        tk = str(r.get("to_key") or "").strip()
        if not fk.startswith("ent:") or not tk.startswith("ent:"):
            continue
        rels_out.append({"rel_type": rt, "from_key": fk, "to_key": tk})
    return {"entity_keys": trimmed_keys, "relationships": rels_out}


def build_pass_c_payload_for_round(
    batch: list[dict[str, Any]],
    *,
    outer_round: int,
    max_outer_rounds: int,
    prior_entity_keys: Optional[list[str]] = None,
    prior_rels: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Round 1: compact JSON array (cache-friendly). Round 2+: wrapped object with prior topology."""
    rows = _build_pass_c_entity_rows(batch)
    if prior_entity_keys is None or outer_round <= 1:
        return json.dumps(rows, ensure_ascii=False)
    prior_block = build_pass_c_prior_topology_block(prior_entity_keys, prior_rels or [])
    body: dict[str, Any] = {
        "entity_batch": rows,
        "pass_c_outer_round": int(outer_round),
        "pass_c_max_outer_rounds": int(max_outer_rounds),
        "prior_entity_keys": prior_block["entity_keys"],
        "prior_relationships": prior_block["relationships"],
    }
    return json.dumps(body, ensure_ascii=False)


def pass_c_cluster_slices(
    clusters: list[dict[str, Any]],
    batch_size: int,
) -> list[list[dict[str, Any]]]:
    if not clusters:
        return []
    if batch_size <= 0:
        return [clusters]
    return [clusters[i : i + batch_size] for i in range(0, len(clusters), batch_size)]


def merge_pass_c_outputs(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge several Pass C result objects.

    Accepts both new {relationships, self_confidence} format and the legacy
    {kg_topology: {nodes, relationships}} format for backward compatibility.
    Returns a merged object in the same format as its first non-empty input.
    Nodes (if present from mechanical generation) are merged by key (last wins).
    Relationships are deduplicated by (rel_type, from_key, to_key).
    """
    if not parts:
        return {"kg_topology": {"nodes": [], "relationships": []}, "self_confidence": 0.75}

    all_rels: list[dict[str, Any]] = []
    nodes_by_key: dict[str, dict[str, Any]] = {}
    scs: list[float] = []

    for p in parts:
        if not isinstance(p, dict):
            continue
        try:
            scs.append(max(0.0, min(1.0, float(p.get("self_confidence") or 0.7))))
        except (TypeError, ValueError):
            scs.append(0.7)
        # New format: relationships key
        if "relationships" in p:
            for r in (p.get("relationships") or []):
                if isinstance(r, dict):
                    all_rels.append(r)
        # Legacy format: kg_topology
        kt = p.get("kg_topology")
        if isinstance(kt, dict):
            for n in (kt.get("nodes") or []):
                if isinstance(n, dict):
                    k = str(n.get("key") or "").strip()
                    if k:
                        nodes_by_key[k] = dict(n)
            for r in (kt.get("relationships") or []):
                if isinstance(r, dict):
                    all_rels.append(r)

    merged_sc = min(scs) if scs else 0.75
    deduped_rels = _pass_c_dedup_rels(all_rels)

    if nodes_by_key:
        return {
            "kg_topology": {"nodes": list(nodes_by_key.values()), "relationships": deduped_rels},
            "self_confidence": merged_sc,
        }
    return {"relationships": deduped_rels, "self_confidence": merged_sc}


def _pass_c_entity_rows_from_payload(payload: str) -> list[dict[str, Any]]:
    """Parse Pass C LLM payload and return the entity rows (new or legacy format)."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if isinstance(data, list):
        rows_raw = data
    elif isinstance(data, dict):
        # New format: entity_batch; legacy format: cluster_batch
        rows_raw = data.get("entity_batch") or data.get("cluster_batch")
        if not isinstance(rows_raw, list):
            rows_raw = []
    else:
        return []
    out: list[dict[str, Any]] = []
    for x in rows_raw:
        if not isinstance(x, dict):
            continue
        # Accept both new (key) and legacy (canonical_key) format
        if str(x.get("key") or x.get("canonical_key") or "").strip():
            out.append(x)
    return out


def _pass_c_progress_trace_step(
    variant: str,
    batch_index: Optional[int],
    n_batches: Optional[int],
    outer_round: Optional[int],
    max_outer_rounds: Optional[int],
) -> str:
    base = {"cache": "Pass C（磁盘缓存）", "skip": "Pass C（skip_llm）"}.get(variant, "Pass C")
    tail: list[str] = []
    if outer_round is not None and max_outer_rounds is not None and max_outer_rounds > 1:
        tail.append(f"轮{outer_round}/{max_outer_rounds}")
    if batch_index is not None and n_batches is not None and n_batches > 1:
        tail.append(f"批{batch_index}/{n_batches}")
    return base + (" · " + " · ".join(tail) if tail else "")


def run_pass_c(
    payload: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    model: Optional[str] = None,
    skip_llm: bool = False,
    progress: Any = None,
    batch_index: Optional[int] = None,
    n_batches: Optional[int] = None,
    outer_round: Optional[int] = None,
    max_outer_rounds: Optional[int] = None,
) -> dict[str, Any]:
    """Run one Pass C LLM call (edges-only task).

    Returns ``{relationships: [...], self_confidence: N}``.
    Entity nodes are no longer generated by the LLM; use ``_clusters_to_nodes``
    for mechanical node generation (done once in ``run_pass_c_batched``).
    """
    m = resolve_llm_model(model)
    ck = _c_key(_PASS_C_CACHE_VERSION + "\n" + payload, m or "")
    path = cache_dir / f"pass_c_{ck}.json"
    user_content = pass_c_user(
        payload,
        batch_index=batch_index,
        n_batches=n_batches,
        outer_round=outer_round,
        max_outer_rounds=max_outer_rounds,
    )
    n_entity_rows = len(_pass_c_entity_rows_from_payload(payload))

    # ── Cache hit ─────────────────────────────────────────────────────────────
    hit = load_json(path)
    if hit and isinstance(hit, dict) and "relationships" in hit:
        hit_c = coerce_pass_c_edges_raw(hit)
        rels = list(hit_c.get("relationships") or [])
        sc_hit = float(hit_c.get("self_confidence") or 0.7)
        if progress is not None and hasattr(progress, "record_llm_exchange"):
            resp: dict[str, Any] = {
                "self_confidence": sc_hit,
                "relationships": rels[:120],
                "_from_disk_cache": True,
                "_kg_rel_total": len(rels),
            }
            if len(rels) > 120:
                resp["_preview_truncated"] = True
            progress.record_llm_exchange(
                phase="pass_c",
                step=_pass_c_progress_trace_step(
                    "cache", batch_index, n_batches, outer_round, max_outer_rounds
                ),
                model=m or "",
                messages=[
                    {"role": "system", "content": pass_c_system()},
                    {"role": "user", "content": user_content},
                ],
                response=resp,
                from_cache=True,
                meta={
                    "payload_chars": len(payload),
                    "cache_file": path.name,
                    "n_entity_rows": n_entity_rows,
                    "n_kg_rels": len(rels),
                    "pass_c_batch_index": batch_index,
                    "pass_c_n_batches": n_batches,
                    "pass_c_outer_round": outer_round,
                    "pass_c_max_outer_rounds": max_outer_rounds,
                },
            )
        return {"relationships": rels, "self_confidence": sc_hit}

    # ── skip_llm: return empty edges (nodes are mechanical, no loss) ──────────
    if skip_llm:
        if progress is not None and hasattr(progress, "record_llm_exchange"):
            progress.record_llm_exchange(
                phase="pass_c",
                step=_pass_c_progress_trace_step(
                    "skip", batch_index, n_batches, outer_round, max_outer_rounds
                ),
                model=m or "",
                messages=[
                    {"role": "system", "content": pass_c_system()},
                    {"role": "user", "content": user_content},
                ],
                response={"relationships": [], "skip_llm": True},
                from_cache=False,
                meta={
                    "payload_chars": len(payload),
                    "pass_c_batch_index": batch_index,
                    "pass_c_n_batches": n_batches,
                    "pass_c_outer_round": outer_round,
                    "pass_c_max_outer_rounds": max_outer_rounds,
                },
            )
        return {"relationships": [], "self_confidence": 0.75}

    # ── Live LLM call ─────────────────────────────────────────────────────────
    step_c = _pass_c_progress_trace_step(
        "live", batch_index, n_batches, outer_round, max_outer_rounds
    )
    messages = [
        {"role": "system", "content": pass_c_system()},
        {"role": "user", "content": user_content},
    ]
    parsed = complete_json(
        messages,
        json_schema=PASS_C_EDGES_SCHEMA,
        model=m,
        max_retries=4,
        temperature=0.0,
        enable_thinking=False,
        raw_coerce=coerce_pass_c_edges_raw,
        http_timeout=knowledge_ingest_heavy_llm_timeout_sec(),
    )
    rels_out = list(parsed.get("relationships") or [])
    sc_out = float(parsed.get("self_confidence") or 0.7)

    if progress is not None and hasattr(progress, "record_llm_exchange"):
        progress.record_llm_exchange(
            phase="pass_c",
            step=step_c,
            model=m or "",
            messages=[{"role": msg["role"], "content": msg["content"]} for msg in messages],
            response=parsed,
            from_cache=False,
            meta={
                "payload_chars": len(payload),
                "n_entity_rows": n_entity_rows,
                "n_kg_rels": len(rels_out),
                "pass_c_batch_index": batch_index,
                "pass_c_n_batches": n_batches,
                "pass_c_outer_round": outer_round,
                "pass_c_max_outer_rounds": max_outer_rounds,
            },
        )
    save_json(path, {"relationships": rels_out, "self_confidence": sc_out})
    return {"relationships": rels_out, "self_confidence": sc_out}


def run_pass_c_batched(
    clusters: list[dict[str, Any]],
    *,
    batch_size: int = 100,
    max_outer_rounds: int = 1,
    skip_llm: bool = False,
    progress: Any = None,
) -> dict[str, Any]:
    """Run Pass C: generate Entity nodes mechanically, then ask LLM for edges only.

    Architecture:
    • Nodes are generated deterministically from Pass B clusters (``_clusters_to_nodes``).
      This eliminates the 0-nodes failure mode entirely.
    • The LLM's sole task is inferring RELATED_TO / HAS_INSTANCE edges between the
      pre-committed entities.  Payload is a compact {key, name, type} list — smaller
      and less ambiguous than the old full-cluster rows.
    • Outer round 1: JSON array payload per slice (disk-cache friendly).
      Round 2+: wrapped object with ``prior_entity_keys`` + ``prior_relationships``
      so the LLM can add cross-slice edges.
    """
    if not clusters:
        return {"kg_topology": {"nodes": [], "relationships": []}, "self_confidence": 0.75}

    # ── Mechanical node generation (deterministic, zero LLM failure risk) ─────
    mechanical_nodes = _clusters_to_nodes(clusters)
    all_ent_keys = [n["key"] for n in mechanical_nodes]

    slices = pass_c_cluster_slices(clusters, batch_size)
    n_batches = len(slices)
    or_max = max(1, int(max_outer_rounds))

    merged_rels: list[dict[str, Any]] = []
    min_sc = 0.75

    for rnd in range(or_max):
        outer_round = rnd + 1
        parts: list[dict[str, Any]] = []

        for bi, batch in enumerate(slices):
            payload = build_pass_c_payload_for_round(
                batch,
                outer_round=outer_round,
                max_outer_rounds=or_max,
                prior_entity_keys=all_ent_keys if rnd > 0 else None,
                prior_rels=merged_rels if rnd > 0 else None,
            )
            pc = run_pass_c(
                payload,
                skip_llm=skip_llm,
                progress=progress,
                batch_index=bi + 1,
                n_batches=n_batches,
                outer_round=outer_round,
                max_outer_rounds=or_max,
            )
            parts.append(pc)
            if progress is not None and hasattr(progress, "pass_c_batch_done"):
                nr = len(list(pc.get("relationships") or []))
                progress.pass_c_batch_done(
                    bi + 1, n_batches, n_nodes=len(mechanical_nodes), n_rels=nr
                )

        round_rels: list[dict[str, Any]] = []
        for p in parts:
            for r in (p.get("relationships") or []):
                if isinstance(r, dict):
                    round_rels.append(r)

        merged_rels = _pass_c_dedup_rels(merged_rels + round_rels)

        sc_vals = [float(p.get("self_confidence") or 0.7) for p in parts if isinstance(p, dict)]
        if sc_vals:
            min_sc = min(min_sc, min(sc_vals))

    return {
        "kg_topology": {"nodes": mechanical_nodes, "relationships": merged_rels},
        "self_confidence": min_sc,
    }

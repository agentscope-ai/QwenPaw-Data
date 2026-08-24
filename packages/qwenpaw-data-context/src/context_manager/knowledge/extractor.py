"""Pass A LLM extraction with disk cache."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from qwenpaw_data.context.paths import knowledge_ingest_cache_dir

from ..config import CFG, knowledge_ingest_heavy_llm_timeout_sec
from ..openai_client import complete_json, resolve_llm_model
from ..utils import get_logger
from .chunker import Chunk
from .prompts import (
    PASS_A_ENTITY_TYPES,
    PASS_A_EVENT_TYPES,
    PASS_A_JSON_SCHEMA,
    PASS_A_RELATION_SUBTYPES,
    pass_a_system,
    pass_a_user,
)

log = get_logger("knowledge.extractor")

DEFAULT_CACHE_DIR = knowledge_ingest_cache_dir()

_ENTITY_TYPES = frozenset(PASS_A_ENTITY_TYPES)
_ENTITY_TYPE_ALIASES: dict[str, str] = {
    # Soft remaps for the most common synonyms LLMs emit so we lose less data
    # to the "other" bucket. Anything not listed falls through to "other".
    "model": "model_family",
    "model_name": "model_version",
    "ai_model": "model_family",
    "model_series": "model_family",
    "model_endpoint": "model_version",
    "model_snapshot": "model_version",
    "model_category": "concept",
    "model_architecture": "concept",
    "competitor_product": "product",
    "tool": "product",
    "ai_tool": "product",
    "software": "product",
    "software_tool": "product",
    "software_component": "product",
    "system_component": "product",
    "product_module": "feature",
    "product_feature": "feature",
    "application_template": "feature",
    "capability": "feature",
    "framework": "concept",
    "methodology": "concept",
    "methodology_framework": "concept",
    "prompt_framework": "concept",
    "technology_concept": "concept",
    "protocol": "concept",
    "protocol_standard": "concept",
    "competitor": "company",
    "organization": "company",
    "organization_unit": "team",
    "internal_business_unit": "team",
    "business_unit": "team",
    "user_segment": "customer",
    "user_type": "customer",
    "brand": "company",
    "pricing_plan": "subscription_plan",
}

_EVENT_TYPES = frozenset(PASS_A_EVENT_TYPES)
_EVENT_TYPE_ALIASES: dict[str, str] = {
    "metric_change": "business_change",
    "product_launch": "release",
    "launch": "release",
    "policy": "policy_change",
    "upgrade": "system_upgrade",
    "system": "system_upgrade",
    "deprecate": "deprecation",
    "sunset": "deprecation",
}

_RELATION_SUBTYPES = frozenset(PASS_A_RELATION_SUBTYPES)
_RELATION_SUBTYPE_ALIASES: dict[str, str] = {
    "rival": "competitor",
    "rivalry": "competitor",
    "rival_of": "competitor",
    "market_dominance": "competitor",
    "supersedes": "see_also",
    "successor": "see_also",
    "predecessor": "see_also",
    "variant_of": "see_also",
    "version_of": "see_also",
    "related": "see_also",
    "similar": "synonym",
    "same_as": "synonym",
    "alias": "synonym",
    "opposite": "antonym",
    "co_occurs": "correlates",
    "correlation": "correlates",
    "complementary": "complement",
    "complements": "complement",
}


def _s(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n]


def _normalize_entity_type(raw: str) -> str:
    t = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if not t:
        return "other"
    if t in _ENTITY_TYPES:
        return t
    return _ENTITY_TYPE_ALIASES.get(t, "other")


def _normalize_relation_subtype(raw: str) -> str:
    t = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if not t:
        return "see_also"
    if t in _RELATION_SUBTYPES:
        return t
    return _RELATION_SUBTYPE_ALIASES.get(t, "see_also")


def coerce_pass_a_raw(parsed: Any) -> Any:
    """Normalize Pass A LLM output: enforce enums, drop unknown fields, discard events
    whose ``about_surface`` is not in the entity list."""
    if not isinstance(parsed, dict):
        return parsed
    out: dict[str, Any] = dict(parsed)

    for k in ("entities", "events", "entity_relations"):
        v = out.get(k)
        if v is None or not isinstance(v, list):
            out[k] = []

    sc = out.get("self_confidence")
    try:
        out["self_confidence"] = float(sc) if sc is not None else 0.5
    except (TypeError, ValueError):
        out["self_confidence"] = 0.5

    # ---- entities --------------------------------------------------
    ent_out: list[dict[str, Any]] = []
    surfaces_seen: set[str] = set()
    for e in out["entities"]:
        if not isinstance(e, dict):
            continue
        surface = _s(e.get("surface") or e.get("name") or e.get("title") or e.get("entity"))
        if not surface or surface in surfaces_seen:
            continue
        surfaces_seen.add(surface)
        et = _normalize_entity_type(_s(e.get("type")))
        aliases_raw = e.get("aliases")
        if not isinstance(aliases_raw, list):
            aliases_raw = []
        aliases = []
        for a in aliases_raw:
            t = _s(a)
            if t and t != surface and t not in aliases:
                aliases.append(t)
        ev = _s(
            e.get("evidence_quote") or e.get("description") or e.get("summary") or e.get("evidence")
        )
        row: dict[str, Any] = {
            "surface": surface,
            "type": et,
            "aliases": aliases,
            "evidence_quote": _clip(ev, 200),
        }
        definition = _s(e.get("definition") or e.get("short_description"))
        if definition:
            row["definition"] = _clip(definition, 400)
        ent_out.append(row)
    out["entities"] = ent_out

    valid_surfaces = {row["surface"] for row in ent_out}

    # ---- events (discard when about_surface is not an extracted entity) ---
    evt_out: list[dict[str, Any]] = []
    for ev in out["events"]:
        if not isinstance(ev, dict):
            continue
        name = _s(ev.get("name") or ev.get("subject") or ev.get("title"))
        if not name:
            continue
        raw_t = (
            _s(ev.get("type") or ev.get("event_type")).lower().replace(" ", "_").replace("-", "_")
        )
        if raw_t in _EVENT_TYPES:
            etype = raw_t
        else:
            etype = _EVENT_TYPE_ALIASES.get(raw_t, "other")
        about = _s(ev.get("about_surface") or ev.get("about") or ev.get("entity"))
        if about not in valid_surfaces:
            continue
        date_from = _s(ev.get("date_from") or ev.get("date") or ev.get("month"))
        description = _s(
            ev.get("description")
            or ev.get("change_description")
            or ev.get("summary")
            or ev.get("detail")
        )
        evt_out.append(
            {
                "name": name,
                "type": etype,
                "date_from": date_from,
                "date_to": _s(ev.get("date_to")),
                "description": _clip(description, 800),
                "about_surface": about,
            }
        )
    out["events"] = evt_out

    # ---- entity_relations ------------------------------------------
    rel_out: list[dict[str, Any]] = []
    for r in out["entity_relations"]:
        if not isinstance(r, dict):
            continue
        fs = _s(r.get("from_surface") or r.get("head") or r.get("source") or r.get("from"))
        ts = _s(r.get("to_surface") or r.get("tail") or r.get("target") or r.get("to"))
        if not fs or not ts or fs == ts:
            continue
        if fs not in valid_surfaces or ts not in valid_surfaces:
            continue
        st = _normalize_relation_subtype(
            _s(r.get("relation_subtype") or r.get("relation_type") or r.get("type"))
        )
        desc = _s(r.get("description") or r.get("evidence_quote") or r.get("rationale") or r.get("note"))
        row = {
            "from_surface": fs,
            "to_surface": ts,
            "relation_subtype": st,
            "description": _clip(desc, 500),
        }
        scope = _s(r.get("scope"))
        if scope:
            row["scope"] = _clip(scope, 200)
        rel_out.append(row)
    out["entity_relations"] = rel_out

    # Drop legacy fields if old caches/responses still carry them
    for legacy in ("surface_metric_hints", "dim_values", "calibers", "formulas"):
        out.pop(legacy, None)

    return out


_PASS_A_PROMPT_VERSION = hashlib.sha1(pass_a_system().encode("utf-8")).hexdigest()[:12]


def _cache_key(chunk: Chunk, model: str, *, enable_thinking: bool) -> str:
    payload = (
        f"{model}\n{int(bool(enable_thinking))}\n{_PASS_A_PROMPT_VERSION}\n"
        f"{chunk.source_doc}\n{chunk.chunk_idx}\n{chunk.text}"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def load_cached(cache_dir: Path, key: str) -> Optional[dict[str, Any]]:
    path = cache_dir / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_cached(cache_dir: Path, key: str, data: dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_chunk(
    chunk: Chunk,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    model: Optional[str] = None,
    max_retries: int = 2,
    skip_llm: bool = False,
    progress: Any = None,
    enable_thinking: bool = False,
) -> dict[str, Any]:
    """Run Pass A on one chunk; returns parsed JSON (+ meta)."""
    m = resolve_llm_model(model)
    ck = _cache_key(chunk, m or "", enable_thinking=enable_thinking)
    cached = load_cached(cache_dir, ck)
    if cached is not None and "parsed" in cached:
        out = dict(cached["parsed"])
        if isinstance(out, dict):
            out["_ingest_from_cache"] = True
        if progress is not None and hasattr(progress, "record_llm_exchange"):
            progress.record_llm_exchange(
                phase="pass_a",
                step=f"chunk #{chunk.chunk_idx + 1} · {chunk.source_doc}（磁盘缓存）",
                model=m or "",
                messages=[],
                response=out,
                from_cache=True,
                meta={
                    "chunk_idx": chunk.chunk_idx,
                    "source_doc": chunk.source_doc,
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                },
            )
        return out

    if skip_llm:
        empty = {
            "entities": [],
            "events": [],
            "entity_relations": [],
            "self_confidence": 0.0,
            "_ingest_skip_llm": True,
        }
        if progress is not None and hasattr(progress, "record_llm_exchange"):
            messages = [
                {"role": "system", "content": pass_a_system()},
                {"role": "user", "content": pass_a_user(chunk.text, chunk.source_doc)},
            ]
            progress.record_llm_exchange(
                phase="pass_a",
                step=f"chunk #{chunk.chunk_idx + 1} · {chunk.source_doc}（skip_llm）",
                model=m or "",
                messages=messages,
                response=empty,
                from_cache=False,
                meta={
                    "chunk_idx": chunk.chunk_idx,
                    "source_doc": chunk.source_doc,
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                    "note": "未调用 LLM",
                },
            )
        return empty

    messages = [
        {"role": "system", "content": pass_a_system()},
        {"role": "user", "content": pass_a_user(chunk.text, chunk.source_doc)},
    ]
    meta: dict[str, Any] = {}
    parsed = complete_json(
        messages,
        json_schema=PASS_A_JSON_SCHEMA,
        model=m,
        max_retries=max_retries,
        temperature=0.0,
        metadata_out=meta,
        enable_thinking=enable_thinking,
        raw_coerce=coerce_pass_a_raw,
        http_timeout=knowledge_ingest_heavy_llm_timeout_sec(),
    )
    if isinstance(parsed, dict):
        parsed = dict(parsed)
        parsed["_ingest_from_cache"] = False

    if progress is not None and hasattr(progress, "record_llm_exchange"):
        progress.record_llm_exchange(
            phase="pass_a",
            step=f"chunk #{chunk.chunk_idx + 1} · {chunk.source_doc}",
            model=m or "",
            messages=[{"role": m["role"], "content": m["content"]} for m in messages],
            response=parsed,
            from_cache=False,
            meta={
                "chunk_idx": chunk.chunk_idx,
                "source_doc": chunk.source_doc,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "llm_meta": {k: v for k, v in meta.items() if k != "raw_content"},
            },
        )

    save_cached(
        cache_dir,
        ck,
        {
            "chunk": {
                "source_doc": chunk.source_doc,
                "chunk_idx": chunk.chunk_idx,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
            },
            "parsed": parsed,
            "llm_meta": {k: v for k, v in meta.items() if k != "raw_content"},
        },
    )
    return parsed

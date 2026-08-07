"""Topology: LLM extracts entities + catalog-oriented alias phrases for anchor recall only.

This step is **separate** from :mod:`query_rewrite` (multi-turn consolidation) and from
:mod:`semantic_split` (facet decomposition). Do not share system prompts across those modules."""
from __future__ import annotations

from typing import Any, Optional

from ..config import CFG
from ..openai_client import complete_json
from ..utils import get_logger

log = get_logger("runtime.retrieval_entity_expand")

_MAX_RECALL_PHRASE_LEN = 256
_MAX_RECALL_QUERIES = 14

ENTITY_EXPAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "description": (
                "Business or analytics mentions worth matching against a warehouse / metric catalog."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Surface form as in the input text",
                    },
                    "aliases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Other spellings: abbreviations, English tokens, internal codes, "
                            "legacy nicknames"
                        ),
                    },
                },
                "required": ["name"],
            },
        },
        "entity_recall_queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "0–12 compact lines for graph fulltext/vector retrieval only. "
                "Each line mixes one anchor concept with a few likely catalog synonyms; "
                "avoid pasting the full user paragraph."
            ),
        },
    },
    "required": ["entities", "entity_recall_queries"],
}

# Intentionally distinct from query_rewrite / semantic_split prompts (no shared copy).
ENTITY_EXPAND_SYSTEM = """You are the retrieval-entity specialist for an analytics knowledge graph.

Task: read ONE primary natural-language block (already the analyst's question, or a merged
follow-up summary). Extract entities that may appear under different names in databases
(metrics, dimensions, products, platforms, org names, funnel steps).

Output JSON only:
- ``entities``: list of objects with ``name`` (string) and ``aliases`` (string array). ``name``
  is what the text actually says; ``aliases`` are plausible catalog spellings (English,
  abbreviations, codes).
- ``entity_recall_queries``: short retrieval lines (not full sentences) built from those
  surfaces — each line should help a keyword or embedding index hit the right node when the
  analyst's wording differs from the physical schema.

Rules:
- Do not invent metrics or filters that are not reasonably implied by the primary text
  (and optional disambiguation block).
- Keep each ``entity_recall_queries`` entry under ~120 characters unless packing several
  short synonyms.
- Match the analyst's language where possible; keep English metric/column tokens when given.
- If nothing useful beyond literal words in the primary text, return empty arrays.
- If a domain scope is provided, use it to understand context (e.g. which product's
  metrics are being asked about), but do NOT include the domain name in
  entity_recall_queries. Domain scoping is handled as a separate post-retrieval
  filter, not as a search token.
"""


def _normalize_entities(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        aliases: list[str] = []
        for a in item.get("aliases") or []:
            s = str(a).strip()
            if s and s not in aliases:
                aliases.append(s)
        out.append({"name": name, "aliases": aliases[:12]})
    return out[:16]


def _normalize_entity_recall_queries(parsed: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    raw_rq = parsed.get("entity_recall_queries") or []
    out: list[str] = []
    if isinstance(raw_rq, list):
        for x in raw_rq:
            s = str(x).strip()
            if len(s) < 2:
                continue
            out.append(s[:_MAX_RECALL_PHRASE_LEN])
    if out:
        return out[:_MAX_RECALL_QUERIES], _normalize_entities(parsed.get("entities"))

    built: list[str] = []
    ents = _normalize_entities(parsed.get("entities"))
    for ent in ents:
        name = (ent.get("name") or "").strip()
        als = [str(a).strip() for a in (ent.get("aliases") or []) if str(a).strip()]
        if name and als:
            frag = ", ".join(als[:5])
            line = f"{name} {frag}"[:_MAX_RECALL_PHRASE_LEN]
            if len(line) >= 2:
                built.append(line)
        elif name and len(name) >= 2:
            built.append(name[:_MAX_RECALL_PHRASE_LEN])
    return built[:_MAX_RECALL_QUERIES], ents


def expand_entities_for_anchor_recall(
    *,
    primary_nl: str,
    disambiguation_block: str = "",
    model: str,
    reasoning_capture: Optional[list[str]] = None,
    metadata_out: Optional[dict[str, Any]] = None,
    enable_thinking: Optional[bool] = None,
    domain: str = "",
) -> tuple[list[str], dict[str, Any]]:
    """Return ``(recall_queries, meta)`` for extra anchor recall lines.

    ``primary_nl`` — first turn: ``Example.question``; after multi-turn rewrite: the
    consolidated ``effective_retrieval_q``. Optional ``disambiguation_block`` is read-only
    context (e.g. original pipeline question when primary is the rewritten summary).
    """
    meta: dict[str, Any] = {"fallback": False, "skipped": False}
    base = (primary_nl or "").strip()
    if not base:
        meta["skipped"] = True
        meta["reason"] = "empty_primary_nl"
        return [], meta

    user_parts = [f"Primary text for entity / alias extraction:\n{base}"]
    if domain:
        user_parts.insert(0, f"Domain scope: {domain}")
    db = (disambiguation_block or "").strip()
    if db:
        user_parts.append(
            "Disambiguation (do not treat as new requirements; use only if the primary text is ambiguous):\n"
            + db[:2400]
        )
    messages = [
        {"role": "system", "content": ENTITY_EXPAND_SYSTEM},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    try:
        parsed = complete_json(
            messages,
            json_schema=ENTITY_EXPAND_SCHEMA,
            model=model,
            max_retries=CFG.agent_retrieval_entity_expand_max_retries,
            temperature=CFG.agent_retrieval_entity_expand_temperature,
            reasoning_capture=reasoning_capture,
            metadata_out=metadata_out,
            enable_thinking=enable_thinking,
        )
        recall_queries, entities_norm = _normalize_entity_recall_queries(parsed)
        meta["llm_called"] = True
        meta["recall_queries"] = recall_queries
        meta["entities"] = entities_norm
        meta["n_recall_queries"] = len(recall_queries)
        if metadata_out:
            meta["llm"] = dict(metadata_out)
        return recall_queries, meta
    except Exception as exc:
        log.warning("retrieval_entity_expand failed: %s", exc)
        meta["fallback"] = True
        meta["error"] = str(exc)
        return [], meta

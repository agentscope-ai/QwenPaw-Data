"""L2 explore_entity multi-hop synthesis.

Pipeline (called from cm_api._explore_entity_impl):
  1. zoom_entity → 1-hop neighbors (already exists; reused upstream)
  2. pick_2hop_targets_llm → choose <=K 1-hop nodes worth expanding
  3. expand_2hop → fetch 2-hop neighbors via Neo4j
  4. synthesize_entity_context_llm → produce 4 NL fields per entity type

Prompts are written in English. The LLM emits JSON whose VALUES are Chinese
(downstream consumers are Chinese BI users / metrics_dict.yaml).
"""
from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from neo4j import Driver

from ..utils import neo4j_session  # re-exported so tests can monkeypatch
from ..openai_client import complete_json  # re-exported so tests can monkeypatch

log = logging.getLogger("context_manager.runtime.explore_synthesis")


# ---------------------------------------------------------------------- #
# Output JSON schemas (used with openai_client.complete_json validation)
# ---------------------------------------------------------------------- #

SYNTHESIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "usage_guidance": {"type": "string"},
        "related_metrics_nl": {
            "type": "array",
            "items": {"type": "string"},
        },
        "experience_hints": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "summary", "usage_guidance", "related_metrics_nl", "experience_hints"
    ],
}

PICKER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["node_id"],
            },
        },
    },
    "required": ["picks"],
}


# ---------------------------------------------------------------------- #
# Prompts (all English; LLM still emits Chinese JSON values)
# ---------------------------------------------------------------------- #

_COMMON_SYSTEM_PREAMBLE = """You are a data semantics assistant for a Chinese BI agent.
Given an entity card and its graph neighbors, describe this entity's role in
the user's current query.

Strict output rules:
- Output ONE JSON object only. No markdown, no explanation, no leading text.
- Write the JSON values in Chinese (downstream consumers are Chinese BI users).
- The JSON KEYS must remain English: summary, usage_guidance,
  related_metrics_nl, experience_hints.
- If a slot lacks information, return an empty string or empty array. Never
  invent table names, column names, SQL, or metric formulas.
- Length budgets: summary <= 80 Chinese chars; usage_guidance <= 120 chars;
  each related_metrics_nl item <= 30 chars; each experience_hints item <= 40
  chars. Hard-cap arrays at 5 items.
"""

_COMMON_USER_TEMPLATE_FOOTER = """
Original user query (may be empty if not provided): {original_query}
Entity card:
{entity_card}

1-hop neighbors:
{hop1_neighbors}

Selected 2-hop neighbors:
{hop2_picked}

If "Original user query" is empty, write summary/usage_guidance describing the
entity in general (do not hallucinate a query context).

Emit the JSON object now.
"""


_METRIC_INSTRUCTIONS = """
Entity type: Metric.

Special focus:
- summary should name the metric domain and metric role (e.g. north-star /
  process / auxiliary) and explain how it would serve the user query.
- usage_guidance MUST cover at least one of: aggregation method (inferred from
  formula), required partition filter (from common_filters), recommended drill
  dimensions (from drill_dimensions). Do NOT recommend SUM if the formula is a
  bare daily count column - call that out explicitly.
- related_metrics_nl should describe the relation type, e.g. "同 Dataset 兄弟",
  "分子/分母", "上卷指标", read from the 2-hop edges if visible.
- experience_hints distill card lessons into action-oriented warnings.

Special slots: formula_text={formula_text}; partition_predicate={partition_predicate}; topline_value={topline_value}.
"""

_DIMENSION_INSTRUCTIONS = """
Entity type: Dimension.

Special focus:
- summary should classify the dimension (time / geo / user-tier / product-tier)
  and mention cardinality if available.
- usage_guidance MUST cover at least one of: which physical column to use
  (domain.table.col), whether a JOIN is required, typical filter values.
- related_metrics_nl should list metrics that are commonly sliced by this
  dimension.
- experience_hints focus on dirty-value or semantic-pitfall warnings (e.g.
  "X has both empty string and NULL values").

Special slots: cardinality={cardinality}; sample_values={sample_values}; column_binding={column_binding}.
"""

_DATASET_INSTRUCTIONS = """
Entity type: Dataset (table).

Special focus:
- summary should describe what business entity this fact / dimension table
  represents and at what grain.
- usage_guidance MUST cover at least one of: primary key, partition column,
  typical join paths, refresh cadence.
- related_metrics_nl should list the core metrics anchored on this table.
- experience_hints focus on table-level data-quality / partition warnings.

Special slots: primary_key={primary_key}; partition_columns={partition_columns}; table_grain={table_grain}.
"""

_COLUMN_INSTRUCTIONS = """
Entity type: Column.

Special focus:
- summary should state which table this column belongs to, its data type, and
  business meaning in one sentence.
- usage_guidance MUST cover at least one of: whether the column participates
  in a metric formula, typical filter values, whether a CAST is required.
- related_metrics_nl describes which metrics consume this column (from
  PARTICIPATES_IN edges).
- experience_hints: null ratio, special-value semantics.

Special slots: data_type={data_type}; participates_in_formulas={participates_in_formulas}; distinct_count={distinct_count}.
"""


def _build_user_template(extra_instructions: str) -> str:
    return extra_instructions.rstrip() + "\n" + _COMMON_USER_TEMPLATE_FOOTER


PROMPTS: dict[str, dict[str, str]] = {
    "Metric": {
        "system": _COMMON_SYSTEM_PREAMBLE,
        "user_template": _build_user_template(_METRIC_INSTRUCTIONS),
    },
    "Dimension": {
        "system": _COMMON_SYSTEM_PREAMBLE,
        "user_template": _build_user_template(_DIMENSION_INSTRUCTIONS),
    },
    "Dataset": {
        "system": _COMMON_SYSTEM_PREAMBLE,
        "user_template": _build_user_template(_DATASET_INSTRUCTIONS),
    },
    "Column": {
        "system": _COMMON_SYSTEM_PREAMBLE,
        "user_template": _build_user_template(_COLUMN_INSTRUCTIONS),
    },
}


PICKER_PROMPT: dict[str, str] = {
    "system": """You are a graph traversal planner for a BI agent.
Given a list of 1-hop neighbor candidates of an entity and the user's
original query, select at most K candidates whose 2-hop expansion would
best help describe how the entity serves the query.

Strict output rules:
- Output ONE JSON object: {"picks": [{"node_id": "...", "reason": "..."}]}.
- node_id must come from the candidate list verbatim. Do NOT invent ids.
- Prefer candidates semantically closest to the user query.
- Prefer RelatedMetric and Card nodes (high information density).
- Avoid duplicate information sources (do not pick two siblings of the same
  parent if one suffices).
- Output at most K items. Empty array is acceptable if no candidate is useful.
""",
    "user_template": """Original user query (may be empty): {original_query}
Entity: {entity_name} ({entity_type})
K (max picks): {k}

1-hop candidates (node_id | label | brief):
{candidates}

Emit the JSON object now.
""",
}


def _resolve_prompt(entity_type: Optional[str]) -> dict[str, str]:
    """Return the prompt dict for this entity type; fall back to Metric."""
    if not entity_type:
        return PROMPTS["Metric"]
    return PROMPTS.get(entity_type, PROMPTS["Metric"])


# ---------------------------------------------------------------------- #
# Config loader
# ---------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "fields": {
        "summary": True,
        "usage_guidance": True,
        "related_metrics_nl": True,
        "experience_hints": True,
    },
    "hop2": {
        "enabled": True,
        "max_targets": 4,
        "candidate_sources": ["related_metrics", "source_columns", "related_cards"],
    },
    "llm": {
        "model": None,
        "synthesis_max_tokens": 1200,
        "picker_max_tokens": 200,
        "temperature": 0.2,
        "picker_timeout_sec": 8.0,
        "synthesis_timeout_sec": 15.0,
    },
    "context": {
        "include_original_query": True,
        "max_neighbors_per_node": 3,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge override onto a deep copy of base.

    Both base and override values are deep-copied into the result so the
    returned dict shares no mutable references with either input. This
    prevents callers from accidentally mutating module-level defaults
    (e.g. DEFAULT_CONFIG["hop2"]["candidate_sources"]).
    """
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_explore_synthesis_config(path: Optional[Path] = None) -> dict[str, Any]:
    """Read explore_synthesis section from agent_explorer.json with defaults.

    Missing file or missing section → returns DEFAULT_CONFIG copy.
    Partial section → deep-merged onto defaults.
    """
    if path is None:
        # explore_synthesis.py lives at semantic-layer/context_manager/runtime/
        # → parents[3] is the repo root
        repo_root = Path(__file__).resolve().parents[3]
        path = repo_root / "config" / "agent_explorer.json"
    try:
        with path.open() as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _deep_merge(DEFAULT_CONFIG, {})
    section = raw.get("explore_synthesis") or {}
    return _deep_merge(DEFAULT_CONFIG, section)


# ---------------------------------------------------------------------- #
# Placeholder fallback (mirrors original _explore_entity_impl behavior)
# ---------------------------------------------------------------------- #

def _placeholder_fields(detail: Any) -> dict[str, Any]:
    """Compute the four NL fields the way the current placeholder does.

    Used when LLM synthesis is disabled or fails. Mirrors the production
    logic in ``cm_api._explore_entity_impl`` EXACTLY:

      summary             ← detail.definition or f"{detail.name} 实体上下文"
      usage_guidance      ← "; ".join(detail.common_filters[:2]) if any else ""
      related_metrics_nl  ← [f"关联指标 {m.name}" for m in detail.related_metrics[:5]]
      experience_hints    ← [str(c.lesson) for c in detail.related_cards if lesson][:5]
      knowledge_notes     ← [k.summary for k in detail.related_knowledge if summary][:5]
    """
    name = getattr(detail, "name", "") or ""
    definition = getattr(detail, "definition", "") or ""
    common_filters = list(getattr(detail, "common_filters", []) or [])
    related_metrics = list(getattr(detail, "related_metrics", []) or [])
    related_cards = list(getattr(detail, "related_cards", []) or [])
    related_knowledge = list(getattr(detail, "related_knowledge", []) or [])

    summary = definition or f"{name} 实体上下文"
    usage_guidance = "; ".join(common_filters[:2]) if common_filters else ""
    related_metrics_nl = [
        f"关联指标 {m.name}" for m in related_metrics[:5]
    ]
    experience_hints = [
        str(c.lesson) for c in related_cards if getattr(c, "lesson", "")
    ][:5]
    knowledge_notes = [
        k.summary for k in related_knowledge if getattr(k, "summary", "")
    ][:5]

    return {
        "summary": summary,
        "usage_guidance": usage_guidance,
        "related_metrics_nl": related_metrics_nl,
        "experience_hints": experience_hints,
        "knowledge_notes": knowledge_notes,
    }


# ---------------------------------------------------------------------- #
# Graph helpers
# ---------------------------------------------------------------------- #

_EXPAND_2HOP_CYPHER = """
MATCH (n {key: $key})-[r]-(neighbor)
WHERE neighbor.key IS NOT NULL
RETURN neighbor.key   AS key,
       coalesce(labels(neighbor)[0], '') AS label,
       coalesce(neighbor.name, '')       AS name,
       type(r)         AS rel_type
LIMIT $cap
"""


def expand_2hop(
    driver: Any,
    picked_keys: list[str],
    *,
    max_neighbors_per_node: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch <=max_neighbors neighbors for each picked 1-hop node key.

    Returns {key: [{key, label, name, rel_type}, ...], ...}. On Neo4j error
    for a particular key, returns [] for that key (does not raise).
    """
    out: dict[str, list[dict[str, Any]]] = {}
    if not picked_keys:
        return out
    cap = max(1, int(max_neighbors_per_node))
    # Cypher LIMIT cannot bind to a parameter in older Neo4j; substitute it
    # via string replacement (cap is server-side integer, not user input).
    cypher = _EXPAND_2HOP_CYPHER.replace("$cap", str(cap))
    for key in picked_keys:
        try:
            with neo4j_session(driver) as s:
                rows = s.run(cypher, key=key).data()
            out[key] = [
                {
                    "key": str(r.get("key") or ""),
                    "label": str(r.get("label") or ""),
                    "name": str(r.get("name") or ""),
                    "rel_type": str(r.get("rel_type") or ""),
                }
                for r in rows
                if r.get("key")
            ][:cap]
        except Exception as exc:  # noqa: BLE001 - graph errors must not crash explore_entity
            log.warning("expand_2hop failed for key=%s: %s", key, exc)
            out[key] = []
    return out


# ---------------------------------------------------------------------- #
# 2-hop picker
# ---------------------------------------------------------------------- #

def _gather_1hop_candidates(detail: Any, sources: list[str]) -> list[dict[str, str]]:
    """Flatten the 1-hop subset that the picker may choose from.

    Returns [{node_id, label, brief}, ...] preserving the order in
    candidate_sources from config.
    """
    out: list[dict[str, str]] = []

    def _push(node_id: str, label: str, brief: str) -> None:
        node_id = (node_id or "").strip()
        if not node_id:
            return
        if any(c["node_id"] == node_id for c in out):
            return
        out.append({"node_id": node_id, "label": label, "brief": brief[:120]})

    for src in sources:
        if src == "related_metrics":
            for m in (getattr(detail, "related_metrics", []) or []):
                key = getattr(m, "key", "") or f"metric:{getattr(m, 'name', '')}"
                _push(
                    key,
                    "Metric",
                    f"name={getattr(m, 'name', '')}; def={getattr(m, 'definition', '') or ''}",
                )
        elif src == "source_columns":
            for c in (getattr(detail, "source_columns", []) or []):
                key = getattr(c, "key", "") or (
                    f"col:{getattr(c, 'table', '')}.{getattr(c, 'name', '')}"
                )
                _push(
                    key,
                    "Column",
                    f"table={getattr(c, 'table', '')}; col={getattr(c, 'name', '')}; role={getattr(c, 'role', '')}",
                )
        elif src == "related_cards":
            for k in (getattr(detail, "related_cards", []) or []):
                key = getattr(k, "key", "") or f"card:{id(k)}"
                _push(key, "Card", f"lesson={getattr(k, 'lesson', '') or ''}")
    return out


def pick_2hop_targets_llm(
    detail: Any,
    *,
    original_query: str,
    cfg: dict[str, Any],
) -> list[str]:
    """Ask the LLM which 1-hop nodes to expand to 2-hop. Returns node_id list.

    Caps at cfg['hop2']['max_targets']; drops unknown ids; deduplicates.
    Returns [] on LLM error or when no candidates.
    """
    sources = cfg.get("hop2", {}).get("candidate_sources") or []
    k = int(cfg.get("hop2", {}).get("max_targets") or 4)
    candidates = _gather_1hop_candidates(detail, sources)
    if not candidates:
        return []

    valid_ids = {c["node_id"] for c in candidates}
    cand_lines = "\n".join(
        f"  {c['node_id']} | {c['label']} | {c['brief']}" for c in candidates
    )
    user_msg = PICKER_PROMPT["user_template"].format(
        original_query=original_query or "(none)",
        entity_name=getattr(detail, "name", "") or "",
        entity_type=getattr(detail, "label", "") or "Metric",
        k=k,
        candidates=cand_lines,
    )
    messages = [
        {"role": "system", "content": PICKER_PROMPT["system"]},
        {"role": "user", "content": user_msg},
    ]
    llm_cfg = cfg.get("llm", {})
    try:
        parsed = complete_json(
            messages,
            json_schema=PICKER_JSON_SCHEMA,
            model=llm_cfg.get("model"),
            max_retries=1,
            temperature=float(llm_cfg.get("temperature", 0.2)),
            http_timeout=float(llm_cfg.get("picker_timeout_sec", 8.0)),
            enable_thinking=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("pick_2hop_targets_llm failed: %s", exc)
        return []

    raw_picks = parsed.get("picks") or []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw_picks:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        if not node_id or node_id in seen or node_id not in valid_ids:
            continue
        seen.add(node_id)
        out.append(node_id)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------- #
# Synthesizer
# ---------------------------------------------------------------------- #

def _format_entity_card(detail: Any) -> str:
    name = getattr(detail, "name", "") or ""
    label = getattr(detail, "label", "") or ""
    definition = getattr(detail, "definition", "") or ""
    formula = getattr(detail, "formula_semantic", "") or ""
    return (
        f"name={name}\n"
        f"label={label}\n"
        f"definition={definition}\n"
        f"formula={formula}"
    )


def _format_hop1_neighbors(detail: Any) -> str:
    lines: list[str] = []
    for c in (getattr(detail, "source_columns", []) or [])[:6]:
        lines.append(
            f"- Column {getattr(c, 'name', '')} (table={getattr(c, 'table', '')}, role={getattr(c, 'role', '')})"
        )
    for d in (getattr(detail, "drill_dimensions", []) or [])[:6]:
        lines.append(f"- Dimension {getattr(d, 'name', '')}")
    for f in (getattr(detail, "common_filters", []) or [])[:4]:
        lines.append(f"- Filter {f}")
    for m in (getattr(detail, "related_metrics", []) or [])[:6]:
        lines.append(
            f"- RelatedMetric {getattr(m, 'name', '')}: {getattr(m, 'definition', '') or ''}"
        )
    for k in (getattr(detail, "related_cards", []) or [])[:4]:
        lines.append(f"- Card lesson: {getattr(k, 'lesson', '') or ''}")
    return "\n".join(lines) if lines else "(none)"


def _format_hop2(hop2: dict[str, list[dict[str, Any]]]) -> str:
    if not hop2:
        return "(none)"
    blocks: list[str] = []
    for parent_key, neighbors in hop2.items():
        if not neighbors:
            continue
        block = [f"From {parent_key}:"]
        for n in neighbors:
            block.append(
                f"  - {n.get('label', '')} {n.get('name', '')} ({n.get('rel_type', '')})"
            )
        blocks.append("\n".join(block))
    return "\n".join(blocks) if blocks else "(none)"


def _slot_value(detail: Any, attr: str, default: str = "") -> str:
    v = getattr(detail, attr, None)
    if v is None:
        return default
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v[:5])
    return str(v)


def synthesize_entity_context_llm(
    *,
    entity_type: str,
    detail: Any,
    hop2: dict[str, list[dict[str, Any]]],
    original_query: str,
    cfg: dict[str, Any],
    cache_key: Optional[str] = None,  # reserved for future caching
) -> dict[str, Any]:
    """Run the synthesis LLM call and return four NL fields.

    On any LLM / parsing failure, returns ``_placeholder_fields(detail)``.
    Always truncates ``related_metrics_nl`` and ``experience_hints`` to 5.
    """
    prompt = _resolve_prompt(entity_type)
    user_msg = prompt["user_template"].format(
        original_query=original_query or "",
        entity_card=_format_entity_card(detail),
        hop1_neighbors=_format_hop1_neighbors(detail),
        hop2_picked=_format_hop2(hop2),
        # Metric slots
        formula_text=_slot_value(detail, "formula_semantic"),
        partition_predicate=_slot_value(detail, "partition_predicate"),
        topline_value=_slot_value(detail, "topline_value"),
        # Dimension slots
        cardinality=_slot_value(detail, "cardinality"),
        sample_values=_slot_value(detail, "sample_values"),
        column_binding=_slot_value(detail, "column_binding"),
        # Dataset slots
        primary_key=_slot_value(detail, "primary_key"),
        partition_columns=_slot_value(detail, "partition_columns"),
        table_grain=_slot_value(detail, "table_grain"),
        # Column slots
        data_type=_slot_value(detail, "data_type"),
        participates_in_formulas=_slot_value(detail, "participates_in_formulas"),
        distinct_count=_slot_value(detail, "distinct_count"),
    )
    messages = [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": user_msg},
    ]
    llm_cfg = cfg.get("llm", {})
    try:
        parsed = complete_json(
            messages,
            json_schema=SYNTHESIS_JSON_SCHEMA,
            model=llm_cfg.get("model"),
            max_retries=1,
            temperature=float(llm_cfg.get("temperature", 0.2)),
            http_timeout=float(llm_cfg.get("synthesis_timeout_sec", 15.0)),
            enable_thinking=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("synthesize_entity_context_llm failed: %s", exc)
        return _placeholder_fields(detail)

    summary = str(parsed.get("summary") or "").strip()
    usage_guidance = str(parsed.get("usage_guidance") or "").strip()
    related = [str(x).strip() for x in (parsed.get("related_metrics_nl") or []) if str(x).strip()][:5]
    hints = [str(x).strip() for x in (parsed.get("experience_hints") or []) if str(x).strip()][:5]

    pf = _placeholder_fields(detail)
    return {
        "summary": summary or pf["summary"],
        "usage_guidance": usage_guidance or pf["usage_guidance"],
        "related_metrics_nl": related,
        "experience_hints": hints,
        "knowledge_notes": pf["knowledge_notes"],  # always from placeholder
    }


def apply_field_toggles(
    llm_fields: dict[str, Any],
    detail: Any,
    cfg_fields: dict[str, bool],
) -> dict[str, Any]:
    """Replace any LLM field whose toggle is False with placeholder output."""
    pf = _placeholder_fields(detail)
    out = dict(llm_fields)
    for field in ("summary", "usage_guidance", "related_metrics_nl", "experience_hints"):
        if not cfg_fields.get(field, True):
            out[field] = pf[field]
    return out


def synthesize_from_subgraph(
    *,
    detail: Any,
    subgraph_text: str,
    original_query: str,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Synthesize the 4 NL fields using the session multi-hop subgraph text as
    the hop2 substrate instead of entity-local expand_2hop. Falls back to
    _placeholder_fields on disable / any failure.
    """
    if not cfg.get("enabled", True):
        return _placeholder_fields(detail)
    prompt = _resolve_prompt(getattr(detail, "label", "") or "Metric")
    user_msg = prompt["user_template"].format(
        original_query=original_query or "",
        entity_card=_format_entity_card(detail),
        hop1_neighbors=_format_hop1_neighbors(detail),
        hop2_picked=subgraph_text or "(none)",
        formula_text=_slot_value(detail, "formula_semantic"),
        partition_predicate=_slot_value(detail, "partition_predicate"),
        topline_value=_slot_value(detail, "topline_value"),
        cardinality=_slot_value(detail, "cardinality"),
        sample_values=_slot_value(detail, "sample_values"),
        column_binding=_slot_value(detail, "column_binding"),
        primary_key=_slot_value(detail, "primary_key"),
        partition_columns=_slot_value(detail, "partition_columns"),
        table_grain=_slot_value(detail, "table_grain"),
        data_type=_slot_value(detail, "data_type"),
        participates_in_formulas=_slot_value(detail, "participates_in_formulas"),
        distinct_count=_slot_value(detail, "distinct_count"),
    )
    messages = [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": user_msg},
    ]
    llm_cfg = cfg.get("llm", {})
    try:
        parsed = complete_json(
            messages, json_schema=SYNTHESIS_JSON_SCHEMA, model=llm_cfg.get("model"),
            max_retries=1, temperature=float(llm_cfg.get("temperature", 0.2)),
            http_timeout=float(llm_cfg.get("synthesis_timeout_sec", 15.0)),
            enable_thinking=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("synthesize_from_subgraph failed: %s", exc)
        return _placeholder_fields(detail)
    pf = _placeholder_fields(detail)
    return {
        "summary": str(parsed.get("summary") or "").strip() or pf["summary"],
        "usage_guidance": str(parsed.get("usage_guidance") or "").strip() or pf["usage_guidance"],
        "related_metrics_nl": [str(x).strip() for x in (parsed.get("related_metrics_nl") or []) if str(x).strip()][:5],
        "experience_hints": [str(x).strip() for x in (parsed.get("experience_hints") or []) if str(x).strip()][:5],
        "knowledge_notes": pf["knowledge_notes"],
    }


__all__ = [
    "PROMPTS",
    "PICKER_PROMPT",
    "SYNTHESIS_JSON_SCHEMA",
    "PICKER_JSON_SCHEMA",
    "DEFAULT_CONFIG",
    "load_explore_synthesis_config",
    "_resolve_prompt",
    "_placeholder_fields",
    "expand_2hop",
    "_gather_1hop_candidates",
    "pick_2hop_targets_llm",
    "synthesize_entity_context_llm",
    "synthesize_from_subgraph",
    "apply_field_toggles",
]

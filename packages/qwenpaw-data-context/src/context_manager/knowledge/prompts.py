"""LLM prompts + JSON schemas for knowledge ingest pipeline."""
from __future__ import annotations

PASS_A_ENTITY_TYPES: tuple[str, ...] = (
    "model_family",
    "model_version",
    "product",
    "platform",
    "feature",
    "subscription_plan",
    "company",
    "team",
    "customer",
    "scenario",
    "concept",
    "other",
)

PASS_A_EVENT_TYPES: tuple[str, ...] = (
    "release",
    "deprecation",
    "policy_change",
    "system_upgrade",
    "business_change",
    "other",
)

PASS_A_RELATION_SUBTYPES: tuple[str, ...] = (
    "synonym",
    "antonym",
    "competitor",
    "complement",
    "correlates",
    "see_also",
)

PASS_A_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["entities", "events", "entity_relations", "self_confidence"],
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["surface", "type", "aliases", "evidence_quote"],
                "properties": {
                    "surface": {"type": "string"},
                    "type": {"type": "string", "enum": list(PASS_A_ENTITY_TYPES)},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "definition": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                },
            },
        },
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "type", "date_from", "description", "about_surface"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": list(PASS_A_EVENT_TYPES)},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "description": {"type": "string"},
                    "about_surface": {"type": "string"},
                },
            },
        },
        "entity_relations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["from_surface", "to_surface", "relation_subtype", "description"],
                "properties": {
                    "from_surface": {"type": "string"},
                    "to_surface": {"type": "string"},
                    "relation_subtype": {
                        "type": "string",
                        "enum": list(PASS_A_RELATION_SUBTYPES),
                    },
                    "description": {"type": "string"},
                    "scope": {"type": "string"},
                },
            },
        },
        "self_confidence": {"type": "number"},
    },
}

PASS_B_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["clusters", "self_confidence"],
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "canonical_key",
                    "canonical_name",
                    "entity_type",
                    "surfaces",
                    "aliases",
                    "best_evidence_quote",
                ],
                "properties": {
                    "canonical_key": {"type": "string"},
                    "canonical_name": {"type": "string"},
                    "entity_type": {"type": "string"},
                    "surfaces": {"type": "array", "items": {"type": "string"}},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "best_evidence_quote": {"type": "string"},
                    "lifecycle_state": {
                        "type": "string",
                        "enum": [
                            "active",
                            "archived",
                            "frozen",
                            "invalidated",
                            "superseded",
                            "needs_revalidation",
                        ],
                    },
                },
            },
        },
        "self_confidence": {"type": "number"},
    },
}

PASS_B_CLUSTER_SCHEMA: dict = {
    "type": "object",
    "required": ["clusters", "self_confidence"],
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["canonical_name", "surfaces", "aliases", "best_evidence_quote"],
                "properties": {
                    "canonical_name": {"type": "string"},
                    "surfaces": {"type": "array", "items": {"type": "string"}},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "best_evidence_quote": {"type": "string"},
                    "lifecycle_state": {
                        "type": "string",
                        "enum": [
                            "active",
                            "archived",
                            "frozen",
                            "invalidated",
                            "superseded",
                            "needs_revalidation",
                        ],
                    },
                },
            },
        },
        "self_confidence": {"type": "number"},
    },
}

# ── Per-type labels and merge rules used by pass_b_system() ──────────────────

_PASS_B_TYPE_LABELS: dict[str, str] = {
    "model_family": "AI model families — abstract series names (e.g. AlphaChat, BetaGen, Claude, GLM)",
    "model_version": "specific AI model versions — concrete release identifiers (e.g. alpha-3.6-plus, beta-2.6-i2v, glm-5)",
    "product": "software products and tools (e.g. Cursor, Acme Code/CodeAssist, AcmeHub, Coze)",
    "platform": "operator consoles and platforms (e.g. AcmeHub控制台, ModelHub控制台)",
    "feature": "product features and capabilities (e.g. 图生视频, 知识库, agent模式, 工作流)",
    "subscription_plan": "subscription and pricing plans (e.g. Coding Plan, Pro, Plus, 按量计费)",
    "company": "companies and organizations (e.g. Acme Corp/Acme科技, CloudCorp, OpenAI, Anthropic)",
    "team": "internal teams and business units (e.g. Acme Retail, Acme Labs, Gamma Team)",
    "customer": "external customers and clients (e.g. Acme客户, Beta Industries, Gamma Corp)",
    "scenario": "business application scenarios (e.g. 内容创作, AI搜索, 数据标注)",
    "concept": "abstract concepts, protocols, methodologies (e.g. RAG, MCP协议, 大模型, AI Agent)",
    "other": "entities not fitting the above categories",
}

_PASS_B_TYPE_MERGE_RULES: dict[str, str] = {
    "model_family": (
        "Merge surfaces that name the SAME model family across languages or abbreviations "
        "('AlphaChat' ↔ 'Alpha'; 'BetaGen' ↔ 'ImageGen'). "
        "This batch contains only family-level names. If a surface carries an explicit version "
        "suffix or number (like '-plus', '2.6', '-i2v', '-turbo'), it does not belong here — "
        "note the anomaly in self_confidence if you encounter one."
    ),
    "model_version": (
        "Merge surfaces that unambiguously denote the SAME model version: "
        "Chinese↔English variants of the same release ('AlphaChat-Plus' ↔ 'alpha-plus'), and "
        "date-stamped snapshots of the same base release "
        "('AlphaChat-Plus-2025-04-28' and 'AlphaChat-Plus-2025-01-25' are snapshots of 'alpha-plus' "
        "— they may cluster together unless evidence treats them as distinct deployments). "
        "HARD RULE: NEVER merge two surfaces with DIFFERENT version numbers. "
        "'beta-2.6-i2v' and 'beta-2.7-i2v' are DIFFERENT entities — they must be in separate clusters "
        "even though they share a family. When version numbers differ, always split."
    ),
    "company": (
        "Merge surfaces that name the same legal entity or well-known organization across languages "
        "('Acme科技' ↔ 'Acme AI'; 'Globex' ↔ 'Globex Inc'; 'CloudCorp' ↔ 'Cloud Corporation'). "
        "Be conservative: subsidiaries or brands often deserve their own cluster — "
        "only merge when evidence clearly equates them to the parent."
    ),
    "product": (
        "Merge surfaces that name the same shippable product across languages or abbreviations "
        "('Acme Code' ↔ 'CodeAssist' ↔ 'Acme助手'). "
        "Different products from the same company stay separate."
    ),
    "feature": (
        "Merge surfaces that clearly describe the same product feature under different names "
        "('图生视频' ↔ 'Image-to-Video' ↔ 'I2V' when they clearly refer to the same feature). "
        "Features from different products stay separate even if similar in function."
    ),
    "concept": (
        "Merge surfaces that refer to the same abstract concept, standard, or methodology "
        "across languages or abbreviations "
        "('检索增强生成' ↔ 'RAG'; '大语言模型' ↔ '大模型' ↔ 'LLM'). "
        "Keep distinct concepts separate even if closely related."
    ),
}

PASS_C_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["kg_topology", "self_confidence"],
    "properties": {
        "kg_topology": {
            "type": "object",
            "required": ["nodes", "relationships"],
            "properties": {
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["label", "key", "properties"],
                        "properties": {
                            "label": {"type": "string", "enum": ["Entity"]},
                            "key": {"type": "string"},
                            "properties": {"type": "object", "additionalProperties": True},
                        },
                    },
                },
                "relationships": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["rel_type", "from_key", "to_key"],
                        "properties": {
                            "rel_type": {
                                "type": "string",
                                "enum": [
                                    "RELATED_TO",
                                    "HAS_INSTANCE",
                                ],
                            },
                            "from_key": {"type": "string"},
                            "to_key": {"type": "string"},
                            "properties": {"type": "object", "additionalProperties": True},
                        },
                    },
                },
            },
        },
        "self_confidence": {"type": "number"},
    },
}

PASS_D_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["edges", "self_confidence"],
    "properties": {
        "connection_assessment": {
            "type": "string",
            "description": "Brief read on MG/TG/KG snapshot and how edges relate zones (optional).",
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["rel_type", "from_key", "to_key"],
                "properties": {
                    "rel_type": {
                        "type": "string",
                        "enum": [
                            "SURFACE_METRIC",
                            "SURFACE_DIMENSION",
                            "SURFACE_DOMAIN",
                            "SURFACE_OPERATOR",
                            "ABOUT",
                            "CONCERNS",
                            "RELATED_TO",
                            "HAS_INSTANCE",
                        ],
                    },
                    "from_key": {"type": "string"},
                    "to_key": {"type": "string"},
                    "role": {"type": "string"},
                    "rationale": {"type": "string"},
                    "relation_subtype": {
                        "type": "string",
                        "description": "RELATED_TO: synonym|antonym|competitor|complement|correlates|see_also",
                    },
                    "description": {"type": "string"},
                    "scope": {"type": "string"},
                    "sim_score": {"type": "number"},
                },
            },
        },
        "self_confidence": {"type": "number"},
    },
}


PASS_C_EDGES_SCHEMA: dict = {
    "type": "object",
    "required": ["relationships", "self_confidence"],
    "properties": {
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["rel_type", "from_key", "to_key"],
                "properties": {
                    "rel_type": {
                        "type": "string",
                        "enum": ["RELATED_TO", "HAS_INSTANCE"],
                    },
                    "from_key": {"type": "string"},
                    "to_key": {"type": "string"},
                    "properties": {"type": "object", "additionalProperties": True},
                },
            },
        },
        "self_confidence": {"type": "number"},
    },
}


def pass_a_system() -> str:
    return (
        "You extract structured knowledge entities, events and relationships from Chinese "
        "operational / research / how-to documents for a business knowledge graph. "
        "Only extract concepts that belong to the **knowledge layer**: products, models, "
        "features, scenarios, companies, customers, etc. Do **NOT** extract metrics, "
        "dimensions, datasets, calibers or SQL formulas — those live in a separate metadata "
        "graph and would pollute this layer.\n\n"
        "Output ONLY one JSON object with these top-level keys (use [] when nothing fits): "
        "`entities`, `events`, `entity_relations`, `self_confidence` (number 0..1).\n\n"
        "## entities[]\n"
        "Required fields: surface, type, aliases, evidence_quote. Optional: definition.\n"
        "- `surface`: the most canonical form of the name as written in the chunk; do not "
        "  translate, do not paraphrase, do not add quotes. Prefer the version with the most "
        "  characters of context (e.g. `beta-2.6-i2v` over `beta-2.6`).\n"
        "- `type`: MUST be exactly one of these 12 values: "
        + ", ".join(PASS_A_ENTITY_TYPES) + ".\n"
        "  Type boundary rules:\n"
        "    * model_family vs model_version: family is the abstract series name with NO "
        "      version suffix (`AlphaChat`, `BetaGen`, `GLM`, `Claude`); version has a "
        "      concrete version / variant suffix (`alpha-3.6-plus`, `beta-2.6-i2v`, `glm-5`).\n"
        "    * product: any concrete shippable product, INCLUDING coding-agent IDEs and "
        "      tools like `Cursor`, `Cline`, `Claude Code`, `Qoder`, `Acme Code`, plus model "
        "      services like `AcmeHub`, `ModelHub`.\n"
        "    * platform: the operator-facing console/portal that hosts products "
        "      (`AcmeHub控制台`, `ModelHub 控制台`).\n"
        "    * feature: a function inside a product (`图生视频`, `知识库`, `agent 模式`).\n"
        "    * subscription_plan: a billing tier (`Coding Plan`, `Plus`, `Pro`, `按量计费`).\n"
        "    * company: legal entities / large groups (`CloudCorp`, `Acme Corp`, `OpenAI`).\n"
        "    * team: BU or team inside a company (`Acme Retail`, `Gamma Team`, `Acme Labs`).\n"
        "    * customer: external callers / clients (`Acme客户`, `Beta Industries`, `Gamma Corp`).\n"
        "    * scenario: business application scenario (`内容创作`, `数据标注`, `AI 搜索`).\n"
        "    * concept: abstract idea / protocol / methodology (`大模型`, `RAG`, "
        "      `MCP 协议`, `AI Agent`). `MCP` the protocol → concept; a specific MCP "
        "      server implementation → product.\n"
        "    * other: only when nothing else fits; do not abuse it.\n"
        "- `aliases`: alternative names that **literally appear in the chunk** (e.g. when "
        "  the chunk says `AlphaChat (Beta)` you may list `Beta` as an alias). Never invent "
        "  translations or external knowledge.\n"
        "- `definition` (optional): one short sentence describing what the entity is, in "
        "  the chunk's own words. Use it when the chunk explicitly defines the concept; "
        "  omit otherwise.\n"
        "- `evidence_quote`: the SHORTEST verbatim sentence (≤200 chars) from the chunk "
        "  that contains the surface AND enough context to identify what it is. Do not "
        "  paraphrase. Do not concatenate sentences.\n\n"
        "## events[]\n"
        "Required: name, type, date_from, description, about_surface. Optional: date_to.\n"
        "- `type`: exactly one of: " + ", ".join(PASS_A_EVENT_TYPES) + ".\n"
        "- `date_from`: `YYYY-MM` or `YYYY-MM-DD`; leave empty string when truly unknown.\n"
        "- `about_surface`: MUST exactly equal a `surface` you also emit in `entities`. "
        "  Events whose subject is not in `entities` will be discarded downstream, so "
        "  either add the subject to `entities` first or omit the event.\n\n"
        "## entity_relations[]\n"
        "Required: from_surface, to_surface, relation_subtype, description. Optional: scope.\n"
        "- `from_surface` and `to_surface` MUST both equal `surface` values from `entities`.\n"
        "- `relation_subtype`: exactly one of: "
        + ", ".join(PASS_A_RELATION_SUBTYPES) + ".\n"
        "  Use `competitor` for rivals, `complement` for tech-stack pairings, `synonym` "
        "  for equivalent surfaces, `correlates` for statistical co-movement, `see_also` "
        "  as the generic fallback. Do not invent other subtypes.\n\n"
        "Quality bar: prefer fewer, well-grounded extractions over many speculative ones. "
        "Output strict JSON only (no markdown, no commentary)."
    )


def pass_a_user(chunk_text: str, source_doc: str) -> str:
    example = (
        "Example input (for guidance only — do NOT echo this):\n"
        "  Text: \"3 月 BetaGen 全球 DAU 9.3 万，使用主要模型是 beta-2.6-i2v 和 beta-2.6-image。"
        "4 月新模型 beta-2.7-i2v 上线，但点赞率仅 27%。ImageGen和NovaGen在国内市场是直接竞争对手。\"\n"
        "Example output:\n"
        '{"entities":[\n'
        '  {"surface":"BetaGen","type":"model_family","aliases":["ImageGen"],'
        '"evidence_quote":"3 月 BetaGen 全球 DAU 9.3 万，使用主要模型是 beta-2.6-i2v 和 beta-2.6-image"},\n'
        '  {"surface":"beta-2.6-i2v","type":"model_version","aliases":[],'
        '"evidence_quote":"使用主要模型是 beta-2.6-i2v 和 beta-2.6-image"},\n'
        '  {"surface":"beta-2.7-i2v","type":"model_version","aliases":[],'
        '"evidence_quote":"4 月新模型 beta-2.7-i2v 上线，但点赞率仅 27%"},\n'
        '  {"surface":"NovaGen","type":"model_family","aliases":[],'
        '"evidence_quote":"ImageGen和NovaGen在国内市场是直接竞争对手"}\n'
        '],"events":[\n'
        '  {"name":"beta-2.7-i2v 上线","type":"release","date_from":"2026-04",'
        '"description":"4 月新模型 beta-2.7-i2v 上线","about_surface":"beta-2.7-i2v"}\n'
        '],"entity_relations":[\n'
        '  {"from_surface":"BetaGen","to_surface":"NovaGen","relation_subtype":"competitor",'
        '"description":"在国内市场直接竞争"}\n'
        '],"self_confidence":0.85}\n'
    )
    return (
        f"Source document name: {source_doc}\n\n"
        f"{example}\n"
        "Now extract from the text below. Return ONLY the JSON object as specified.\n\n"
        "### Text chunk\n"
        f"{chunk_text}\n"
    )


def pass_b_system(entity_type: str = "") -> str:
    """Type-aware system prompt for Pass B: cluster entity mentions into canonical groups.

    ``entity_type`` should be one of the 12 Pass A entity types; when empty a generic
    prompt is returned (used for backward-compat display and skip_llm stubs).
    """
    type_label = _PASS_B_TYPE_LABELS.get(entity_type, entity_type or "entities")
    merge_rule = _PASS_B_TYPE_MERGE_RULES.get(
        entity_type,
        (
            f"Merge surfaces that clearly refer to the same real-world {entity_type or 'entity'} "
            "across different spellings, languages, or abbreviations. "
            "When uncertain whether two surfaces denote the same entity, keep them in separate clusters."
        ),
    )
    return (
        "You are consolidating entity mention strings extracted from Chinese business documents "
        "into canonical clusters. Each cluster represents one distinct real-world entity.\n\n"
        f"Entity type in this batch: **{type_label}**\n\n"
        f"Merge guidance: {merge_rule}\n\n"
        "For each cluster output:\n"
        "• `canonical_name`: the most complete, recognizable name for this entity. "
        "Prefer the official English or internationally known form when unambiguous "
        "(e.g. 'Acme AI' over 'Acme科技'); otherwise use the most complete original form. "
        "For model versions always include the version identifier.\n"
        "• `surfaces`: EVERY surface string from the input that refers to this entity "
        "(copy verbatim — do not paraphrase or invent).\n"
        "• `aliases`: well-known alternative names NOT already in `surfaces` "
        "(omit rather than guess — only add when highly confident).\n"
        "• `best_evidence_quote`: the shortest input `evidence` string that clearly identifies "
        "this entity (copy verbatim; use empty string when no evidence is provided).\n"
        "• `lifecycle_state` (optional): omit or use `active` by default; use `archived`, "
        "`superseded`, or `needs_revalidation` only when the input evidence explicitly indicates it.\n\n"
        "Strict rules:\n"
        "1. Every input surface must appear in exactly one cluster — no omissions, no duplicates.\n"
        "2. Never invent surfaces not present in the input.\n"
        "3. When in doubt whether two surfaces name the same entity, put them in separate clusters. "
        "A missed merge is easier to fix later than a wrong merge.\n"
        "4. Top-level `self_confidence` (0.0–1.0) reflects your overall certainty.\n"
        "Output ONLY valid JSON."
    )


def pass_b_user(records_json: str, entity_type: str = "") -> str:
    """User-turn prompt for Pass B.

    ``records_json`` is a JSON array where each item has
    ``surface``, ``evidence``, ``aliases``, ``chunk_count``, optional ``definition``.
    """
    type_label = _PASS_B_TYPE_LABELS.get(entity_type, entity_type or "entities")
    minimal = (
        '{"clusters":['
        '{"canonical_name":"Canonical Name",'
        '"surfaces":["surface1","surface2"],'
        '"aliases":[],'
        '"best_evidence_quote":"verbatim evidence text"}'
        '],"self_confidence":0.85}'
    )
    return (
        f"### Entity surfaces to cluster (type: {type_label})\n"
        "Each item: `surface` (the raw mention string), `evidence` (verbatim context — may be empty), "
        "`aliases` (Pass A–identified alternative names), `chunk_count` (how many document chunks "
        "mentioned this surface — higher means it appears more frequently).\n\n"
        f"{records_json}\n\n"
        "Return JSON with:\n"
        "- `clusters`: array — each cluster needs `canonical_name`, `surfaces` (all input surfaces "
        "belonging to this entity), `aliases`, `best_evidence_quote`; optionally `lifecycle_state`.\n"
        "- `self_confidence`: number 0–1.\n"
        f"Minimal valid shape (replace with real values): {minimal}"
    )


def pass_c_system() -> str:
    """System prompt for Pass C (edges-only).

    Entity nodes are generated mechanically by the pipeline from Pass B clusters —
    the LLM is NOT asked to produce nodes, eliminating the empty-nodes failure mode.
    The only cognitive task here is identifying structural relationships.
    """
    return (
        "You are adding relationship edges to a pre-built knowledge graph. "
        "The Entity nodes are already committed to the graph — do NOT include any nodes in your response.\n\n"
        "Your task: given the entity list, identify which entities are structurally related and "
        "output ONLY the relationship edges that are clearly supported.\n\n"
        "## RELATED_TO — semantic relationship between two entities\n"
        "Required: `properties.relation_subtype` — exactly one of:\n"
        "• `competitor` — rival products, models, or platforms targeting the same use case\n"
        "• `complement` — tech-stack pairing; one uses, enables, or enhances the other\n"
        "• `correlates` — statistical co-movement or strong business correlation\n"
        "• `synonym` — same entity described differently (only when clustering missed the merge)\n"
        "• `antonym` — mutually exclusive or opposing alternatives\n"
        "• `see_also` — generic related; use as last resort when no other subtype fits\n\n"
        "## HAS_INSTANCE — hierarchy edge from abstract to concrete\n"
        "Direction is always **from_key = abstract entity → to_key = concrete instance**.\n"
        "Typical type-pair patterns to look for:\n"
        "• model_family → model_version (e.g. AlphaChat → alpha-3.6-plus; BetaGen → beta-2.6-i2v)\n"
        "• company → product (e.g. Acme Corp → AlphaChat)\n"
        "• product → feature (e.g. AcmeHub → 知识库; AcmeHub → 图生视频)\n"
        "• concept → product (e.g. AI Agent → Cursor)\n"
        "• scenario → feature (e.g. 内容创作 → 图生视频)\n\n"
        "Hard constraints:\n"
        "1. `from_key` and `to_key` must be ent: keys from the provided entity list — never invent a key.\n"
        "2. Omit an edge when evidence is absent or the relationship is ambiguous.\n"
        "3. `relationships` may be [] — that is valid and preferred over speculative edges.\n"
        "4. Do NOT output any `nodes` field — only `relationships` and `self_confidence`.\n"
        "5. Do not output ABOUT, CAUSES, ev: endpoints, or any MG/TG edge types here.\n"
        "Output ONLY valid JSON: {\"relationships\": [...], \"self_confidence\": 0.0–1.0}."
    )


def pass_c_user(
    payload: str,
    *,
    batch_index: int | None = None,
    n_batches: int | None = None,
    outer_round: int | None = None,
    max_outer_rounds: int | None = None,
) -> str:
    """User-turn prompt for Pass C (edges-only).

    ``payload`` is either a JSON array of ``{key, name, type}`` entity rows (round 1),
    or a wrapped object with ``entity_batch`` + ``prior_entity_keys`` (round 2+).
    """
    sk = (
        '{"relationships":['
        '{"rel_type":"HAS_INSTANCE",'
        '"from_key":"ent:model_family:alphachat","to_key":"ent:model_version:alpha-3-6-plus"},'
        '{"rel_type":"RELATED_TO",'
        '"from_key":"ent:model_family:betagen","to_key":"ent:model_family:novagen",'
        '"properties":{"relation_subtype":"competitor","description":"both compete in video generation"}}'
        '],"self_confidence":0.82}'
    )
    notes: list[str] = []
    if batch_index is not None and n_batches is not None and n_batches > 1:
        notes.append(
            f"This is **batch {batch_index} of {n_batches}** (a slice of all entities). "
            "Add edges within this batch. Cross-batch edges can be added in later refinement rounds."
        )
    if outer_round is not None and max_outer_rounds is not None and max_outer_rounds > 1:
        if outer_round > 1:
            notes.append(
                f"**Refinement round {outer_round} of {max_outer_rounds}**. "
                "The payload includes `entity_batch` (current slice) and `prior_entity_keys` (other batches). "
                "You may add RELATED_TO / HAS_INSTANCE between current-batch entities and those prior keys. "
                "Avoid duplicating pairs already listed in `prior_relationships`."
            )
        else:
            notes.append(
                f"**Round 1 of {max_outer_rounds}** (plain entity list). "
                "Later rounds will supply prior entity keys for cross-batch edges."
            )
    batch_note = ("\n\n### Batch / round context\n" + "\n".join(notes) + "\n") if notes else ""
    return (
        "### Entity list (nodes already committed — add edges only)\n"
        "Each item: `key` (ent: identifier), `name`, `type`.\n\n"
        f"{payload}\n"
        f"{batch_note}\n"
        "Return JSON: `relationships` (array, may be []) and `self_confidence` (0–1).\n"
        "Each relationship needs: `rel_type` (RELATED_TO or HAS_INSTANCE), `from_key`, `to_key`; "
        "for RELATED_TO also `properties.relation_subtype`.\n"
        f"Shape example (use real keys from the list above, not these placeholders): {sk}"
    )


def pass_d_system() -> str:
    """System prompt for Pass D: cross-graph edge proposal.

    Key discipline changes vs earlier versions:
    - No 'Pass D runs after…' framing (avoids spurious priors).
    - ent: key rule is the FIRST hard constraint — fabricated keys are the #1 failure mode.
    """
    return (
        "You are connecting knowledge-layer entities to a metadata graph (MG) and a trace graph (TG). "
        "You are given a JSON snapshot containing: MG catalog keys, existing knowledge-graph (:Entity) rows "
        "already in Neo4j, sample TG :Task / :Step rows, zone counts, and "
        "**doc_ingest_entities** — the canonical `ent:` keys from the current document's clustering pass "
        "(these may not yet appear in knowledge_graph_entity_sample when running in preview mode).\n\n"
        "Your job: propose cross-graph edges (MG↔KG, TG↔KG) and KG-internal ent:↔ent: edges.\n\n"
        "## CRITICAL — key discipline\n\n"
        "**ent: keys (strict)**\n"
        "The ONLY valid `ent:` keys are those listed verbatim in `doc_ingest_entities[].canonical_key`. "
        "Copy them character-for-character. NEVER construct, guess, abbreviate, or paraphrase `ent:` keys. "
        "If no `ent:` key in the list matches a desired entity, do not propose that edge.\n\n"
        "**MG keys (met:/dim:/dom:/op:)**\n"
        "Use only verbatim keys from `metadata_graph_catalog`. Never invent MG keys from prose descriptions.\n\n"
        "**TG keys (task:/plan:)**\n"
        "Use only verbatim keys from `trace_graph_catalog`.\n\n"
        "## Cross-graph edge types\n\n"
        "**KG → MG (ent: → MG key):**\n"
        "• SURFACE_METRIC (ent: → met:, role: primary | alias_of | partial_view)\n"
        "• SURFACE_DIMENSION (ent: → dim:, role: primary | alias_of)\n"
        "• SURFACE_DOMAIN (ent: → dom:)\n"
        "• SURFACE_OPERATOR (ent: → op:)\n\n"
        "**TG → KG (TG key → ent:):**\n"
        "• ABOUT (task: → ent:) — when a Task's goal directly concerns this entity\n"
        "• CONCERNS (plan: → ent:, role: subject | context | filter)\n\n"
        "## KG-internal edges (ent: ↔ ent:)\n"
        "• RELATED_TO (optional properties: relation_subtype synonym|antonym|competitor|complement|correlates|see_also; "
        "description, scope, sim_score)\n"
        "• HAS_INSTANCE (abstract → concrete)\n\n"
        "## Output discipline\n"
        "• Put every confident link into `edges` with a brief `rationale` per edge.\n"
        "• Do not describe links in `connection_assessment` that are absent from `edges`.\n"
        "• If `edges` is empty, `connection_assessment` must only explain why.\n"
        "• Do NOT use :Event nodes, ev: keys, CAUSES, or Event→Entity ABOUT.\n"
        "• If `pass_d_incremental` is present, `doc_ingest_entities` is one batch — use only those keys.\n"
        "Top level MUST include `edges` (array, may be []) and `self_confidence` (0–1). "
        "Optional: `connection_assessment`. Output ONLY valid JSON."
    )


def pass_d_user(payload: str) -> str:
    sk = (
        '{"edges":['
        '{"rel_type":"SURFACE_METRIC","from_key":"ent:<from_doc_ingest_entities>","to_key":"met:<verbatim_from_catalog>",'
        '"role":"primary","rationale":"short reason"},'
        '{"rel_type":"RELATED_TO","from_key":"ent:<a>","to_key":"ent:<b>","relation_subtype":"see_also",'
        '"rationale":"optional description in rationale or separate description field"}'
        '],"self_confidence":0.72,"connection_assessment":"Optional; must not claim links absent from edges."}'
    )
    return (
        "### Neo4j snapshot + this run's doc entities (JSON)\n"
        f"{payload}\n\n"
        "Return JSON: edges (array), self_confidence; optional connection_assessment. "
        "Copy to_key exactly from catalog JSON keys (e.g. metrics[].key); never invent dom:/met: names from prose.\n"
        f"Shape example (replace with real keys only): {sk}\n"
    )

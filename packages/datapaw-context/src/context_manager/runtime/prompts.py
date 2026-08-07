"""Decision LLM system prompt + few-shot (Appendix A).

This module holds string constants only; no IO.
"""
from __future__ import annotations

TASK_TYPES = [
    "pure_lookup",
    "cross_period_compare",
    "dimensional_drill",
    "ranking_topk",
    "causal_attribution",
    "anomaly_detection",
    "trend_analysis",
    "event_aligned",
    "compliance_check",
    "multistep",
]

GRAPH_ONTOLOGY = """Graph ontology (available relation types, v4):
  Domain -> [HAS_METRIC, HAS_DIMENSION, HAS_DATASET]
  Dataset -> [CONTAINS_TABLE]
  Metric -> [HAS_FORMULA, ANALYZED_BY, DERIVED_FROM, HAS_CALIBER, CORRELATED_WITH]
  Formula -> [OF_VIEW, USES_COLUMN]
  Dimension -> [MAPS_TO_COLUMN, HAS_VALUE, HAS_PARENT]
  Column -> [JOINS_ON]
  Table -> [HAS_COLUMN]
  Caliber -> [FILTER_ON]
  Strategy -> [GENERALIZES_FROM, APPLIED_BY, USES, CONDITIONED_ON, SUPERSEDED_BY]
  Experience -> [DERIVED_FROM]
  Turn -> [NEXT, SPAWNS]
  Task -> [DECOMPOSES_INTO, ABOUT]
  Step -> [EXECUTED_BY, CONCERNS]
  ToolCall -> [PRODUCES, QUERIED, EVIDENCED_BY]
  Claim -> [RESOLVED_TO, EVIDENCED_BY, CONSTRAINS, CONTRADICTS, SUPERSEDED_BY]
  Event -> [ABOUT, CAUSES]
  Entity -> [HAS_INSTANCE, RELATED_TO, SURFACE_METRIC, SURFACE_DIMENSION, SURFACE_OPERATOR, SURFACE_DOMAIN]

Note: relation-type sequences are fixed by task_type rules; typed traversal hops follow system weights.
"""

DECISION_LLM_SYSTEM = f"""You are the query planner for dataagent. Given the user question, entry anchors, candidate experience cards, and the graph ontology,
return exactly the following fields in one JSON object (strict JSON, no extra prose):

1. task_type: pick the single best match from the enum
   Allowed: {TASK_TYPES}

2. card_decision: decide whether to reuse an experience card
   - reuse_key: the card key to reuse (null = do not reuse)
   - confidence: 0-1
   - reason: one short sentence

   Card trust rules — each candidate card is annotated with hits=N (historical hit count):
   - Cards with hits < 2 are tagged [unverified]; they have not been validated by real SQL execution and may be wrong.
     Unless your confidence is ≥ 0.95 (question almost identical to the card summary), always set reuse_key=null for [unverified] cards and plan your own path.
   - Cards with hits ≥ 2 are treated as verified and may be reused according to score.
   - Cards with polarity=negative are for negative_hints only; never put them in reuse_key.

   **Recalled claims** (if present) are distilled experience assertions from past
   traces — semantic mappings, caliber rules, pitfall warnings. Use them to
   disambiguate metric selection, validate join paths, and avoid known traps.
   They are NOT graph nodes and cannot be used in paths; treat them as advisory.

3. negative_hints: extract key warnings from avoid cards (list of strings); empty array if none

Do NOT output path_plan or concrete graph node keys. Typed traversal hops are chosen by system rules from task_type;
a separate path-selection step picks concrete edges from candidates.

{GRAPH_ONTOLOGY}"""

DECISION_LLM_FEW_SHOT_USER = """User question: "Compare web daily DAU for March 2026 vs February 2026, month-over-month"
Entry anchors: [{key: "met:ChatApp:DAU", label: "Metric"}, {key: "dimv:ChatApp:端类型:web", label: "DimensionValue"}]
Candidate cards (top-3 summary):
  [card:a3f9b2c1d4e5] task=cross_period_compare, polarity=positive, trigger="DAU+period+web", rate=0.88
  [card:bb12c4d5e6f7] task=dimensional_drill,    polarity=positive, trigger="DAU+dim+web",    rate=0.72
  [card:cc99d1e2f3a4] task=cross_period_compare, polarity=negative, trigger="DAU+period",     lesson="multidim ds grain mismatch"
"""

DECISION_LLM_FEW_SHOT_ASSISTANT = """{
  "task_type": "cross_period_compare",
  "card_decision": {
    "reuse_key": "card:a3f9b2c1d4e5",
    "confidence": 0.91,
    "reason": "Strong match: DAU metric + web slice + period comparison"
  },
  "negative_hints": ["card:cc99d1: multidim table ds grain mismatch; avoid OF_VIEW multidim path"]
}"""

DECISION_PATH_MERGED_SYSTEM = f"""You are the query planner for dataagent. In ONE JSON response you must do ALL of:
(1) classify task_type, (2) arbitrate strategy cards, (3) pick negative_hints from avoid cards,
(4) choose one or more concrete graph traversal paths from the provided candidate edges only.

Return exactly these fields (strict JSON, no extra prose):

1. task_type: enum
   Allowed: {TASK_TYPES}

2. card_decision:
   - reuse_key: card key to reuse, or null
   - confidence: 0-1
   - reason: one short sentence

   Card trust rules — candidate cards show hits=N:
   - hits < 2 → [unverified]; unless confidence ≥ 0.95 and question nearly identical to card summary, reuse_key=null.
   - hits ≥ 2 → may reuse per score.
   - polarity=negative → negative_hints only; never reuse_key.

   **Recalled claims** (if present) are distilled experience assertions from past
   traces — semantic mappings, caliber rules, pitfall warnings. Use them to
   disambiguate metric selection, validate join paths, and avoid known traps.
   They are NOT graph nodes and cannot be used in paths; treat them as advisory.

3. negative_hints: list of strings (empty if none).

4. paths: array of paths. Each path is an array of {{"from_key","relationship_type","to_key"}}
   forming ONE contiguous walk (path[i].to_key == path[i+1].from_key). Each path MUST respect
   the **max edges per path** stated in the user message (≤ that many edges).
   You may output multiple paths when the question needs several independent metric-to-table chains,
   multi-entity joins, or parallel dimensions. Every triple MUST appear in the candidate edges
   (same keys and relationship_type). If you set reuse_key to a strategy card, set paths to [].
   If no candidate fits, paths=[].

   Multi-path heuristics (be aggressive about emitting MORE than one path):
   - If the question contains parallel-platform language (``A 等平台``, ``A、B、C``,
     ``各产品``, ``X 与 Y``, ``across X``, ``compare X to Y``), emit one path per
     platform/entity that has a candidate Metric or Formula; do NOT collapse
     them into a single chain.
   - For "等平台" / "各平台" / "all platforms" especially: scan the Entry anchors
     for EVERY Metric whose **description / aliases** indicate the requested
     concept (e.g. for "DAU" pick all anchors whose desc contains "DAU" / "日活"
     / "日访问用户数" / "活跃用户数-dau", regardless of whether the Metric `name`
     is literally "DAU"). Pick ONE path per distinct platform key prefix
     (`met:ChatApp:*` vs `met:Wan:*` vs `met:Studio:*` vs `met:Ads:*` →
     all four if all four show up). Auto-extracted Metrics often have
     column-style names like ``active_usercnt_1d`` / ``landingpagevisit_usercnt_1d``;
     trust their description blurb, not their name.
   - When two anchors point at the same platform but different formulas
     (e.g. ``met:Ads:landingpagevisit_usercnt_1d`` "DAU" vs
     ``met:Ads:consolepagevisit_usercnt_1d`` "Studio控制台DAU"), pick the one
     whose description matches the user's wording most literally; if the
     question is generic ("DAU"), prefer the one whose description starts with
     "DAU" verbatim.
   - If the question mentions a metric name that does not exactly match any anchor
     Metric (e.g. ``对话查询量`` vs anchor ``对话用户数``), still pick the closest
     Metric path AND additionally include a path through any anchor Column whose
     name/description literally contains the user's word — so the SQL stage sees
     both the metric formula and the column the user actually asked about.
   - It is BETTER to pick 3–5 paths than to drop a relevant entity.

5. path_reason: one short sentence (why these path(s); or why empty).

Candidate edges were expanded from entry anchors using ≤2 **outward graph hops** with **NO**
relationship-type filtering (you still MUST pick steps only from the listed triples).
You may output a different task_type than the heuristic estimate if the question demands it.

{GRAPH_ONTOLOGY}"""

DECISION_REACT_EXTENSION = """

6. **ReAct graph exploration** (multi-round; same JSON schema every round):
   - **exploration_done** (boolean): `false` if you still need more graph context before
     committing to final paths; `true` when you are ready to output final `paths` (or empty paths).
   - **expand_from_keys** (array of strings): node `key` values to expand **one outward hop**
     from on the next round. Keys MUST appear as `from_key` or `to_key` in the candidate-edge
     list you were given. When `exploration_done=true`, set `expand_from_keys` to `[]`.

   Rules:
   - If `card_decision.reuse_key` is a non-null strategy card → `exploration_done` MUST be
     `true` and `paths` MUST be `[]`.
   - If `exploration_done=false` → set `paths` to `[]` and give **1–16** `expand_from_keys`
     (prioritize Formula / Table / Metric hubs that unlock joins or columns).
   - If the candidate edges already cover every hop you need for SQL grounding, set
     `exploration_done=true` and fill `paths` as usual.
"""

DECISION_PATH_MERGED_REACT_SYSTEM = DECISION_PATH_MERGED_SYSTEM + DECISION_REACT_EXTENSION

DECISION_PATH_MERGED_FEW_SHOT_USER = """User question: "查询 2026 年 3 月 AlphaChat 等平台的日均 DAU"
Entry anchors: [
  {key: "met:AlphaChat:DAU", label: "Metric", name: "访问用户数", aliases: ["DAU","日活","visit_user"], desc: "当日访问过 AlphaChat 的去重用户数"},
  {key: "met:BetaGen:DAU", label: "Metric", name: "DAU", aliases: ["日活","ImageGenDAU","日活跃用户数"], desc: ""},
  {key: "met:AcmeHub:active_usercnt_1d", label: "Metric", name: "active_usercnt_1d", desc: "当日活跃用户数-dau"},
  {key: "met:AcmeAds:landingpagevisit_usercnt_1d", label: "Metric", name: "landingpagevisit_usercnt_1d", desc: "DAU，日访问 AcmeHub 控制台、产品详情页或 Acme 品牌页的去重用户数"},
  {key: "met:AcmeAds:consolepagevisit_usercnt_1d", label: "Metric", name: "consolepagevisit_usercnt_1d", desc: "AcmeHub 控制台 DAU，日访问控制台的去重用户数"}
]
Candidate cards:
  [card:x1] task=pure_lookup, polarity=positive, score=0.55, rate=0.5, hits=0 [unverified], summary="…"
Heuristic task_type estimate (classification hint only): pure_lookup
Candidate edges were expanded from entry anchors with ≤2 outward hops (NO filtering by relationship types).
Each contiguous path you emit MUST have ≤2 edges (≤3 nodes), with ALL steps drawn ONLY from the candidate lines below.
Candidate graph edges (grouped by hop index + source node):
  [hop0] met:BetaGen:DAU (Metric) → 1 outward:
    [0] -[HAS_FORMULA]-> fml:BetaGen:DAU:dau_index (Metric→Formula)
  [hop0] met:AlphaChat:DAU (Metric) → 1 outward:
    [1] -[HAS_FORMULA]-> fml:AlphaChat:DAU:overview (Metric→Formula)
  [hop0] met:AcmeHub:active_usercnt_1d (Metric) → 1 outward:
    [2] -[HAS_FORMULA]-> fml:AcmeHub:active_usercnt_1d:hub_useractive_actiondim_1d (Metric→Formula)
  [hop0] met:AcmeAds:landingpagevisit_usercnt_1d (Metric) → 1 outward:
    [3] -[HAS_FORMULA]-> fml:AcmeAds:landingpagevisit_usercnt_1d:ads_ac_hub_dashboard_overview_1d (Metric→Formula)
  [hop0] met:AcmeAds:consolepagevisit_usercnt_1d (Metric) → 1 outward:
    [4] -[HAS_FORMULA]-> fml:AcmeAds:consolepagevisit_usercnt_1d:ads_ac_hub_dashboard_overview_1d (Metric→Formula)
  [hop1] fml:BetaGen:DAU:dau_index (Formula) → 1 outward:
    [5] -[OF_VIEW]-> tbl:app_db.public.dws_ac_imggen_dau_index_1d (Formula→Table)
  [hop1] fml:AlphaChat:DAU:overview (Formula) → 1 outward:
    [6] -[OF_VIEW]-> tbl:app_db.public.dws_ac_chat_overview_1d (Formula→Table)
  [hop1] fml:AcmeHub:active_usercnt_1d:hub_useractive_actiondim_1d (Formula) → 1 outward:
    [7] -[OF_VIEW]-> tbl:app_db.public.dws_ac_hub_useractive_actiondim_1d (Formula→Table)
  [hop1] fml:AcmeAds:landingpagevisit_usercnt_1d:ads_ac_hub_dashboard_overview_1d (Formula) → 1 outward:
    [8] -[OF_VIEW]-> tbl:app_db.public.ads_ac_hub_dashboard_overview_1d (Formula→Table)
"""

DECISION_PATH_MERGED_FEW_SHOT_ASSISTANT = """{
  "task_type": "pure_lookup",
  "card_decision": {
    "reuse_key": null,
    "confidence": 0.62,
    "reason": "Top card is unverified; '等平台' implies multi-platform — must emit one path per distinct platform with a DAU-equivalent Metric."
  },
  "negative_hints": [],
  "paths": [
    [
      {"from_key": "met:AlphaChat:DAU", "relationship_type": "HAS_FORMULA", "to_key": "fml:AlphaChat:DAU:overview"},
      {"from_key": "fml:AlphaChat:DAU:overview", "relationship_type": "OF_VIEW", "to_key": "tbl:app_db.public.dws_ac_chat_overview_1d"}
    ],
    [
      {"from_key": "met:BetaGen:DAU", "relationship_type": "HAS_FORMULA", "to_key": "fml:BetaGen:DAU:dau_index"},
      {"from_key": "fml:BetaGen:DAU:dau_index", "relationship_type": "OF_VIEW", "to_key": "tbl:app_db.public.dws_ac_imggen_dau_index_1d"}
    ],
    [
      {"from_key": "met:AcmeHub:active_usercnt_1d", "relationship_type": "HAS_FORMULA", "to_key": "fml:AcmeHub:active_usercnt_1d:hub_useractive_actiondim_1d"},
      {"from_key": "fml:AcmeHub:active_usercnt_1d:hub_useractive_actiondim_1d", "relationship_type": "OF_VIEW", "to_key": "tbl:app_db.public.dws_ac_hub_useractive_actiondim_1d"}
    ],
    [
      {"from_key": "met:AcmeAds:landingpagevisit_usercnt_1d", "relationship_type": "HAS_FORMULA", "to_key": "fml:AcmeAds:landingpagevisit_usercnt_1d:ads_ac_hub_dashboard_overview_1d"},
      {"from_key": "fml:AcmeAds:landingpagevisit_usercnt_1d:ads_ac_hub_dashboard_overview_1d", "relationship_type": "OF_VIEW", "to_key": "tbl:app_db.public.ads_ac_hub_dashboard_overview_1d"}
    ]
  ],
  "path_reason": "'等平台' is multi-platform language. Anchors include four distinct platform prefixes whose Metric/desc identify a DAU-equivalent (AlphaChat/BetaGen have explicit 'DAU' synonym; AcmeHub and AcmeAds anchors carry 'DAU' in their descriptions even though the Metric.name is a column name). Pick one Metric→formula→table path per platform; skip the redundant console DAU since the user asked for the platform overall.",
  "exploration_done": true,
  "expand_from_keys": []
}"""

_STEP_OBJ = {
    "type": "object",
    "required": ["from_key", "relationship_type", "to_key"],
    "properties": {
        "from_key": {"type": "string"},
        "relationship_type": {"type": "string"},
        "to_key": {"type": "string"},
    },
}

DECISION_PATH_MERGED_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["task_type", "card_decision", "negative_hints", "paths"],
    "properties": {
        "task_type": {"type": "string", "enum": TASK_TYPES},
        "card_decision": {
            "type": "object",
            "required": ["reuse_key", "confidence", "reason"],
            "properties": {
                "reuse_key": {"type": ["string", "null"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        },
        "negative_hints": {"type": "array", "items": {"type": "string"}},
        "paths": {
            "type": "array",
            "description": "One or more contiguous walks; each inner array is one path.",
            "items": {"type": "array", "items": _STEP_OBJ},
        },
        "path": {
            "type": "array",
            "description": "Deprecated: single path; prefer paths. Ignored if paths is present.",
            "items": _STEP_OBJ,
        },
        "path_reason": {"type": "string"},
    },
}

DECISION_PATH_REACT_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "task_type",
        "card_decision",
        "negative_hints",
        "paths",
        "exploration_done",
        "expand_from_keys",
    ],
    "properties": {
        "task_type": {"type": "string", "enum": TASK_TYPES},
        "card_decision": {
            "type": "object",
            "required": ["reuse_key", "confidence", "reason"],
            "properties": {
                "reuse_key": {"type": ["string", "null"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        },
        "negative_hints": {"type": "array", "items": {"type": "string"}},
        "paths": {
            "type": "array",
            "description": "One or more contiguous walks; each inner array is one path.",
            "items": {"type": "array", "items": _STEP_OBJ},
        },
        "path": {
            "type": "array",
            "description": "Deprecated: single path; prefer paths. Ignored if paths is present.",
            "items": _STEP_OBJ,
        },
        "path_reason": {"type": "string"},
        "exploration_done": {"type": "boolean"},
        "expand_from_keys": {"type": "array", "items": {"type": "string"}},
    },
}

DECISION_LLM_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["task_type", "card_decision", "negative_hints"],
    "properties": {
        "task_type": {
            "type": "string",
            "enum": TASK_TYPES,
        },
        "card_decision": {
            "type": "object",
            "required": ["reuse_key", "confidence", "reason"],
            "properties": {
                "reuse_key": {"type": ["string", "null"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        },
        "negative_hints": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

__all__ = [
    "TASK_TYPES",
    "GRAPH_ONTOLOGY",
    "DECISION_LLM_SYSTEM",
    "DECISION_LLM_FEW_SHOT_USER",
    "DECISION_LLM_FEW_SHOT_ASSISTANT",
    "DECISION_LLM_JSON_SCHEMA",
    "DECISION_PATH_MERGED_SYSTEM",
    "DECISION_PATH_MERGED_FEW_SHOT_USER",
    "DECISION_PATH_MERGED_FEW_SHOT_ASSISTANT",
    "DECISION_PATH_MERGED_JSON_SCHEMA",
    "DECISION_REACT_EXTENSION",
    "DECISION_PATH_MERGED_REACT_SYSTEM",
    "DECISION_PATH_REACT_JSON_SCHEMA",
]

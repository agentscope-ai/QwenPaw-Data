"""L2 explore_entity synthesis. Implementation currently lives in
runtime/explore_synthesis.py (shared by eval scripts); this module re-exports it
as the cohesive home under the synthesis package.
"""
from __future__ import annotations

from ..explore_synthesis import *  # noqa: F401,F403
from ..explore_synthesis import (  # explicit re-export
    complete_json,
    neo4j_session,
    expand_2hop,
    pick_2hop_targets_llm,
    synthesize_entity_context_llm,
    synthesize_from_subgraph,
    apply_field_toggles,
    load_explore_synthesis_config,
    _placeholder_fields,
    _resolve_prompt,
    PROMPTS,
    PICKER_PROMPT,
    SYNTHESIS_JSON_SCHEMA,
    PICKER_JSON_SCHEMA,
    DEFAULT_CONFIG,
)

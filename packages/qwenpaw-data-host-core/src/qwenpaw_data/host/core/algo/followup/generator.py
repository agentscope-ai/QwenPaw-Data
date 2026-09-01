# -*- coding: utf-8 -*-
"""The model channel: render the snapshot into a prompt, read back candidates.

The template lives beside this module as Markdown so its wording and constraints
can be tuned without touching Python. A candidate that breaks the contract is
dropped on its own: one hallucinated entity must not cost the whole batch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from qwenpaw_data.host.core.algo.followup.llm import FollowUpLLM, FollowUpLLMError
from qwenpaw_data.host.core.algo.followup.models import (
    Candidate,
    EntityRecord,
    SignalSnapshot,
)
from qwenpaw_data.host.core.algo.followup.relevance import attribute_entities
from qwenpaw_data.host.core.algo.followup.skills import format_skill_index

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "FOLLOWUP_GENERATION.md"

_NONE = "无"


def render_prompt(
    snapshot: SignalSnapshot, template_path: Path = PROMPT_TEMPLATE_PATH
) -> str:
    """Fill the template from a frozen snapshot.

    Placeholders are substituted literally rather than through ``str.format``,
    because the template carries a JSON example full of braces.
    """

    # Datasets are deliberately absent: a physical table name in a suggestion
    # is meaningless to the user and leaks the warehouse layout.
    values = {
        "user_input": snapshot.user_input or _NONE,
        "final_answer_summary": snapshot.final_answer_summary or _NONE,
        "completed_nodes": "；".join(snapshot.completed_nodes) or _NONE,
        "skills_used": "、".join(snapshot.skills_used) or _NONE,
        "artifacts_summary": snapshot.artifacts_summary or _NONE,
        "anchor_metric": snapshot.anchor_metric or _NONE,
        "metrics": _entities(snapshot.metrics),
        "dimensions": _entities(snapshot.dimensions),
        "unused_dimensions": "、".join(snapshot.unused_dimensions) or _NONE,
        "skill_capability_index": format_skill_index(),
        "previous_followups": "；".join(snapshot.previous_followups) or _NONE,
        "intent_coverage": snapshot.intent_coverage or _NONE,
        "intent_gaps": "；".join(snapshot.intent_gaps) or _NONE,
        "intent_next_step": snapshot.intent_next_step or _NONE,
        "golden_query_note": (
            "本轮已命中已验证 SQL，勿在问题中写出 SQL"
            if snapshot.has_golden_query
            else _NONE
        ),
    }
    prompt = template_path.read_text(encoding="utf-8")
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", value)
    return prompt


def parse_candidates(
    payload: dict[str, Any], snapshot: SignalSnapshot
) -> list[Candidate]:
    """Validate the returned questions one by one, keeping what holds up.

    Entities are attributed here rather than taken from the model, and a
    malformed item is dropped alone: one bad question must not cost the batch,
    since the call has no second attempt inside the turn budget.
    """

    items = payload.get("questions")
    if not isinstance(items, list):
        logger.warning("Follow-up model output carries no questions list")
        return []

    candidates: list[Candidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        entities = attribute_entities(text, snapshot)
        intent = item.get("intent")
        # "把本次分析整理成一份报告" names no entity by nature, so a report
        # question inherits the anchor instead of looking ungrounded.
        if not entities and intent == "synthesis" and snapshot.anchor_metric:
            entities = [snapshot.anchor_metric]
        try:
            candidate = Candidate.model_validate(
                {
                    "text": text,
                    "intent_category": intent,
                    "target_entities": entities,
                    "source_channel": "llm",
                }
            )
        except ValidationError as exc:
            logger.warning("Dropping a malformed follow-up candidate: %s", exc)
            continue
        candidates.append(candidate)
    return candidates


async def generate_llm_candidates(
    snapshot: SignalSnapshot, *, llm: FollowUpLLM | None
) -> list[Candidate]:
    """Run the single model call, or return nothing when it cannot be made."""
    if llm is None:
        return []
    try:
        payload = await llm.complete(render_prompt(snapshot))
    except FollowUpLLMError as exc:
        logger.warning("Follow-up model call failed: %s", exc)
        return []
    except OSError as exc:
        logger.warning("Follow-up model is unreachable: %s", exc)
        return []
    return parse_candidates(payload, snapshot)


def _entities(entities: tuple[EntityRecord, ...]) -> str:
    """Name the entities, marking which ones the run actually worked on."""
    if not entities:
        return _NONE
    return "、".join(
        f"{entity.name}({'已分析' if entity.analyzed else '仅出现'})"
        for entity in entities
    )


__all__ = [
    "PROMPT_TEMPLATE_PATH",
    "generate_llm_candidates",
    "parse_candidates",
    "render_prompt",
]

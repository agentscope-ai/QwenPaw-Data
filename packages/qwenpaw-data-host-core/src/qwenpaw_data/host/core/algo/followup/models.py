# -*- coding: utf-8 -*-
"""Data models for follow-up recommendation: the signal snapshot and candidates."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

IntentCategory = Literal[
    "drilldown",
    "comparison",
    "attribution",
    "adjacent",
    "synthesis",
]
SourceChannel = Literal["rules", "llm"]

# How an entity was touched. This is the whole basis of relevance scoring: a
# name returned by a domain-wide listing means far less than one the run looked
# up by name or grouped a query by.
Provenance = Literal[
    "targeted",
    "metric_bound",
    "sql_groupby",
    "sql_where",
    "domain_dump",
]

QUESTION_MAX_CHARS = 60


class EntityRecord(BaseModel):
    """A metric, dimension or dataset this Chat touched.

    ``analyzed`` separates what the run worked on from what merely showed up in
    a lookup result: recommendations build on the former and mine the latter.
    ``relevance`` is how close the entity sits to the anchor metric.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    analyzed: bool = False
    relevance: float = 0.0


class SignalSnapshot(BaseModel):
    """Everything both channels get to see, frozen when the Chat ends.

    Collecting throughout the run and freezing once keeps the end of the turn
    free of queries: the prompt is rendered straight from these fields.

    ``metrics`` and ``dimensions`` are pruned by relevance and are the prompt's
    material, while ``business_entities`` keeps every metric and dimension the
    Chat touched, so a question can still be attributed to one that did not
    make the cut. Datasets stay out of both: a physical table name is internal
    plumbing and must never reach a suggestion.
    """

    model_config = ConfigDict(frozen=True)

    user_input: str = ""
    final_answer_summary: str = ""
    completed_nodes: tuple[str, ...] = ()
    skills_used: tuple[str, ...] = ()
    anchor_metric: str = ""
    metrics: tuple[EntityRecord, ...] = ()
    dimensions: tuple[EntityRecord, ...] = ()
    datasets: tuple[EntityRecord, ...] = ()
    unused_dimensions: tuple[str, ...] = ()
    business_entities: tuple[str, ...] = ()
    entity_aliases: tuple[tuple[str, str], ...] = ()
    artifacts_summary: str = ""
    previous_followups: tuple[str, ...] = ()
    intent_coverage: str = ""
    intent_gaps: tuple[str, ...] = ()
    intent_next_step: str = ""
    has_golden_query: bool = False

    def entity_names(self) -> frozenset[str]:
        """The real entities a question may be attributed to.

        Falls back to the pruned lists when nothing was recorded explicitly, so
        a hand-built snapshot stays usable.
        """

        if self.business_entities:
            return frozenset(self.business_entities)
        return frozenset(
            entity.name
            for group in (self.metrics, self.dimensions)
            for entity in group
        )

    def alias_map(self) -> dict[str, str]:
        """Alias to canonical name, for the second attribution path."""
        return dict(self.entity_aliases)

    def relevance_map(self) -> dict[str, float]:
        """Relevance by entity name, for scoring an attributed candidate."""
        return {
            entity.name: entity.relevance
            for group in (self.metrics, self.dimensions)
            for entity in group
        }


class Candidate(BaseModel):
    """One proposed follow-up question, before ranking picks the survivors.

    No skill is carried: the host re-routes the question through its own intent
    router when the user clicks it, so a skill picked here is never honoured.
    """

    text: str = Field(min_length=1, max_length=QUESTION_MAX_CHARS)
    intent_category: IntentCategory
    target_entities: list[str] = []
    source_channel: SourceChannel
    score: float = 0.0


class FollowUp(BaseModel):
    """Frontend contract: the keys ``OutputStream.followup_generated`` accepts.

    Intent and entities stay server-side; the protocol carries the question
    texts alone.
    """

    chat_id: str
    questions: list[str]

    @classmethod
    def of(cls, chat_id: str, candidates: list[Candidate]) -> FollowUp:
        return cls(
            chat_id=chat_id,
            questions=[candidate.text for candidate in candidates],
        )


__all__ = [
    "QUESTION_MAX_CHARS",
    "Candidate",
    "EntityRecord",
    "FollowUp",
    "IntentCategory",
    "Provenance",
    "SignalSnapshot",
    "SourceChannel",
]

# -*- coding: utf-8 -*-
"""Span-step, tracer, score, and tag names, used like log levels.
Values are byte-stable — Langfuse data depends on them, never rename.
"""

from __future__ import annotations

from enum import StrEnum


class Tracer(StrEnum):
    HOST = "qwenpaw_data.host"
    CONTEXT = "qwenpaw_data.context"


class Score(StrEnum):
    STOP = "stop"
    CHAT_STATUS = "chat_status"
    REACTION = "reaction"
    COPY = "copy"
    ARTIFACT_COMMENT = "artifact_comment"
    FOLLOWUP_ADOPTED = "followup_adopted"
    CLARIFICATION = "clarification"
    STEER = "steer"
    PLAN_EDIT = "plan_edit"
    TRACE_VIEW = "trace_view"
    SEGMENT_TOGGLE = "segment_toggle"
    SESSION_REVISIT = "session_revisit"
    CONFIDENCE = "confidence"
    SETTLEMENT_CARD = "settlement_card"
    SKILL_PROPOSAL = "skill_proposal"


class Tag(StrEnum):
    """Trace tags. Register a member before passing it to ``tracer(..., tags=)``."""

    CHAT = "chat"
    SETTLEMENT = "settlement"
    SKILL_EVOLUTION = "skill_evolution"
    BIZTRACE = "biztrace"


class Step(StrEnum):
    # host: chat 主 trace
    ARTIFACT = "artifact"
    STEER = "steer"
    # host: settlement 派生 trace
    SETTLEMENT = "settlement"
    SETTLEMENT_DETECT = "settlement.detect"
    SETTLEMENT_CONFIRM = "settlement.confirm"
    SETTLEMENT_DISMISSED_FILTER = "settlement.dismissed_filter"
    # host: skill evolution 派生 trace
    SKILL_EVOLUTION = "skill_evolution"
    # host: biztrace 派生 trace
    BIZTRACE = "biztrace"
    CONTINUITY_DECISION = "biztrace.continuity_decision"
    SEGMENT_KIND = "biztrace.segment_kind"
    SEGMENT_METADATA = "biztrace.segment_metadata"
    PRESENTATION_BODY = "biztrace.presentation_body"
    SQL_CALL = "biztrace.sql_call"
    SEMANTIC_CALL = "biztrace.semantic_call"
    FILE_WRITE_CALL = "biztrace.file_write_call"
    SHELL_CALL = "biztrace.shell_call"
    TOOL_PRESENTATION = "biztrace.tool_presentation"
    SEMANTIC_RESULT = "biztrace.semantic_result"
    FILE_WRITE_RESULT = "biztrace.file_write_result"
    SHELL_RESULT = "biztrace.shell_result"
    SQL_RESULT = "biztrace.sql_result"
    TOOL_OUTPUT_PRESENTATION = "biztrace.tool_output_presentation"
    # host: compare（API 请求内裸 generation，无 labels 不上 session）
    RECOMMEND = "compare_recommendations"
    # CM: 检索管线
    SEMANTIC_SPLIT = "semantic_split"
    ENTITY_EXPAND = "entity_expand"
    RERANK = "rerank"
    DECISION = "decision"
    PATH_HINT = "path_hint"
    JOURNEY_EXPLORE = "journey_explore"
    EMBEDDING = "embedding"
    EMBEDDING_WARMUP = "embedding_warmup"


__all__ = ["Score", "Step", "Tag", "Tracer"]

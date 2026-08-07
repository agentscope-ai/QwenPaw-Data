"""Pydantic request/response models for the TG management API."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ─── Task ────────────────────────────────────────────────────────────── #

class UpdateTaskStatusRequest(BaseModel):
    """Archive or invalidate a single task."""
    status: Literal["archived", "invalidated"]
    reason: str | None = None


class BatchTaskKeysRequest(BaseModel):
    """Batch-operate on up to 50 tasks by key."""
    task_keys: list[str] = Field(..., max_length=50)
    reason: str | None = None


# ─── Claim ───────────────────────────────────────────────────────────── #

class UpdateClaimFieldsRequest(BaseModel):
    """Partially update a claim's text, confidence, or SPO triple."""
    text: str | None = None
    confidence: float | None = Field(None, ge=0, le=1)
    subject_type: str | None = None
    predicate: str | None = None
    object: str | None = None


class InvalidateRequest(BaseModel):
    """Mark a claim as invalid with a mandatory reason."""
    reason: str


# ─── Strategy Card ───────────────────────────────────────────────────── #

class UpdateStrategyFieldsRequest(BaseModel):
    """Partially update a strategy card's semantics, tier, or polarity."""
    strategy_semantics: str | None = None
    memory_tier: Literal["hot", "warm", "cold"] | None = None
    source_trust: float | None = Field(None, ge=0, le=1)
    polarity: Literal["positive", "negative"] | None = None
    example_query: str | None = None

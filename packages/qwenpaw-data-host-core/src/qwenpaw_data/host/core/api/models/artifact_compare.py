# -*- coding: utf-8 -*-
"""Request/response models for the artifact compare endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

from qwenpaw_data.host.core.agent.tools.compare import DEFAULT_TOLERANCE


class CompareFilesRequest(BaseModel):
    """Body of the generic two-document compare endpoint.

    Both paths are artifact-dir relative, exactly like `/access`.
    """

    target_path: str
    reference_path: str
    tolerance: float = DEFAULT_TOLERANCE


class DiffFilesRequest(BaseModel):
    """Body of the generic line-by-line diff endpoint."""

    target_path: str
    reference_path: str


class CompareCandidate(BaseModel):
    """One selectable document offered to the recommendation model."""

    path: str
    name: str


class CompareRecommendationsRequest(BaseModel):
    """Body of the LLM compare-recommendation endpoint."""

    current_path: str | None = None
    candidates: list[CompareCandidate] = Field(default_factory=list)


class Recommendation(BaseModel):
    """One compare suggestion returned to the picker."""

    key: str
    label: str
    description: str | None = None
    reference_path: str
    target_path: str


class RecommendationList(BaseModel):
    """Structured-output schema handed to the model."""

    recommendations: list[Recommendation] = Field(default_factory=list)

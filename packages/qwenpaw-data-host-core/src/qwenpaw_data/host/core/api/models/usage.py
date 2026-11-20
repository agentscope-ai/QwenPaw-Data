# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Literal

from qwenpaw_data.host.core.api.models.common import ApiModel


class UsageWindowSchema(ApiModel):
    period: Literal["1d", "7d", "30d"]
    start: datetime
    end: datetime
    timezone: str
    as_of: datetime


class GenerationUsageSchema(ApiModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    calls: int


class EmbeddingUsageSchema(ApiModel):
    tokens: int
    calls: int


class UsageModelSchema(ApiModel):
    provider_id: str
    model_id: str
    model_type: Literal["text", "embedding"]


class UsagePaginationSchema(ApiModel):
    page: int
    page_size: int
    total: int
    page_count: int
    has_previous: bool
    has_next: bool


class UsageTotalsSchema(ApiModel):
    generation: GenerationUsageSchema
    embedding: EmbeddingUsageSchema
    total_tokens: int


class _UsageLineSchema(UsageTotalsSchema):
    last_activity_at: datetime
    models: list[UsageModelSchema]


class UsageTimeseriesPointSchema(ApiModel):
    bucket_start: datetime
    bucket_end: datetime
    label: str
    is_partial: bool
    generation: GenerationUsageSchema
    embedding: EmbeddingUsageSchema


class UsageSummarySchema(UsageTotalsSchema):
    window: UsageWindowSchema
    sessions: dict[str, int]
    background_tasks: dict[str, int]


class UsageTimeseriesSchema(ApiModel):
    window: UsageWindowSchema
    granularity: Literal["hour4", "day"]
    points: list[UsageTimeseriesPointSchema]
    totals: UsageTotalsSchema


class SessionUsageItemSchema(_UsageLineSchema):
    session_id: str
    title: str
    session_state: Literal["active", "deleted"]


class BackgroundTaskUsageItemSchema(_UsageLineSchema):
    group_id: str
    source: str


class SessionUsageTotalsSchema(UsageTotalsSchema):
    sessions: int


class BackgroundTaskUsageTotalsSchema(UsageTotalsSchema):
    groups: int


class SessionUsagePageSchema(ApiModel):
    window: UsageWindowSchema
    items: list[SessionUsageItemSchema]
    totals: SessionUsageTotalsSchema
    pagination: UsagePaginationSchema


class BackgroundTaskUsagePageSchema(ApiModel):
    window: UsageWindowSchema
    items: list[BackgroundTaskUsageItemSchema]
    totals: BackgroundTaskUsageTotalsSchema
    pagination: UsagePaginationSchema

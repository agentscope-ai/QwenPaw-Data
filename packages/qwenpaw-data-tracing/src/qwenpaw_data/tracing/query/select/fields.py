# -*- coding: utf-8 -*-
"""Trimming rows down to the chosen field combination."""

from __future__ import annotations

from typing import Any

from qwenpaw_data.tracing.query.backend.contract import SpanRow
from qwenpaw_data.tracing.query.models import (
    METADATA_FIELDS,
    SPAN_FIELDS,
    MetadataField,
    MetadataPart,
    SpanField,
    SpanPart,
)

# Langfuse / OTel attach these on ingest: flat "attributes.x" on v4, nested on v3.
_PLATFORM_KEYS = ("scope", "resourceAttributes", "attributes")


def span_parts(fields: SpanField) -> frozenset[SpanPart]:
    return SPAN_FIELDS[SpanField(fields)]


def metadata_parts(fields: MetadataField) -> frozenset[MetadataPart]:
    return METADATA_FIELDS[MetadataField(fields)]


def pick_span(row: SpanRow, parts: frozenset[SpanPart]) -> dict[str, Any]:
    return {part.value: row[part.value] if part in parts else None for part in SpanPart}


def _metadata_part(key: str) -> MetadataPart:
    if any(key == p or key.startswith(f"{p}.") for p in _PLATFORM_KEYS):
        return MetadataPart.PLATFORM
    return MetadataPart.BUSINESS


def pick_metadata(
    metadata: dict[str, Any], parts: frozenset[MetadataPart]
) -> dict[str, Any]:
    return {k: v for k, v in metadata.items() if _metadata_part(k) in parts}

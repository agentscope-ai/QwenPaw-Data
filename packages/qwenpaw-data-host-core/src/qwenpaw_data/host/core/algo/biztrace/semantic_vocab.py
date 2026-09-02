# -*- coding: utf-8 -*-
"""Semantic-config vocabulary used to link business entities in card bodies.

Four Context Manager endpoints supply the terms: business domains, metrics,
dimensions and datasets. They are pulled page by page into an in-memory index
and refreshed in the background, so linking a card never touches the network.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BIZ_DOMAIN_PATH = "/api/semantic-config/biz-domain"
DIMENSION_PATH = "/api/semantic-config/dimension"
METRIC_PATH = "/api/semantic-config/metric-lib"
DATASET_PATH = "/api/semantic-config/dataset-meta"

DEFAULT_PAGE_SIZE = 200
MAX_PAGES = 100
DEFAULT_TIMEOUT_SECONDS = 10.0

EntityType = str


@dataclass(frozen=True)
class VocabEntry:
    """One linkable term and the record it points at."""

    entity_type: EntityType
    domain_id: str | None
    entity_id: str | None


@dataclass(frozen=True)
class VocabSource:
    """How one endpoint's records map onto vocabulary entries."""

    entity_type: EntityType
    path: str
    name_field: str
    id_field: str | None
    synonym_field: str | None = None


VOCAB_SOURCES: tuple[VocabSource, ...] = (
    # Business domains match on their canonical name only: display_name and
    # aliases are editorial labels and would over-link.
    VocabSource(entity_type="biz_domain", path=BIZ_DOMAIN_PATH,
                name_field="domain_name", id_field=None),
    VocabSource(entity_type="metric", path=METRIC_PATH,
                name_field="metric_name", id_field="metric_id",
                synonym_field="synonyms"),
    # /dimension rather than /dataset-dimension: only this one carries synonyms.
    VocabSource(entity_type="dimension", path=DIMENSION_PATH,
                name_field="dimension_name", id_field="dimension_id",
                synonym_field="synonyms"),
    VocabSource(entity_type="dataset", path=DATASET_PATH,
                name_field="dataset_name", id_field="dataset_id"),
)


def split_terms(value: Any) -> list[str]:
    """Split a comma- or pipe-separated synonym field, as the graph loader does."""
    if not value:
        return []
    if isinstance(value, str):
        if "," in value or "|" in value:
            return [
                part.strip()
                for part in value.replace("|", ",").split(",")
                if part.strip()
            ]
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


class SemanticVocabulary:
    """In-memory term index with TTL refresh and silent degradation."""

    def __init__(
        self,
        *,
        base_url: str,
        datasource_id: str | None,
        access_token: str | None = None,
        ttl: float = 300.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.datasource_id = datasource_id
        self.access_token = access_token
        self.ttl = ttl
        self.page_size = page_size
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._terms: dict[str, VocabEntry] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def terms(self) -> dict[str, VocabEntry]:
        return self._terms

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ensure_fresh(self) -> None:
        """Reload the vocabulary when the TTL has elapsed; never raises."""
        if self._terms and monotonic() - self._loaded_at < self.ttl:
            return
        async with self._lock:
            if self._terms and monotonic() - self._loaded_at < self.ttl:
                return
            terms = await self._load()
            if terms is None:
                # Keep serving the previous snapshot rather than un-linking.
                return
            self._terms = terms
            self._loaded_at = monotonic()

    async def _load(self) -> dict[str, VocabEntry] | None:
        collected: dict[str, list[VocabEntry]] = {}
        loaded_any = False
        for source in VOCAB_SOURCES:
            records = await self._fetch(source)
            if records is None:
                continue
            loaded_any = True
            for record in records:
                self._collect(collected, source, record)
        if not loaded_any:
            return None
        # A term claimed by more than one record is ambiguous, within a type or
        # across types, and is dropped rather than linked to an arbitrary one.
        return {
            term: entries[0]
            for term, entries in collected.items()
            if len(entries) == 1
        }

    def _collect(
        self,
        collected: dict[str, list[VocabEntry]],
        source: VocabSource,
        record: dict[str, Any],
    ) -> None:
        # v2.1: only datasets stay datasource-scoped. Domain / dimension /
        # metric rows no longer carry datasource_id; filtering them would
        # silently drop every term in a bound session.
        if (
            source.path == DATASET_PATH
            and self.datasource_id
            and record.get("datasource_id") != self.datasource_id
        ):
            return
        name = record.get(source.name_field)
        if not isinstance(name, str) or not name.strip():
            return
        entry = VocabEntry(
            entity_type=source.entity_type,
            domain_id=_as_id(record.get("domain_id")),
            entity_id=(
                _as_id(record.get(source.id_field)) if source.id_field else None
            ),
        )
        terms = [name.strip()]
        if source.synonym_field:
            terms.extend(split_terms(record.get(source.synonym_field)))
        for term in terms:
            if term:
                collected.setdefault(term, []).append(entry)

    async def _fetch(self, source: VocabSource) -> list[dict[str, Any]] | None:
        records: list[dict[str, Any]] = []
        params: dict[str, Any] = {"size": self.page_size}
        if source.path == DATASET_PATH and self.datasource_id:
            params["datasource_id"] = self.datasource_id
        try:
            for page in range(1, MAX_PAGES + 1):
                payload = await self._get(source.path, {**params, "page": page})
                if isinstance(payload, list):
                    return [item for item in payload if isinstance(item, dict)]
                page_records = payload.get("records")
                if not isinstance(page_records, list):
                    return records
                records.extend(
                    item for item in page_records if isinstance(item, dict)
                )
                total = payload.get("total")
                if not page_records or not isinstance(total, int):
                    return records
                if len(records) >= total:
                    return records
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("semantic vocabulary %s unavailable: %s", source.path, exc)
            return None
        return records

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        headers: dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        response = await self._client.get(
            f"{self.base_url}{path}", params=params, headers=headers
        )
        if response.status_code in (401, 403):
            raise httpx.HTTPError(
                f"HTTP {response.status_code} (auth rejected for {path})"
            )
        if not response.is_success:
            raise httpx.HTTPError(f"HTTP {response.status_code}")
        return response.json()


def _as_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "BIZ_DOMAIN_PATH",
    "DATASET_PATH",
    "DIMENSION_PATH",
    "METRIC_PATH",
    "SemanticVocabulary",
    "VOCAB_SOURCES",
    "VocabEntry",
    "VocabSource",
    "split_terms",
]

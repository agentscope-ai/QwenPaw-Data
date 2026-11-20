"""Serve develop's four Metadata HTTP contracts consumed by Host settlement."""

import json
import logging
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from ...auth import GLOBAL_OWNER_ID
from ...compatibility.metadata import MetadataHttpReader
from ...runtime import ContextRuntimeUnavailable
from ...schema import Zone
from ...store import StoreError
from ..dependencies import AuthorizationDependency, RuntimeDependency

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/cm", tags=["legacy-metadata"])


@contextmanager
def _reader(
    runtime,
    context,
    datasource_id,
    dataset_name="",
    include_identifiers=False,
):
    """Authorize MG ownership; pass datasource selection to the semantic reader."""
    try:
        scope = runtime.issue_graph_read_scope(
            context,
            zone=Zone.METADATA.value,
            owner_id=GLOBAL_OWNER_ID,
        )
        yield MetadataHttpReader(
            runtime.graph_store(scope),
            scope,
            datasource_id=datasource_id or "",
            dataset_name=dataset_name or "",
            include_identifiers=include_identifiers,
        )
    except ContextRuntimeUnavailable as exc:
        raise HTTPException(503, "Context runtime is unavailable") from exc
    except PermissionError as exc:
        raise HTTPException(403, "Metadata read is forbidden") from exc
    except StoreError as exc:
        raise HTTPException(500, "Metadata store query failed") from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _headers(response, reader, *, dataset=True):
    response.headers["Deprecation"] = "true"
    if dataset:
        note = reader.dataset_scope_note()
        if note:
            response.headers["X-CM-Dataset-Scope"] = json.dumps(note, ensure_ascii=True)


async def _query_embedding(runtime, context, query):
    """Share HTTP/MCP query embedding and develop's text-only fallback."""
    try:
        models = await runtime.run_graph_read(runtime.models, context)
        async with models.embedder() as embedder:
            return (await embedder.embed((query,)))[0]
    except BlockingIOError:
        raise
    except Exception:  # noqa: BLE001 - develop retains text retrieval on provider failure.
        log.warning("Metric query embedding unavailable; using fulltext only")
        return None


@router.get("/domains")
async def list_domains(
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    datasource_id: str | None = None,
) -> list[dict]:
    def read():
        with _reader(runtime, context, datasource_id) as reader:
            result = reader.list_domains()
            _headers(response, reader, dataset=False)
            return result

    try:
        return await runtime.run_graph_read(read)
    except BlockingIOError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/search-metrics")
async def search_metrics(
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    query: str,
    domain: str | None = None,
    k: Annotated[int, Query(ge=1, le=50)] = 10,
    datasource_id: str | None = None,
    dataset_name: str | None = None,
    relevance_threshold: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
) -> list[dict] | dict:
    def read(embedding):
        with _reader(runtime, context, datasource_id, dataset_name) as reader:
            domain_id = reader.domain(domain)
            if not query.strip():
                raise HTTPException(422, "query is required")
            result = reader.search_metrics(
                domain_id,
                query.strip(),
                k,
                embedding=embedding,
                relevance_threshold=relevance_threshold,
            )
            _headers(response, reader)
            return result

    try:
        embedding = (
            await _query_embedding(runtime, context, query.strip())
            if query.strip()
            else None
        )
        return await runtime.run_graph_read(read, embedding)
    except BlockingIOError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/dimensions")
async def dimensions(
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    domain: str | None = None,
    name: str | None = None,
    datasource_id: str | None = None,
    dataset_name: str | None = None,
) -> list[dict] | dict:
    def read():
        with _reader(runtime, context, datasource_id, dataset_name) as reader:
            domain_id = reader.domain(domain)
            result = reader.dimensions(domain_id, name)
            _headers(response, reader)
            return result

    try:
        return await runtime.run_graph_read(read)
    except BlockingIOError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/datasets")
async def datasets(
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    domain: str | None = None,
    name: str | None = None,
    datasource_id: str | None = None,
    dataset_name: str | None = None,
) -> list[dict] | dict:
    def read():
        # As in develop, an explicit name overrides the optional dataset anchor.
        explicit_name = bool((name or "").strip())
        with _reader(
            runtime,
            context,
            datasource_id,
            "" if explicit_name else dataset_name,
        ) as reader:
            domain_id = reader.domain(domain)
            result = reader.datasets(domain_id, name)
            _headers(response, reader, dataset=not explicit_name)
            return result

    try:
        return await runtime.run_graph_read(read)
    except BlockingIOError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/datasets/{name}/columns")
async def dataset_columns(
    name: str,
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    domain: str | None = None,
    datasource_id: str | None = None,
) -> list[dict] | dict:
    """Retain the dataset-column contract consumed by the legacy MCP tool."""

    def read():
        with _reader(runtime, context, datasource_id) as reader:
            domain_id = reader.domain(domain)
            result = reader.datasets(domain_id, name)
            _headers(response, reader, dataset=False)
            return result.get("columns", result) if isinstance(result, dict) else result

    try:
        return await runtime.run_graph_read(read)
    except BlockingIOError as exc:
        raise HTTPException(503, str(exc)) from exc

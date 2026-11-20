"""Expose the authenticated KG document catalog and import actions."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from urllib.parse import quote

from qwenpaw_data_auth.authorization import AuthorizationContext
from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from ....auth import WriteOwner
from ....knowledge import (
    KnowledgeImportError,
    KnowledgeImportErrorCode,
    UploadedDocument,
)
from ....knowledge.document import (
    Document,
    DocumentContentMismatchError,
    DocumentStatus,
)
from ....knowledge.repository import DocumentLeaseLost
from ....runtime import ContextRuntimeUnavailable
from ....store import StoreError
from ...dependencies import (
    AuthorizationDependency,
    RuntimeDependency,
)

_T = TypeVar("_T")
IngestStatus = Literal["building", "ready", "failed"]
_INGEST_STATUS: dict[DocumentStatus, IngestStatus] = {
    DocumentStatus.UPLOADED: "building",
    DocumentStatus.PROCESSING: "building",
    DocumentStatus.DELETING: "building",
    DocumentStatus.READY: "ready",
    DocumentStatus.DEGRADED: "ready",
    DocumentStatus.FAILED: "failed",
}


class DocumentView(BaseModel):
    doc_id: str
    filename: str
    file_size: int
    download_url: str
    ingest_status: IngestStatus
    last_modified: datetime
    ingest_error: str | None
    owner_id: str
    owner_name: str
    can_delete: bool
    retry_action: Literal["reingest", "delete"] | None


class DocumentPage(BaseModel):
    list: list[DocumentView]
    page: int
    page_size: int
    total: int
    status_counts: dict[IngestStatus, int]


class DeletedDocument(BaseModel):
    doc_id: str


class DocumentEnvelope(BaseModel, Generic[_T]):
    code: Literal[0] = 0
    message: Literal["success"] = "success"
    data: _T


router = APIRouter(prefix="/api/v1/docs", tags=["knowledge-documents"])


@router.get("", response_model=DocumentEnvelope[DocumentPage])
async def list_documents(
    request: Request,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: IngestStatus | None = None,
    keyword: Annotated[str, Query(max_length=500)] = "",
):
    """List this user's documents with tab counts taken before filtering."""
    try:
        documents = runtime.knowledge_documents(context)
        items = await documents.list()
        counts = {
            value: sum(_ingest_status(item) == value for item in items)
            for value in ("building", "ready", "failed")
        }
        needle = keyword.strip().casefold()
        items = [
            item
            for item in items
            if needle in item.metadata.filename.casefold()
            and (status is None or _ingest_status(item) == status)
        ]
        start = (page - 1) * page_size
        page_items = items[start : start + page_size]
        owner_name = await documents.owner_name() if page_items else context.user_id
    except (
        BlockingIOError,
        ContextRuntimeUnavailable,
        SQLAlchemyError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        raise _http_error(exc) from exc
    return {
        "data": {
            "list": [_document_view(item, context, owner_name) for item in page_items],
            "page": page,
            "page_size": page_size,
            "total": len(items),
            "status_counts": counts,
        }
    }


@router.post("/upload", response_model=DocumentEnvelope[DocumentView])
async def upload_document(
    request: Request,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    file: Annotated[UploadFile, File()],
):
    """Accept a supported original and return building while import runs."""
    try:
        scope = runtime.issue_graph_write_scope(
            context,
            owner=WriteOwner.USER,
            zone="knowledge",
        )
        importer = runtime.knowledge_importer(context)
        max_bytes = importer.document_limits.max_bytes
        content = await file.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise HTTPException(413, "file_too_large")
        documents = runtime.knowledge_documents(context)
        owner_name = await documents.owner_name()
        document = await importer.start_import(
            UploadedDocument(file.filename or "", file.content_type, content),
            context,
            scope,
        )
        return {"data": _document_view(document, context, owner_name)}
    except (
        BlockingIOError,
        ContextRuntimeUnavailable,
        KnowledgeImportError,
        DocumentLeaseLost,
        SQLAlchemyError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        raise _http_error(exc) from exc
    finally:
        await file.close()


@router.post("/reingest", response_model=DocumentEnvelope[DocumentView])
async def reingest_document(
    doc_id: str,
    request: Request,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    """Reimport this user's retained original using a fresh model snapshot."""
    try:
        scope = runtime.issue_graph_write_scope(
            context,
            owner=WriteOwner.USER,
            zone="knowledge",
        )
        importer = runtime.knowledge_importer(context)
        documents = runtime.knowledge_documents(context)
        owner_name = await documents.owner_name()
        document = await importer.start_reingest(doc_id, context, scope)
        return {"data": _document_view(document, context, owner_name)}
    except (
        BlockingIOError,
        ContextRuntimeUnavailable,
        KnowledgeImportError,
        DocumentLeaseLost,
        SQLAlchemyError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        raise _http_error(exc) from exc


@router.get("/{doc_id}/download", response_class=Response)
async def download_document(
    doc_id: str,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    """Return original bytes for downloads and client-side PDF/DOCX previews."""
    try:
        documents = runtime.knowledge_documents(context)
        document, content = await documents.download(doc_id)
    except (
        BlockingIOError,
        ContextRuntimeUnavailable,
        SQLAlchemyError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        raise _http_error(exc) from exc
    return Response(
        content,
        media_type=document.metadata.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document.metadata.filename, safe='')}",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete("/{doc_id}", response_model=DocumentEnvelope[DeletedDocument])
async def delete_document(
    doc_id: str,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    """Retract this user's graph sources before deleting the original document."""
    try:
        scope = runtime.issue_graph_write_scope(context, owner=WriteOwner.USER)
        await runtime.knowledge_deleter(context).delete_document(doc_id, scope)
    except (
        BlockingIOError,
        ContextRuntimeUnavailable,
        StoreError,
        DocumentLeaseLost,
        SQLAlchemyError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        raise _http_error(exc) from exc
    return {"data": {"doc_id": doc_id}}


def _ingest_status(document: Document) -> IngestStatus:
    """Expose interrupted deletion as a failure while retaining its retry state."""
    if (
        document.metadata.status is DocumentStatus.DELETING
        and document.metadata.ingest_error
    ):
        return "failed"
    return _INGEST_STATUS[document.metadata.status]


def _document_view(
    document: Document,
    context: AuthorizationContext,
    owner_name: str,
) -> DocumentView:
    metadata = document.metadata
    status = _ingest_status(document)
    return DocumentView(
        doc_id=document.document_id,
        filename=metadata.filename,
        file_size=metadata.byte_size,
        download_url=f"/api/v1/docs/{quote(document.document_id, safe='')}/download",
        ingest_status=status,
        last_modified=metadata.updated_at,
        ingest_error=metadata.ingest_error,
        owner_id=context.user_id,
        owner_name=owner_name,
        can_delete=metadata.status is not DocumentStatus.PROCESSING,
        retry_action=(
            ("delete" if metadata.status is DocumentStatus.DELETING else "reingest")
            if status == "failed"
            else None
        ),
    )


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DocumentLeaseLost):
        return HTTPException(409, "doc_execution_lease_lost")
    if isinstance(exc, PermissionError):
        return HTTPException(403, "document access is forbidden")
    if isinstance(exc, BlockingIOError):
        return HTTPException(503, "knowledge execution is busy")
    if isinstance(exc, ContextRuntimeUnavailable):
        return HTTPException(503, "context runtime is unavailable")
    if isinstance(exc, StoreError):
        return HTTPException(
            503 if exc.retryable else 500, "knowledge graph deletion failed"
        )
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, "doc_not_found")
    if isinstance(exc, DocumentContentMismatchError):
        return HTTPException(409, "doc_content_mismatch")
    if isinstance(exc, FileExistsError):
        if str(exc) in {
            "doc_build_in_progress",
            "doc_delete_in_progress",
            "doc_operation_in_progress",
        }:
            return HTTPException(409, str(exc))
        return HTTPException(409, "doc_already_exists")
    if isinstance(exc, (OSError, SQLAlchemyError)):
        return HTTPException(500, "document storage is unavailable")
    if isinstance(exc, KnowledgeImportError):
        response_status = {
            KnowledgeImportErrorCode.UNSUPPORTED_DOCUMENT: 415,
            KnowledgeImportErrorCode.DOCUMENT_TOO_LARGE: 413,
            KnowledgeImportErrorCode.DOCUMENT_INVALID: 422,
            KnowledgeImportErrorCode.EXTRACTION_INVALID: 422,
            KnowledgeImportErrorCode.EXTRACTION_FAILED: 502,
            KnowledgeImportErrorCode.CANDIDATE_LIMIT: 409,
            KnowledgeImportErrorCode.WRITE_FAILED: 503,
            KnowledgeImportErrorCode.UNAVAILABLE: 503,
        }[exc.code]
        message = {
            KnowledgeImportErrorCode.UNSUPPORTED_DOCUMENT: "unsupported_file_type",
            KnowledgeImportErrorCode.DOCUMENT_TOO_LARGE: "file_too_large",
        }.get(exc.code, str(exc))
        return HTTPException(response_status, message)
    return HTTPException(422, "invalid document request")


__all__ = ["router"]

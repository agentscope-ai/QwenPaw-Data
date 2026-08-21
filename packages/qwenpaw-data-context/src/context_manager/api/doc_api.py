"""KG document management API (MVP): upload, list, download, delete."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from qwenpaw_data.context.blocking_io import BlockingIOError, BlockingPool
from qwenpaw_data.context.uploads import read_upload

from ..kg_docs.exceptions import DocAlreadyExistsError, DocNotFoundError
from ..kg_docs.ingest_error import user_facing_ingest_error
from ..kg_docs.ingest_status import IngestStatusStore, get_ingest_status_store
from ..kg_docs.local_doc_client import LocalDocClient, get_local_doc_client
from ..knowledge.kg_doc_ops import build_kg_from_bytes, delete_kg_nodes_by_source
from ..utils import get_logger

log = get_logger("api.docs")

router = APIRouter(prefix="/api/v1/docs", tags=["docs"])

ALLOWED_EXTENSIONS = {".txt", ".docx", ".pdf", ".md"}
CONTENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _ok(data: Any) -> JSONResponse:
    return JSONResponse({"code": 0, "message": "success", "data": data})


def _err(http_status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"code": code, "message": message, "data": None},
    )


def _extension(filename: str | None) -> str:
    if not filename:
        return ""
    return Path(filename).suffix.lower()


@router.post("/upload")
async def upload_doc(request: Request, file: UploadFile = File(...)) -> JSONResponse:
    ext = _extension(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        return _err(400, 40002, "unsupported_file_type")

    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
    client = get_local_doc_client()
    # 读时即按 DOC_MAX_SIZE 截断，避免超限文件先整读进内存再被拒
    content = await read_upload(file, max_bytes=client.max_size)
    status_store = get_ingest_status_store()
    app_state = getattr(getattr(request, "app", None), "state", None)
    driver = getattr(app_state, "driver", None) if app_state is not None else None

    def _upload_and_begin():
        doc = client.upload(file.filename or "", content, content_type)
        try:
            build_token = status_store.begin(doc.doc_id)
        except Exception:
            try:
                client.delete(doc.doc_id)
            except Exception:
                log.exception("failed to roll back uploaded doc %r", doc.doc_id)
            raise
        return doc, build_token

    try:
        doc, build_token = await request.app.state.blocking_io.run(
            BlockingPool.FILE,
            "documents.upload_and_begin",
            _upload_and_begin,
        )
    except BlockingIOError:
        raise
    except DocAlreadyExistsError:
        return _err(409, 40901, "doc_already_exists")
    except ValueError as exc:
        if "file exceeds max size" in str(exc):
            return _err(400, 40003, "file_exceeds_size")
        return _err(400, 40001, "invalid_param")
    except Exception:
        log.exception("doc upload failed")
        return _err(500, 50001, "server_error")

    download_url = str(request.url_for("download_doc", doc_id=doc.doc_id))
    ingest_status = "building"

    if driver is not None:
        request.app.state.blocking_io.submit(
            BlockingPool.GRAPH,
            "documents.ingest",
            _run_kg_ingest_sync,
            client,
            status_store,
            driver,
            doc.doc_id,
            doc.filename,
            content,
            build_token,
        )
        log.info("KG ingest task scheduled for %r", doc.filename)
    else:
        await request.app.state.blocking_io.run(
            BlockingPool.FILE,
            "documents.status.unavailable",
            status_store.mark_failed,
            doc.doc_id,
            "Knowledge graph service unavailable",
        )
        ingest_status = "failed"
        log.warning("upload_doc: Neo4j driver not available, skipping KG ingest")

    return _ok(
        {
            "doc_id": doc.doc_id,
            "filename": doc.filename,
            "file_size": doc.file_size,
            "download_url": download_url,
            "ingest_status": ingest_status,
        }
    )


def _run_kg_ingest_sync(
    client: LocalDocClient,
    status_store: IngestStatusStore,
    driver: Any,
    doc_id: str,
    filename: str,
    content: bytes,
    build_token: str,
) -> None:
    """Blocking KG ingest — intended to run in a thread pool."""
    try:
        build_kg_from_bytes(driver, filename, content)
    except Exception as exc:
        if not client.file_exists(doc_id):
            log.info("Skipped KG ingest failure status for %r (document removed)", doc_id)
        else:
            updated = status_store.finalize(
                doc_id,
                build_token,
                "failed",
                user_facing_ingest_error(exc),
            )
            if not updated:
                log.info(
                    "Skipped KG ingest failure status for %r (document removed or superseded)",
                    doc_id,
                )
        log.exception("KG ingest failed for %r", filename)
    else:
        if not client.file_exists(doc_id):
            log.info("Skipped KG ingest success status for %r (document removed)", doc_id)
            return
        updated = status_store.finalize(doc_id, build_token, "ready")
        if not updated:
            log.info(
                "Skipped KG ingest success status for %r (document removed or superseded)",
                doc_id,
            )
            return
        log.info("KG ingest completed for %r", filename)


def _run_kg_delete_sync(driver: Any, filename: str) -> None:
    """Blocking KG node deletion — intended to run in a thread pool."""
    try:
        result = delete_kg_nodes_by_source(driver, filename)
        log.info("KG delete completed for %r: %s", filename, result)
    except Exception:
        log.exception("KG delete failed for %r", filename)


@router.get("")
async def list_docs(
    request: Request,
    page: int = Query(1),
    page_size: int = Query(20),
) -> JSONResponse:
    client = get_local_doc_client()
    status_store = get_ingest_status_store()
    try:
        result = await request.app.state.blocking_io.run(
            BlockingPool.FILE,
            "documents.list",
            client.list_docs,
            page,
            page_size,
        )
    except BlockingIOError:
        raise
    except ValueError:
        return _err(400, 40001, "invalid_param")
    except Exception:
        log.exception("doc list failed")
        return _err(500, 50001, "server_error")

    statuses = await request.app.state.blocking_io.run(
        BlockingPool.FILE,
        "documents.status.list",
        lambda: {
            item.doc_id: status_store.resolve(item.doc_id)
            for item in result.items
        },
    )
    items = []
    for item in result.items:
        ingest_status, ingest_error = statuses[item.doc_id]
        items.append(
            {
                "doc_id": item.doc_id,
                "filename": item.filename,
                "file_size": item.file_size,
                "download_url": str(request.url_for("download_doc", doc_id=item.doc_id)),
                "ingest_status": ingest_status,
                "ingest_error": ingest_error,
            }
        )

    return _ok(
        {
            "list": items,
            "page": result.page,
            "page_size": result.page_size,
            "total": result.total,
        }
    )


@router.get("/{doc_id:path}/download", response_model=None)
async def download_doc(request: Request, doc_id: str) -> FileResponse | JSONResponse:
    client = get_local_doc_client()
    try:
        path = await request.app.state.blocking_io.run(
            BlockingPool.FILE,
            "documents.download",
            client.get_file_path,
            doc_id,
        )
    except BlockingIOError:
        raise
    except DocNotFoundError:
        return _err(404, 40401, "doc_not_found")
    except ValueError:
        return _err(400, 40001, "invalid_param")
    except Exception:
        log.exception("doc download failed for %r", doc_id)
        return _err(500, 50001, "server_error")

    ext = Path(doc_id).suffix.lower()
    media_type = CONTENT_TYPES.get(ext, "application/octet-stream")
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=Path(doc_id).name,
    )


@router.delete("/{doc_id:path}")
async def delete_doc(request: Request, doc_id: str) -> JSONResponse:
    client = get_local_doc_client()
    status_store = get_ingest_status_store()

    def _delete_and_remove_status() -> str:
        deleted = client.delete(doc_id)
        status_store.remove(deleted)
        return deleted

    try:
        deleted = await request.app.state.blocking_io.run(
            BlockingPool.FILE,
            "documents.delete",
            _delete_and_remove_status,
        )
    except BlockingIOError:
        raise
    except DocNotFoundError:
        return _err(404, 40401, "doc_not_found")
    except ValueError:
        return _err(400, 40001, "invalid_param")
    except Exception:
        log.exception("doc delete failed")
        return _err(500, 50001, "server_error")

    driver = getattr(getattr(request, "app", None), "state", None)
    driver = getattr(driver, "driver", None) if driver is not None else None
    if driver is not None:
        _filename = Path(doc_id).name
        request.app.state.blocking_io.submit(
            BlockingPool.GRAPH,
            "documents.graph_delete",
            _run_kg_delete_sync,
            driver,
            _filename,
        )
        log.info("KG delete task scheduled for %r", _filename)
    else:
        log.warning("delete_doc: Neo4j driver not available, skipping KG cleanup")

    return _ok({"doc_id": deleted})

# -*- coding: utf-8 -*-
"""HTTP endpoints for comparing two artifacts in a session workspace."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.agent.tools.compare import compare_documents, compare_texts
from qwenpaw_data.host.core.algo.compare.family import compare_family
from qwenpaw_data.host.core.algo.compare.recommend import recommend_compare_pairs
from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.models.artifact_compare import (
    CompareFilesRequest,
    CompareRecommendationsRequest,
    DiffFilesRequest,
)
from qwenpaw_data.host.core.api.routers.artifact import resolve_session_file
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.html_text import html_to_markdown, html_comparison_text
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(prefix="/sessions/{session_id}/files", tags=["files"])

_LOGGER = logging.getLogger("qwenpaw_data.artifact")


def _read_compare_text(file_path: Path) -> str:
    """Read one compared document as utf-8 text, mapping IO errors to HTTP."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise_api("VALIDATION", "compared file is not utf-8 text", status=400)
    except OSError:
        raise_api("NOT_FOUND", "workspace file not found", status=404)
    if file_path.suffix.lower() in {".html", ".htm"}:
        return html_to_markdown(text)
    return text


def _compare_file_payload(
    reference: Path, target: Path, tolerance: float
) -> dict[str, object]:
    source_map = None
    if target.suffix.lower() in {".html", ".htm"}:
        try:
            target_text, source_map = html_comparison_text(
                target.read_text(encoding="utf-8")
            )
        except UnicodeDecodeError:
            raise_api("VALIDATION", "compared file is not utf-8 text", status=400)
        except OSError:
            raise_api("NOT_FOUND", "workspace file not found", status=404)
    else:
        target_text = _read_compare_text(target)
    result = compare_documents(
        _read_compare_text(reference), target_text, tolerance=tolerance
    )
    payload = result.model_dump(mode="json")
    if source_map is not None:
        spans = []
        for index, row in enumerate(result.numeric_differences):
            if row.matched or row.target_start is None or row.target_end is None:
                continue
            mapped = source_map[row.target_start : row.target_end]
            if not mapped or any(span is None for span in mapped):
                continue
            # A number split across HTML nodes cannot be wrapped with one mark.
            if any(
                left[1] != right[0] and left != right
                for left, right in zip(mapped, mapped[1:])
            ):
                continue
            spans.append(
                {"id": f"diff-{index}", "start": mapped[0][0], "end": mapped[-1][1]}
            )
        payload["target_html_spans"] = spans
    return payload


@router.post("/compare")
async def compare_workspace_files(
    session_id: str,
    body: CompareFilesRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    try:
        session = await access_session(db, identity, session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise

    if not 0 <= body.tolerance <= 1:
        raise_api("VALIDATION", "tolerance must be within 0..1", status=400)

    paths = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=session.identity,
        agent_id=session.agent_id,
        session_id=session.id,
    )
    target = resolve_session_file(paths.artifact_dir, body.target_path)
    reference = resolve_session_file(paths.artifact_dir, body.reference_path)
    # Parsing large HTML and numeric alignment must not block file reads, auth,
    # or streaming requests on the API event loop.
    payload = await asyncio.to_thread(
        _compare_file_payload, reference, target, body.tolerance
    )
    payload["target_path"] = body.target_path
    payload["reference_path"] = body.reference_path
    return payload


@router.post("/diff")
async def diff_workspace_files(
    session_id: str,
    body: DiffFilesRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    try:
        session = await access_session(db, identity, session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise

    target_family = compare_family(body.target_path)
    reference_family = compare_family(body.reference_path)
    if target_family is None or target_family != reference_family:
        raise_api(
            "VALIDATION",
            "only two files of the same comparable type can be diffed",
            status=400,
        )

    paths = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=session.identity,
        agent_id=session.agent_id,
        session_id=session.id,
    )
    target = resolve_session_file(paths.artifact_dir, body.target_path)
    reference = resolve_session_file(paths.artifact_dir, body.reference_path)
    result = compare_texts(
        _read_compare_text(reference),
        _read_compare_text(target),
    )
    payload = result.model_dump(mode="json")
    payload["target_path"] = body.target_path
    payload["reference_path"] = body.reference_path
    return payload


# Recommendations are advisory and change only when the candidate set changes,
# so an in-process cache spares the picker a model round-trip every time it is
# reopened for the same document. Keyed per session so one user's suggestions
# are never served to another. Successful results live for the full TTL; empty
# results caused by a model failure are held only for a short negative window so
# the picker stops hammering a failing model yet retries on its own soon after.
_RecommendCacheKey = tuple[str, str | None, tuple[tuple[str, str], ...]]
# Each entry stores its absolute expiry (time.monotonic() based) alongside the
# value, so success and negative entries can carry different lifetimes. The TTLs
# and the max entry count come from Host settings (CompareSettings).
_RECOMMEND_CACHE: dict[_RecommendCacheKey, tuple[float, list[dict[str, Any]]]] = {}


def _recommend_cache_key(
    session_id: str, body: CompareRecommendationsRequest
) -> _RecommendCacheKey:
    return (
        session_id,
        body.current_path,
        tuple(sorted((c.path, c.name) for c in body.candidates)),
    )


def _recommend_cache_get(key: _RecommendCacheKey) -> list[dict[str, Any]] | None:
    entry = _RECOMMEND_CACHE.get(key)
    if entry is None:
        return None
    expiry, value = entry
    if time.monotonic() >= expiry:
        _RECOMMEND_CACHE.pop(key, None)
        return None
    return value


def _recommend_cache_set(
    key: _RecommendCacheKey,
    value: list[dict[str, Any]],
    ttl: float,
) -> None:
    if len(_RECOMMEND_CACHE) >= get_settings().compare.recommend_cache_max:
        _RECOMMEND_CACHE.clear()
    _RECOMMEND_CACHE[key] = (time.monotonic() + ttl, value)


@router.post("/compare/recommendations")
async def compare_recommendations(
    session_id: str,
    body: CompareRecommendationsRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    try:
        await access_session(db, identity, session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise

    if not body.candidates:
        return {"recommendations": []}

    cache_key = _recommend_cache_key(session_id, body)
    cached = _recommend_cache_get(cache_key)
    if cached is not None:
        return {"recommendations": cached}

    compare_settings = get_settings().compare
    recommendations = await recommend_compare_pairs(
        db=db,
        identity=identity,
        session_id=session_id,
        body=body,
        timeout=compare_settings.recommend_timeout_sec,
        attempts=compare_settings.recommend_attempts,
    )
    # A non-empty result is stable and cached for the full TTL; an empty one (no
    # usable model, a failed call, or nothing the model returned that passed
    # validation) is held only for the short negative window so reopening the
    # picker soon retries the model instead of serving empty for long.
    _recommend_cache_set(
        cache_key,
        recommendations,
        compare_settings.recommend_cache_ttl_sec
        if recommendations
        else compare_settings.recommend_cache_negative_ttl_sec,
    )
    return {"recommendations": recommendations}


# Local documents uploaded from the compare picker land here so the access and
# compare endpoints — both scoped to the artifact dir — can reach them.
@router.post("/upload")
async def upload_workspace_file(
    session_id: str,
    file: UploadFile = File(...),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    try:
        session = await access_session(db, identity, session_id, write=True)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise

    filename = Path(file.filename or "").name
    if not filename:
        raise_api("VALIDATION", "filename is required", status=400)
    data = await file.read()
    if not data:
        raise_api("VALIDATION", "uploaded file is empty", status=400)
    max_bytes = get_settings().compare.upload_max_bytes
    if len(data) > max_bytes:
        raise_api("VALIDATION", f"uploaded file exceeds {max_bytes} bytes", status=400)

    paths = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=session.identity,
        agent_id=session.agent_id,
        session_id=session.id,
    )
    artifact_root = paths.artifact_dir.resolve()
    dest_dir = artifact_root / "uploads"
    if dest_dir.is_symlink() or dest_dir.resolve() != dest_dir:
        raise_api("FORBIDDEN", "unsafe upload directory", status=403)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Pin the directory on platforms supporting dir_fd so concurrent renames
    # cannot redirect writes through an agent-created uploads symlink.
    directory_fd = None
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            try:
                directory_fd = os.open(dest_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                raise_api("FORBIDDEN", "unsafe upload directory", status=403)
        stem, suffix = Path(filename).stem, Path(filename).suffix
        counter = 0
        while True:
            name = filename if counter == 0 else f"{stem}-{counter}{suffix}"
            try:
                if directory_fd is not None:
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                 0o600, dir_fd=directory_fd)
                    stream = os.fdopen(fd, "wb")
                else:
                    if dest_dir.is_symlink() or dest_dir.resolve() != dest_dir:
                        raise_api("FORBIDDEN", "unsafe upload directory", status=403)
                    stream = (dest_dir / name).open("xb")
                with stream:
                    stream.write(data)
                return {"path": f"uploads/{name}"}
            except FileExistsError:
                # Includes dangling symlinks and concurrent duplicate uploads.
                counter += 1
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

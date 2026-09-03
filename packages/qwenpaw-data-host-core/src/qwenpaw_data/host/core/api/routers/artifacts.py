# -*- coding: utf-8 -*-
"""Session artifact listing and download."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.workspace import list_session_files

router = APIRouter(prefix="/sessions/{session_id}/artifacts", tags=["artifacts"])


def _artifact_dir(state: ServiceState, session_id: str) -> Path:
    return Path(state.hosts.get(session_id=session_id).paths.artifact_dir)


@router.get("")
async def list_artifacts(
    session_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        await state.sessions.get(session_id)
        items = list_session_files(_artifact_dir(state, session_id))
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"items": items, "count": len(items)}


@router.get("/file")
async def download_artifact(
    session_id: str,
    path: str = Query(..., description="rel_path from the artifact listing"),
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> FileResponse:
    _ = identity
    try:
        await state.sessions.get(session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    # Defense in depth: reject escape characters and dot segments in the raw
    # value before any filesystem resolution. Listings only emit plain
    # forward-slash relative paths.
    if (
        not path
        or path.startswith(("/", "~"))
        or "\\" in path
        or "\x00" in path
        or any(segment in {"", ".", ".."} for segment in path.split("/"))
    ):
        raise_api("NOT_FOUND", "artifact not found", status=404)
    # Recognized normpath+prefix barrier first, then a symlink-safe resolve
    # containment check on top.
    root = _artifact_dir(state, session_id).resolve()
    normalized = os.path.normpath(os.path.join(str(root), path))
    if not normalized.startswith(str(root) + os.sep):
        raise_api("NOT_FOUND", "artifact not found", status=404)
    candidate = Path(normalized).resolve()
    if candidate == root or root not in candidate.parents:
        raise_api("NOT_FOUND", "artifact not found", status=404)
    if not candidate.is_file():
        raise_api("NOT_FOUND", "artifact not found", status=404)
    return FileResponse(candidate, filename=candidate.name)

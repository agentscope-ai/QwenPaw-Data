# -*- coding: utf-8 -*-
"""Session artifact listing and download."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, Response

from qwenpaw_data.host.core.api.deps import (
    ServiceState,
    get_identity,
    get_state,
)
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.workspace import list_session_files

router = APIRouter(
    prefix="/sessions/{session_id}/artifacts",
    tags=["artifacts"],
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _artifact_dir(state: ServiceState, session_id: str) -> Path:
    return Path(state.hosts.get(session_id=session_id).paths.artifact_dir)


async def _owned_session(
    state: ServiceState,
    identity: Identity,
    session_id: str,
):
    session = await state.sessions.get(session_id)
    if session.identity.user_id != identity.user_id:
        raise LookupError(f"session not found: {session_id}")
    return session


@router.get("")
async def list_artifacts(
    session_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        await _owned_session(state, identity, session_id)
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
    digest: str | None = Query(
        None,
        description="Expected immutable sha256 digest for a bounded copy",
    ),
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> Response:
    try:
        await _owned_session(state, identity, session_id)
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
    # Normpath+prefix barrier; the response is only reachable inside the
    # guarded branch, with a symlink-safe resolve containment check on top.
    root = _artifact_dir(state, session_id).resolve()
    normalized = os.path.normpath(os.path.join(str(root), path))
    if normalized.startswith(str(root) + os.sep):
        candidate = Path(normalized).resolve()
        if (
            candidate != root
            and root in candidate.parents
            and candidate.is_file()
        ):
            if digest is not None:
                if _DIGEST.fullmatch(digest) is None:
                    raise_api("NOT_FOUND", "artifact not found", status=404)
                try:
                    content = await asyncio.to_thread(candidate.read_bytes)
                except OSError:
                    raise_api("NOT_FOUND", "artifact not found", status=404)
                actual = "sha256:" + hashlib.sha256(content).hexdigest()
                if actual != digest:
                    raise_api(
                        "CONFLICT",
                        "artifact version changed",
                        status=409,
                    )
                return Response(
                    content=content,
                    media_type="application/octet-stream",
                    headers={
                        "X-Artifact-Digest": actual,
                    },
                )
            return FileResponse(candidate, filename=candidate.name)
    raise_api("NOT_FOUND", "artifact not found", status=404)

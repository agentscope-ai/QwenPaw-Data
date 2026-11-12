# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.models.skills import (
    AgentSkillDetailSchema,
    AgentSkillSchema,
    SkillDocumentRequest,
    SkillRevisionDetailSchema,
    SkillRevisionSchema,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.domain.skill_manager import SkillManager
from qwenpaw_data.host.core.utils.safe_name import require_safe_name

router = APIRouter(prefix="/skills", tags=["skills"])


def _manager(
    db: AsyncSession,
    identity: Identity,
    agent_id: str = "default",
) -> SkillManager:
    paths = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=identity,
        agent_id=require_safe_name(agent_id),
        session_id="default",
    )
    manager = SkillManager(db, identity=identity, paths=paths)
    manager.ensure_ready()
    return manager


def _raise(exc: Exception) -> None:
    mapped = map_domain_error(exc)
    if mapped is not None:
        raise mapped
    raise


def _revision_summary(item: dict[str, Any]) -> dict[str, Any]:
    return SkillRevisionSchema(
        revision_no=item["revision_no"],
        origin=item["origin"],
        source_ref=item["source_ref"],
        note=item["note"],
        created_at=item["created_at"],
    ).model_dump()


@router.get("/agents/{agent_id}")
async def list_agent_skills(
    agent_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        items = await _manager(db, identity, agent_id).list_skills()
    except Exception as exc:
        _raise(exc)
    return {
        "skills": [
            AgentSkillSchema(
                name=item.name,
                enabled=item.enabled,
                source=item.source,
                group=item.group,
                description=item.description,
            ).model_dump()
            for item in items
        ]
    }


@router.post("/agents/{agent_id}")
async def create_agent_skill(
    agent_id: str,
    body: SkillDocumentRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        name = await _manager(db, identity, agent_id).create(body.name, body.content)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"created": name}


@router.post("/agents/{agent_id}/import")
async def import_agent_zip(
    agent_id: str,
    file: UploadFile = File(...),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not file.filename:
        raise_api("VALIDATION", "zip filename is required", status=400)
    try:
        manager = _manager(db, identity, agent_id)
        with tempfile.TemporaryDirectory(prefix="qwenpaw-data-skill-upload-") as tmp:
            zip_path = Path(tmp) / Path(file.filename).name
            zip_path.write_bytes(await file.read())
            names = await manager.import_zip(zip_path)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"imported": names}


@router.get("/agents/{agent_id}/{name}")
async def get_agent_skill(
    agent_id: str,
    name: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        info, content = await _manager(db, identity, agent_id).read_skill(name)
    except Exception as exc:
        _raise(exc)
    return AgentSkillDetailSchema(
        name=info.name,
        enabled=info.enabled,
        source=info.source,
        group=info.group,
        description=info.description,
        content=content,
    ).model_dump()


@router.put("/agents/{agent_id}/{name}")
async def save_agent_skill(
    agent_id: str,
    name: str,
    body: SkillDocumentRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if body.name != name:
        raise_api(
            "VALIDATION",
            f"skill rename is not supported: {name!r} -> {body.name!r}",
            status=400,
        )
    try:
        saved = await _manager(db, identity, agent_id).save(
            name, body.content, base_content=body.base_content
        )
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"saved": saved}


@router.post("/agents/{agent_id}/{name}/enable")
async def enable_agent_skill(
    agent_id: str,
    name: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await _manager(db, identity, agent_id).enable(name)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"enabled": True, "name": name}


@router.post("/agents/{agent_id}/{name}/disable")
async def disable_agent_skill(
    agent_id: str,
    name: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await _manager(db, identity, agent_id).disable(name)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"enabled": False, "name": name}


@router.delete("/agents/{agent_id}/{name}")
async def uninstall_agent_skill(
    agent_id: str,
    name: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await _manager(db, identity, agent_id).uninstall(name)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"uninstalled": name}


@router.get("/agents/{agent_id}/{name}/revisions")
async def list_skill_revisions(
    agent_id: str,
    name: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        items = await _manager(db, identity, agent_id).history(name)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {
        "revisions": [_revision_summary(item) for item in items],
        "count": len(items),
    }


@router.get("/agents/{agent_id}/{name}/revisions/{revision_no}")
async def get_skill_revision(
    agent_id: str,
    name: str,
    revision_no: int,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        item = await _manager(db, identity, agent_id).revision(name, revision_no)
    except Exception as exc:
        _raise(exc)
    return SkillRevisionDetailSchema(
        **_revision_summary(item),
        files=item["files"],
    ).model_dump()


@router.post("/agents/{agent_id}/{name}/revisions/{revision_no}/revert")
async def revert_skill_revision(
    agent_id: str,
    name: str,
    revision_no: int,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        created = await _manager(db, identity, agent_id).revert(name, revision_no)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"ok": True, "revision": _revision_summary(created)}

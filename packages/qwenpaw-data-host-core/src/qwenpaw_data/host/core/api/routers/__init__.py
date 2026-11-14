# -*- coding: utf-8 -*-
from fastapi import APIRouter

from qwenpaw_data.host.core.api.routers import (
    artifact,
    artifact_compare,
    auth,
    channels,
    clarification,
    compat,
    console,
    cron,
    datasources,
    events,
    feedback,
    mcp,
    plan,
    preferences,
    sessions,
    settlement,
    skill_drafts,
    skill_proposals,
    skills,
    sop,
    steer,
    tool_confirmation,
    trace,
    tracking,
    ui,
    usage,
)

def _include_native_routes(router: APIRouter) -> None:
    router.include_router(auth.router)
    router.include_router(sessions.router)
    router.include_router(console.router)
    router.include_router(tracking.router)
    router.include_router(steer.router)
    router.include_router(clarification.router)
    router.include_router(tool_confirmation.router)
    router.include_router(plan.router)
    router.include_router(sop.router)
    router.include_router(feedback.router)
    router.include_router(artifact.router)
    router.include_router(artifact_compare.router)
    router.include_router(trace.router)
    router.include_router(events.router)
    router.include_router(datasources.router)
    router.include_router(channels.router)
    router.include_router(channels.config_router)
    router.include_router(preferences.router)
    router.include_router(settlement.router)
    router.include_router(settlement.workspace_router)
    router.include_router(skill_drafts.router)
    router.include_router(skill_proposals.router)
    router.include_router(skill_proposals.workspace_router)
    router.include_router(skills.router)
    router.include_router(mcp.router)
    router.include_router(cron.router)
    router.include_router(usage.router)
    router.include_router(ui.router)


# The 0.4 Host contract is unversioned beneath /api.
api_router = APIRouter(prefix="/api")
_include_native_routes(api_router)

# Keep the OSS 0.3 surface as a compatibility alias without changing the
# canonical 0.4 route tree. The bridge-only endpoints live on this router too.
legacy_api_router = APIRouter(prefix="/api/v1")
_include_native_routes(legacy_api_router)
legacy_api_router.include_router(compat.router)

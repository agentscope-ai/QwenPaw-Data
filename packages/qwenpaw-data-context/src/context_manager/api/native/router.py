"""Aggregate maintained HTTP capabilities and their request integration hooks."""

from fastapi import APIRouter, FastAPI

from .config.router import router as config_router
from .embedding import router as embedding_router
from .graph.router import router as graph_router
from .knowledge import router as knowledge_router
from .metadata import install_weaving, router as metadata_router
from .rdb.downloads import router as sql_downloads_router
from .rdb.router import router as rdb_router
from .user import router as user_router

router = APIRouter()
router.include_router(graph_router, prefix="/api/v1", tags=["context-manager"])
router.include_router(config_router, prefix="/api/v1", tags=["context-manager"])
router.include_router(embedding_router, prefix="/api/v1", tags=["context-manager"])
router.include_router(rdb_router, prefix="/api/v1", tags=["context-manager"])
router.include_router(user_router)
router.include_router(knowledge_router)
router.include_router(metadata_router)
router.include_router(sql_downloads_router)


def mount_native_routes(application: FastAPI) -> None:
    """Mount maintained routes and bind Metadata publication to their requests."""
    application.include_router(router)
    install_weaving(application)


__all__ = ["mount_native_routes", "router"]

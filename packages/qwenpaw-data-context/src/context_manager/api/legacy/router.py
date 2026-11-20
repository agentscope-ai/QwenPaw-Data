"""Aggregate temporary legacy HTTP adapters outside canonical CM routes."""

from fastapi import APIRouter

from .config import router as config_router
from .datasource_active import router as datasource_active_router
from .datasource_types import router as datasource_types_router
from .embedding_jobs import router as embedding_jobs_router
from .explorer.cypher import router as cypher_router
from .explorer.graph_center import explore_router, router as graph_center_router
from .explorer.raw_cypher import router as raw_cypher_router
from .kg import router as kg_router
from .metadata import router as metadata_router

# TODO(cm-api-compat): remove this aggregate and its app mount after every
# listed legacy frontend caller has migrated to a current API contract.
router = APIRouter()
router.include_router(graph_center_router)
router.include_router(cypher_router)
router.include_router(raw_cypher_router)
router.include_router(explore_router)
router.include_router(config_router)
router.include_router(datasource_active_router)
router.include_router(datasource_types_router)
router.include_router(embedding_jobs_router)
router.include_router(kg_router)
router.include_router(metadata_router)

__all__ = ["router"]

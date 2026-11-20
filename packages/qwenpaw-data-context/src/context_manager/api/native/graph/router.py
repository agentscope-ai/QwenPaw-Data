"""Aggregate canonical graph routes without owning endpoint logic."""

from fastapi import APIRouter

from .edge import router as edge_router
from .nodes import router as nodes_router
from .schema import router as schema_router
from .traversal import router as traversal_router

router = APIRouter(prefix="/graph")
router.include_router(nodes_router)
router.include_router(edge_router)
router.include_router(schema_router)
router.include_router(traversal_router)

__all__ = ["router"]

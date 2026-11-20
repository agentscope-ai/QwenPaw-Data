"""Aggregate controlled relational-query routes without owning endpoint logic."""

from fastapi import APIRouter

from .sql import router as sql_router

router = APIRouter(prefix="/rdb")
router.include_router(sql_router)

__all__ = ["router"]

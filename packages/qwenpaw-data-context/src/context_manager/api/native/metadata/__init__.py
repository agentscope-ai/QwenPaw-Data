"""Expose Metadata weaving, userspace and feedback HTTP operations."""

from fastapi import APIRouter

from .feedback_card import router as feedback_card_router
from .userspace import router as userspace_router
from .weaving import install_weaving
from .weaving import router as weaving_router

router = APIRouter()
router.include_router(feedback_card_router)
router.include_router(userspace_router)
router.include_router(weaving_router)

__all__ = ["install_weaving", "router"]

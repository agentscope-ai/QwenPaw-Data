"""Unified API response envelope for MG/TG/KG/Explorer routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException


def success(data: Any = None, *, meta: Optional[dict] = None) -> dict[str, Any]:
    """Wrap a successful result into the standard envelope."""
    return {"ok": True, "data": data, "error": None, "meta": meta}


def paginated(
    data: Any,
    *,
    total: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Wrap a paginated result with total/page/has_more metadata."""
    return {
        "ok": True,
        "data": data,
        "error": None,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": (page * page_size) < total,
        },
    }


def fail(
    code: str,
    message: str,
    *,
    status_code: int = 400,
    detail: Optional[dict] = None,
) -> None:
    """Raise an HTTPException with the standard error envelope."""
    raise HTTPException(
        status_code=status_code,
        detail={
            "ok": False,
            "data": None,
            "error": {"code": code, "message": message, "detail": detail or {}},
            "meta": None,
        },
    )


def clamp_page(
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    *,
    max_page_size: int = 500,
) -> tuple[int, int]:
    """Normalise and clamp page/page_size to safe bounds."""
    p = max(1, int(page or 1))
    ps = max(1, min(int(page_size or 20), max_page_size))
    return p, ps

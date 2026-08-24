from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """统一分页出参：{ records, total, page, size }。"""

    records: list[T]
    total: int
    page: int
    size: int


def offset(page: int, size: int) -> int:
    return (max(page, 1) - 1) * size

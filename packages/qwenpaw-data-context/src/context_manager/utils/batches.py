"""Stable grouping of independent items within a caller-selected batch."""

from collections.abc import Callable, Hashable, Iterable, ItemsView
from typing import TypeVar

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)


def grouped(rows: Iterable[T], *, key: Callable[[T], K]) -> ItemsView[K, list[T]]:
    """Preserve first group occurrence and input order within each group.

    Callers own batch limits, independence and scope boundaries. This may reorder
    items across groups; never pass dependent phases or a stream of transactions.
    """
    groups: dict[K, list[T]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return groups.items()

"""Validate finite vectors and provide business-neutral vector math."""

from __future__ import annotations

import math
from collections.abc import Sequence


def require_finite_vector(value: object, field_name: str) -> tuple[float, ...]:
    """Return one finite, non-empty numeric vector without booleans."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"{field_name} must be a non-empty sequence")
    if any(
        not isinstance(item, (int, float))
        or isinstance(item, bool)
        or not math.isfinite(float(item))
        for item in value
    ):
        raise ValueError(f"{field_name} contains invalid values")
    return tuple(float(item) for item in value)


def require_vector_batch(
    value: object,
    field_name: str,
    *,
    expected_count: int | None = None,
) -> tuple[tuple[float, ...], ...]:
    """Return a finite batch with one consistent vector dimensionality."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{field_name} must be a sequence of vectors")
    if expected_count is not None and len(value) != expected_count:
        raise ValueError(f"{field_name} count must be {expected_count}")
    vectors = tuple(
        require_finite_vector(item, f"{field_name} vector") for item in value
    )
    if vectors and len({len(vector) for vector in vectors}) != 1:
        raise ValueError(f"{field_name} have inconsistent dimensions")
    return vectors


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return a non-negative cosine score for equal-dimension finite vectors."""
    if len(left) != len(right):
        raise ValueError("vectors must have equal dimensions")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    score = sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )
    return max(0.0, min(1.0, score))


__all__ = [
    "cosine_similarity",
    "require_finite_vector",
    "require_vector_batch",
]

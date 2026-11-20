"""Define User extraction failures without transport or persistence details."""

from __future__ import annotations

from enum import StrEnum


class UserExtractionErrorCode(StrEnum):
    """Classify stable failures from the User algorithm boundary."""

    EXTRACTION_FAILED = "extraction_failed"
    EXTRACTION_INVALID = "extraction_invalid"
    RESOLUTION_FAILED = "resolution_failed"
    RESOLUTION_INVALID = "resolution_invalid"


class UserExtractionError(RuntimeError):
    """Report one sanitized User extraction failure with a stable code."""

    def __init__(self, code: UserExtractionErrorCode, message: str) -> None:
        """Bind a public-safe message to one exact failure class."""
        super().__init__(message)
        self.code = code


__all__ = ["UserExtractionError", "UserExtractionErrorCode"]

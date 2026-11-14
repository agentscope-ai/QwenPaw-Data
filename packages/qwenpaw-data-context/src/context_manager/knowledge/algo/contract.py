"""Define document-import contracts without exposing Store implementation details."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)

from ...utils.validation import require_trimmed_text


class KnowledgeImportErrorCode(StrEnum):
    """Classify stable document-import failures for transport mapping."""

    UNSUPPORTED_DOCUMENT = "unsupported_document"
    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_INVALID = "document_invalid"
    EXTRACTION_FAILED = "extraction_failed"
    EXTRACTION_INVALID = "extraction_invalid"
    CANDIDATE_LIMIT = "candidate_limit"
    WRITE_FAILED = "write_failed"
    UNAVAILABLE = "unavailable"


class KnowledgeImportError(RuntimeError):
    """Report one sanitized import failure with a stable machine code."""

    def __init__(self, code: KnowledgeImportErrorCode, message: str) -> None:
        """Bind a public-safe message to one exact failure class."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class UploadedDocument:
    """Carry one request-local upload without persisting the source object."""

    filename: str
    content_type: str | None
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        """Reject malformed transport values before document parsing."""
        object.__setattr__(
            self,
            "filename",
            require_trimmed_text(self.filename, "filename"),
        )
        if self.content_type is not None and not isinstance(self.content_type, str):
            raise TypeError("content_type must be text or None")
        if not isinstance(self.content, bytes):
            raise TypeError("document content must be bytes")
        if not self.content:
            raise ValueError("document content must not be empty")


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Carry extracted plain text and deterministic source identity in memory."""

    source_id: str
    filename: str
    text: str = field(repr=False)
    content_hash: str


class KnowledgeNodeSeed(BaseModel):
    """Identify one imported graph node for bounded frontend expansion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: Literal["Entity", "Event"]
    identity: dict[str, str]


class KnowledgeImportResult(BaseModel):
    """Return one bounded, secret-free document import result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    status: Literal["success", "degraded"]
    seed_nodes: list[KnowledgeNodeSeed] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "KnowledgeImportError",
    "KnowledgeImportErrorCode",
    "KnowledgeImportResult",
    "KnowledgeNodeSeed",
    "ParsedDocument",
    "UploadedDocument",
]

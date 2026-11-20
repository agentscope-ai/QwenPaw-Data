"""Define Knowledge document identity and lifecycle states."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

_DOCUMENT_ID_PATTERN = re.compile(r"document:([0-9a-f]{64})(?::[0-9a-f]{64})?\Z")


class DocumentContentMismatchError(ValueError):
    """The retained original no longer matches its document identity."""


def make_document_id(filename: str, content_hash: str) -> str:
    """Distinguish named originals while retaining the exact-byte content digest."""
    filename_hash = hashlib.sha256(filename.encode("utf-8")).hexdigest()
    return f"document:{content_hash}:{filename_hash}"


def _content_sha256_from_document_id(document_id: str) -> str:
    """Validate one canonical document identity and return its digest."""
    if not isinstance(document_id, str):
        raise TypeError("document_id must be text")
    matched = _DOCUMENT_ID_PATTERN.fullmatch(document_id)
    if matched is None:
        raise ValueError(
            "document_id must be document:<content sha256>[:<filename sha256>]"
        )
    return matched.group(1)


class DocumentStatus(StrEnum):
    """Enumerate the finite lifecycle states reserved for stored documents."""

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    DELETING = "deleting"


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Describe one stored document without owning its stable identity."""

    filename: str
    content_type: str | None
    byte_size: int
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime
    ingest_error: str | None = None


@dataclass(frozen=True, slots=True)
class Document:
    """Identify a named original independently of other files with identical bytes."""

    document_id: str
    metadata: DocumentMetadata

    def __post_init__(self) -> None:
        """Accept filename-qualified identities and previously stored content-only IDs."""
        _content_sha256_from_document_id(self.document_id)

    @property
    def content_sha256(self) -> str:
        """Return the SHA-256 digest embedded in the document identity."""
        return _content_sha256_from_document_id(self.document_id)

    @classmethod
    def from_content(
        cls,
        content: bytes,
        metadata: DocumentMetadata,
    ) -> Document:
        """Generate a stable identity from the filename and exact uploaded bytes."""
        digest = hashlib.sha256(content).hexdigest()
        return cls(
            document_id=make_document_id(metadata.filename, digest), metadata=metadata
        )


__all__ = [
    "Document",
    "DocumentContentMismatchError",
    "DocumentMetadata",
    "DocumentStatus",
    "make_document_id",
]

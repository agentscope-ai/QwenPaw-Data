"""Read bounded PDF, DOCX, TXT and Markdown uploads into plain text."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from docx import Document
from pypdf import PdfReader

from ..document import make_document_id
from .contract import (
    KnowledgeImportError,
    KnowledgeImportErrorCode,
    ParsedDocument,
    UploadedDocument,
)

_PDF_TYPES = frozenset({"application/pdf", "application/octet-stream", ""})
_DOCX_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream",
        "",
    }
)
_TEXT_TYPES = frozenset(
    {"text/plain", "text/markdown", "text/x-markdown", "application/octet-stream", ""}
)


@dataclass(frozen=True, slots=True)
class DocumentLimits:
    """Bound source bytes and extracted text before LLM processing."""

    max_bytes: int
    max_pages: int
    max_chars: int

    def __post_init__(self) -> None:
        """Require practical positive parsing limits."""
        if not 1 <= self.max_bytes <= 100 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 100 MiB")
        if not 1 <= self.max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        if not 1 <= self.max_chars <= 5_000_000:
            raise ValueError("max_chars must be between 1 and 5,000,000")


def validate_upload(
    upload: UploadedDocument,
    *,
    limits: DocumentLimits,
) -> None:
    """Validate upload admission before accepting a background import."""
    if type(upload) is not UploadedDocument:
        raise TypeError("upload must be an UploadedDocument")
    if type(limits) is not DocumentLimits:
        raise TypeError("limits must be DocumentLimits")
    if len(upload.content) > limits.max_bytes:
        raise KnowledgeImportError(
            KnowledgeImportErrorCode.DOCUMENT_TOO_LARGE,
            "document exceeds the upload size limit",
        )
    suffix = Path(upload.filename).suffix.casefold()
    content_type = (upload.content_type or "").split(";", 1)[0].strip().casefold()
    accepted = {
        ".pdf": _PDF_TYPES,
        ".docx": _DOCX_TYPES,
        ".txt": _TEXT_TYPES,
        ".md": _TEXT_TYPES,
    }
    if suffix not in accepted or content_type not in accepted[suffix]:
        raise _unsupported()


def read_document(
    upload: UploadedDocument,
    *,
    limits: DocumentLimits,
) -> ParsedDocument:
    """Parse a supported upload without storing the original document."""
    validate_upload(upload, limits=limits)
    suffix = Path(upload.filename).suffix.casefold()
    if suffix == ".pdf":
        text = _read_pdf(upload.content, limits)
    elif suffix == ".docx":
        text = _read_docx(upload.content)
    else:
        try:
            text = upload.content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.DOCUMENT_INVALID,
                "text documents must use UTF-8 encoding",
            ) from exc
    normalized = "\n\n".join(
        part.strip() for part in text.split("\n\n") if part.strip()
    )
    if not normalized:
        raise KnowledgeImportError(
            KnowledgeImportErrorCode.DOCUMENT_INVALID,
            "document contains no extractable text",
        )
    if len(normalized) > limits.max_chars:
        raise KnowledgeImportError(
            KnowledgeImportErrorCode.DOCUMENT_TOO_LARGE,
            "extracted document text exceeds the processing limit",
        )
    digest = hashlib.sha256(upload.content).hexdigest()
    return ParsedDocument(
        source_id=make_document_id(upload.filename, digest),
        filename=Path(upload.filename).name,
        text=normalized,
        content_hash=digest,
    )


def _unsupported() -> KnowledgeImportError:
    """Return the stable unsupported-document failure."""
    return KnowledgeImportError(
        KnowledgeImportErrorCode.UNSUPPORTED_DOCUMENT,
        "only PDF, DOCX, TXT and Markdown documents are supported",
    )


def _read_pdf(content: bytes, limits: DocumentLimits) -> str:
    """Extract text from a bounded non-encrypted PDF."""
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.DOCUMENT_INVALID,
                "encrypted PDF documents are not supported",
            )
        if len(reader.pages) > limits.max_pages:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.DOCUMENT_TOO_LARGE,
                "PDF exceeds the page limit",
            )
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except KnowledgeImportError:
        raise
    except Exception as exc:
        raise KnowledgeImportError(
            KnowledgeImportErrorCode.DOCUMENT_INVALID,
            "PDF document cannot be parsed",
        ) from exc


def _read_docx(content: bytes) -> str:
    """Extract visible paragraph and table-cell text from one DOCX file."""
    try:
        document = Document(BytesIO(content))
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                paragraphs.append("\t".join(cell.text for cell in row.cells))
        return "\n\n".join(paragraphs)
    except Exception as exc:
        raise KnowledgeImportError(
            KnowledgeImportErrorCode.DOCUMENT_INVALID,
            "DOCX document cannot be parsed",
        ) from exc


__all__ = ["DocumentLimits", "read_document", "validate_upload"]

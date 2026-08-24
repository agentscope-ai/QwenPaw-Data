"""Extract plain text from uploaded document bytes (.txt / .md / .docx / .pdf)."""
from __future__ import annotations

import io
from pathlib import Path


def extract_text(filename: str, content: bytes) -> str:
    """Return UTF-8 plain text for *content* based on *filename* extension.

    Supported formats:
    - ``.txt`` / ``.md`` — decoded directly as UTF-8
    - ``.docx``          — paragraphs joined via ``python-docx``
    - ``.pdf``           — pages joined via ``pypdf``

    Raises ``ValueError`` for unsupported extensions.
    Raises ``ImportError`` if the required library is not installed.
    """
    ext = Path(filename).suffix.lower()
    if ext in (".txt", ".md"):
        return content.decode("utf-8", errors="replace")
    if ext == ".docx":
        return _extract_docx(content)
    if ext == ".pdf":
        return _extract_pdf(content)
    raise ValueError(f"unsupported file extension for text extraction: {ext!r}")


def _extract_docx(content: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise ImportError(
            "python-docx is required for .docx extraction: pip install python-docx"
        ) from exc
    doc = docx.Document(io.BytesIO(content))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)


def _extract_pdf(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ImportError(
            "pypdf is required for .pdf extraction: pip install pypdf"
        ) from exc
    reader = PdfReader(io.BytesIO(content))
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            pages.append(text)
    return "\n\n".join(pages)

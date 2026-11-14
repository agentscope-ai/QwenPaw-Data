"""Verify the document formats and byte limits offered by the upload page."""

from hashlib import sha256
from types import SimpleNamespace

import pytest
from context_manager.knowledge import (
    DocumentLimits,
    UploadedDocument,
    read_document,
)

from cm.knowledge.import_support import _docx


@pytest.fixture
def document_limits():
    return DocumentLimits(max_bytes=50 * 1024 * 1024, max_pages=100, max_chars=500_000)


@pytest.mark.parametrize(
    "filename,media_type",
    [
        ("项目.txt", "text/plain; charset=utf-8"),
        ("项目.MD", "text/markdown"),
        ("项目.md", "application/octet-stream"),
        ("项目.txt", None),
        ("report.docx", None),
        ("report.pdf", "application/pdf"),
    ],
)
def test_upload_formats_preserve_text_and_original_byte_identity(
    filename,
    media_type,
    document_limits,
    monkeypatch,
):
    text = "# 星舟项目\n\n林岚负责客户培训，计划在下周上线。"
    if filename.endswith("docx"):
        content = _docx(text).content
    elif filename.endswith("pdf"):
        content = b"%PDF-test"
        reader = SimpleNamespace(
            is_encrypted=False,
            pages=[SimpleNamespace(extract_text=lambda: text)],
        )
        monkeypatch.setattr(
            "context_manager.knowledge.document_reader.PdfReader", lambda _: reader
        )
    else:
        content = ("\ufeff" + text).encode()
    parsed = read_document(
        UploadedDocument(filename, media_type, content), limits=document_limits
    )
    assert parsed.text == text
    assert parsed.source_id == (
        f"document:{sha256(content).hexdigest()}:{sha256(filename.encode()).hexdigest()}"
    )
    assert parsed.content_hash == sha256(content).hexdigest()

"""Verify user-scoped original files and explicit persisted document states."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from context_manager.knowledge.document import (
    Document,
    DocumentMetadata,
    DocumentStatus,
)
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from cm.knowledge.document_support import (
    DocumentResources,
    create_document,
    delete_document,
    download_document,
    make_document_resources,
)


def store_at(home: Path, **target: str) -> DocumentResources:
    return make_document_resources(
        home,
        **{
            "tenant_id": "tenant",
            "workspace_id": "workspace",
            "user_id": "alice",
            **target,
        },
    )


def document_for(
    content: bytes = b"report",
    filename: str = "report.pdf",
    content_type: str = "application/pdf",
) -> Document:
    now = datetime.now(UTC)
    return Document.from_content(
        content,
        DocumentMetadata(
            filename=filename,
            content_type=content_type,
            byte_size=len(content),
            status=DocumentStatus.UPLOADED,
            created_at=now,
            updated_at=now,
        ),
    )


def test_concurrent_store_instances_accept_only_one_upload_per_filename(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    stores = (store_at(tmp_path), store_at(tmp_path))
    started = Barrier(2)

    def upload(index):
        content = f"version-{index}".encode()
        document = document_for(content)
        started.wait(timeout=5)
        try:
            return create_document(stores[index], document, content)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(upload, (0, 1)))
    (accepted,) = [result for result in results if result is not None]
    assert stores[0].repository.list() == (accepted,)
    assert download_document(stores[0], accepted.document_id)[1] in (
        b"version-0",
        b"version-1",
    )


def test_original_write_failure_rolls_back_catalog_without_removing_existing_file(
    tmp_path,
):
    store = store_at(tmp_path)
    path = store.files.path("report.pdf")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing original")
    with pytest.raises(FileExistsError):
        create_document(store, document_for(), b"report")
    assert store.repository.list() == ()
    assert path.read_bytes() == b"existing original"


def test_failed_catalog_commit_removes_only_new_original(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    retained = document_for(b"retained", "existing.pdf")
    create_document(store, retained, b"retained")

    def fail_commit(connection):
        if store.files.path("report.pdf").exists():
            assert store.files.path("report.pdf").read_bytes() == b"report"
            raise SQLAlchemyError("database commit failed")

    event.listen(store.repository._engine, "commit", fail_commit)
    try:
        with pytest.raises(SQLAlchemyError, match="database commit failed"):
            create_document(store, document_for(), b"report")
    finally:
        event.remove(store.repository._engine, "commit", fail_commit)

    assert not store.files.path("report.pdf").exists()
    assert store.repository.list() == (retained,)
    assert download_document(store, retained.document_id)[1] == b"retained"


def test_user_directory_symlink_cannot_read_another_users_records(
    tmp_path: Path,
) -> None:
    alice = store_at(tmp_path)
    bob = store_at(tmp_path, user_id="bob")
    document = document_for()
    create_document(bob, document, b"report")
    (tmp_path / "tenant/workspace/alice").symlink_to(
        tmp_path / "tenant/workspace/bob", target_is_directory=True
    )

    assert alice.repository.list() == ()
    with pytest.raises(FileNotFoundError):
        download_document(alice, document.document_id)
    with pytest.raises(FileNotFoundError):
        delete_document(alice, document.document_id)
    assert download_document(bob, document.document_id)[1] == b"report"


def test_original_symlink_cannot_download_or_delete_external_file(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    document = document_for()
    create_document(store, document, b"report")
    external = tmp_path / "external.pdf"
    external.write_bytes(b"private")
    store.files.path("report.pdf").unlink()
    (tmp_path / "tenant/workspace/alice/kg-docs/report.pdf").symlink_to(external)

    with pytest.raises(ValueError, match="symbolic links"):
        download_document(store, document.document_id)
    with pytest.raises(ValueError, match="symbolic links"):
        delete_document(store, document.document_id)
    assert external.read_bytes() == b"private"

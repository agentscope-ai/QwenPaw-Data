"""Verify SQL execution ownership and recovery across independent repositories."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from context_manager.db import CMDatabase
from context_manager.knowledge import repository
from context_manager.knowledge.document import DocumentStatus
from context_manager.knowledge.repository import DocumentLeaseLost, DocumentRepository
from context_manager.schema.relational import documents, kg_user_claims
from context_manager.settings.db import DbSettings

from cm.knowledge.document_support import create_document, make_document_resources
from cm.knowledge.test_document_storage import document_for


@pytest.fixture(params=["sqlite", "postgresql"])
def catalogs(request, tmp_path):
    url = f"sqlite:///{tmp_path / 'catalog.db'}"
    if request.param == "postgresql":
        url = os.environ.get("CM_KG_LEASE_TEST_PG_URL")
        if not url:
            pytest.skip(
                "Set CM_KG_LEASE_TEST_PG_URL to an isolated PostgreSQL test database"
            )
    databases = [CMDatabase.from_settings(DbSettings(url=url)) for _ in range(2)]
    databases[0].initialize()
    # Unique owners keep test cases isolated without deleting another run's records.
    target = {
        "tenant_id": f"kg-test-{uuid4().hex}",
        "workspace_id": "workspace",
        "user_id": "user",
    }
    resources = make_document_resources(tmp_path, engine=databases[0].engine, **target)
    document = document_for()
    create_document(resources, document, b"report")
    try:
        yield (
            resources.repository,
            DocumentRepository(databases[1].engine, **target),
            document,
        )
    finally:
        with databases[0].engine.begin() as connection:
            for table in (kg_user_claims, documents):
                connection.execute(
                    table.delete().where(table.c.tenant_id == target["tenant_id"])
                )
        for database in databases:
            database.close()


def test_concurrent_claims_serialize_user_graph_changes(catalogs):
    first, second, document = catalogs
    duplicate = document_for(b"different content")
    token = second.claim(duplicate.document_id)
    try:
        with pytest.raises(FileExistsError), second.transaction() as connection:
            second.create(connection, duplicate, token=token)
    finally:
        second.release(token)
    gate = Barrier(2)

    def claim(catalog):
        gate.wait(timeout=5)
        try:
            return catalog.claim(document.document_id)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        tokens = list(workers.map(claim, (first, second)))
    assert sum(token is not None for token in tokens) == 1
    token = next(token for token in tokens if token)
    with pytest.raises(FileExistsError):
        second.claim(document.document_id)
    with pytest.raises(FileExistsError, match="doc_operation_in_progress"):
        second.claim("document:other")
    other_user = DocumentRepository(
        first._engine,
        tenant_id=first.target[0],
        workspace_id=first.target[1],
        user_id="other",
    )
    other = other_user.claim(document.document_id)
    other_user.release(other)
    first.release(token)
    # A rejected operation must leave no partial document claim behind.
    replacement = second.claim("document:other")
    second.release(replacement)


def test_stale_claim_lets_a_new_epoch_own_the_document(catalogs, monkeypatch):
    first, second, document = catalogs
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(repository.time, "time", lambda: clock["now"])
    old = first.claim(document.document_id)
    first.set_status(document.document_id, DocumentStatus.PROCESSING, token=old)
    clock["now"] += 301
    interrupted = second.get(document.document_id)
    assert interrupted.metadata.status is DocumentStatus.FAILED
    assert "interrupted" in interrupted.metadata.ingest_error
    new = second.claim(document.document_id)
    second.set_status(document.document_id, DocumentStatus.PROCESSING, token=new)
    assert not first.renew(old)
    with pytest.raises(DocumentLeaseLost):
        first.set_status(document.document_id, DocumentStatus.READY, token=old)
    with pytest.raises(DocumentLeaseLost):
        first.delete(document.document_id, token=old)
    first.release(old)
    with pytest.raises(FileExistsError):
        first.claim(document.document_id)
    second.set_status(document.document_id, DocumentStatus.READY, token=new)
    second.release(new)
    assert first.get(document.document_id).metadata.status is DocumentStatus.READY

    deletion = second.claim(document.document_id)
    second.set_status(document.document_id, DocumentStatus.DELETING, token=deletion)
    clock["now"] += 301
    interrupted = first.get(document.document_id)
    assert interrupted.metadata.status is DocumentStatus.DELETING
    assert "retry deletion" in interrupted.metadata.ingest_error
    retry = first.claim(document.document_id)
    first.delete(document.document_id, token=retry)
    first.release(retry)
    assert first.list() == ()


def _claim_row(catalog):
    from sqlalchemy import select

    from context_manager.schema.relational import kg_user_claims

    with catalog._engine.connect() as connection:
        return (
            connection.execute(
                select(kg_user_claims).where(
                    kg_user_claims.c.tenant_id == catalog.target[0],
                    kg_user_claims.c.workspace_id == catalog.target[1],
                    kg_user_claims.c.user_id == catalog.target[2],
                )
            )
            .mappings()
            .one()
        )


def test_sweep_fails_a_claim_that_is_still_stale(catalogs):
    from sqlalchemy import update

    from context_manager.heartbeat import sweep_connection
    from context_manager.schema.relational import kg_user_claims

    catalog, _other, document = catalogs
    token = catalog.claim(document.document_id)
    catalog.set_status(document.document_id, DocumentStatus.PROCESSING, token=token)
    with catalog._engine.begin() as connection:
        connection.execute(
            update(kg_user_claims)
            .where(
                kg_user_claims.c.tenant_id == catalog.target[0],
                kg_user_claims.c.workspace_id == catalog.target[1],
                kg_user_claims.c.user_id == catalog.target[2],
            )
            .values(heartbeat_at=0)
        )
        sweep_connection(connection)
    assert catalog.get(document.document_id).metadata.status is DocumentStatus.FAILED
    assert _claim_row(catalog)["heartbeat_at"] is None
    assert not catalog.renew(token)
    with pytest.raises(DocumentLeaseLost):
        catalog.set_status(document.document_id, DocumentStatus.READY, token=token)


def test_sweep_keeps_a_fresh_claim_and_a_deleting_document(catalogs):
    from sqlalchemy import update

    from context_manager.heartbeat import sweep_connection
    from context_manager.schema.relational import kg_user_claims

    catalog, _other, document = catalogs
    token = catalog.claim(document.document_id)
    catalog.set_status(document.document_id, DocumentStatus.PROCESSING, token=token)
    with catalog._engine.begin() as connection:
        sweep_connection(connection)
    assert (
        catalog.get(document.document_id).metadata.status is DocumentStatus.PROCESSING
    )
    assert _claim_row(catalog)["heartbeat_at"] is not None
    assert catalog.renew(token)

    catalog.set_status(document.document_id, DocumentStatus.DELETING, token=token)
    with catalog._engine.begin() as connection:
        connection.execute(
            update(kg_user_claims)
            .where(
                kg_user_claims.c.tenant_id == catalog.target[0],
                kg_user_claims.c.workspace_id == catalog.target[1],
                kg_user_claims.c.user_id == catalog.target[2],
            )
            .values(heartbeat_at=0)
        )
        sweep_connection(connection)
    assert catalog.get(document.document_id).metadata.status is DocumentStatus.DELETING
    assert _claim_row(catalog)["heartbeat_at"] is None

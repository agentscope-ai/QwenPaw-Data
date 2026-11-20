"""Provide isolated SQL document catalogs and scoped database failure injection."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from context_manager.db import CMDatabase
from context_manager.knowledge.files import DocumentFileStore
from context_manager.knowledge.repository import DocumentRepository
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError

from cm.infrastructure.execution.support import executor


@dataclass
class DocumentResources:
    repository: DocumentRepository
    files: DocumentFileStore

    @property
    def arguments(self):
        return {
            "documents": self.repository,
            "files": self.files,
            "run_sync": asyncio.to_thread,
        }

    @property
    def import_arguments(self):
        tenant, workspace, _ = self.repository.target
        return {
            "database_engine": self.repository._engine,
            "home": self.files.home,
            "tenant_id": tenant,
            "workspace_id": workspace,
            "executor": executor,
        }


def make_document_resources(
    home: Path, *, engine: Engine | None = None, **target: str
) -> DocumentResources:
    files = DocumentFileStore(home, **target)
    if engine is None:
        engine = create_engine(
            URL.create(
                "sqlite",
                database=str(home.with_name(f"{home.name}-documents.sqlite")),
            )
        )
    CMDatabase(engine).initialize()
    return DocumentResources(DocumentRepository(engine, **target), files)


def runtime_documents(runtime, context):
    importer = runtime.knowledge_importer(context)
    return DocumentResources(
        importer.document_repository(context),
        importer.document_files(context),
    )


def document_importer(resources, *, store=None, caller=None, glossary=None):
    from context_manager.knowledge import KnowledgeImportService
    from context_manager.schema import GRAPH_SCHEMA

    from cm.infrastructure.models.support import StaticModelService
    from cm.infrastructure.storage.schema_support import make_glossary
    from cm.knowledge.import_support import FakeKnowledgeCaller
    from cm.knowledge.support import MemoryGraphStore

    return KnowledgeImportService(
        store if store is not None else MemoryGraphStore(),
        glossary if glossary is not None else make_glossary(GRAPH_SCHEMA),
        lambda user: StaticModelService(
            caller if caller is not None else FakeKnowledgeCaller({})
        ),
        **resources.import_arguments,
    )


def create_document(resources, document, content):
    token = resources.repository.claim(document.document_id)
    try:
        return document_importer(resources)._persist_document(
            document,
            content,
            resources.repository,
            resources.files,
            token=token,
        )
    finally:
        resources.repository.release(token)


def set_document_status(resources, document_id, status, *, error=None, active=False):
    """Seed a status with an owned execution, optionally leaving it active."""
    token = resources.repository.claim(document_id)
    try:
        return resources.repository.set_status(
            document_id, status, error=error, token=token
        )
    finally:
        if not active:
            resources.repository.release(token)


async def wait_for_imports(importer):
    """Await accepted work without stopping admission in lifecycle fixtures."""
    await asyncio.gather(*tuple(importer._imports), return_exceptions=True)


@asynccontextmanager
async def import_service(resources):
    from context_manager.auth import WriteOwner

    from cm.infrastructure.access.support import authorization

    service = document_importer(resources)
    tenant, workspace, user = resources.repository.target
    scope = service._store.access.issue_write_scope(
        authorization(user, tenant, workspace),
        owner=WriteOwner.USER,
        zone="knowledge",
    )
    try:
        yield service, scope
    finally:
        await service.aclose()


def download_document(resources, document_id):
    document = resources.repository.get(document_id)
    return document, resources.files.read(document.metadata.filename)


def document_deleter(resources):
    from context_manager.knowledge import KnowledgeDeleteService

    from cm.knowledge.support import MemoryGraphStore

    return KnowledgeDeleteService(MemoryGraphStore(), **resources.arguments)


def delete_document(resources, document_id):
    from context_manager.auth import WriteOwner

    from cm.infrastructure.access.support import authorization

    service = document_deleter(resources)
    tenant, workspace, user = resources.repository.target
    scope = service._store.access.issue_write_scope(
        authorization(user, tenant, workspace),
        owner=WriteOwner.USER,
    )
    asyncio.run(service.delete_document(document_id, scope))


@contextmanager
def fail_document_sql(resources: DocumentResources, operation: str):
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(operation) and "cm_kg_documents" in statement:
            raise SQLAlchemyError("private database details")

    event.listen(resources.repository._engine, "before_cursor_execute", fail)
    try:
        yield
    finally:
        event.remove(resources.repository._engine, "before_cursor_execute", fail)

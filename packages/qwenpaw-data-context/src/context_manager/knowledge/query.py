"""Read one user's document catalog and retained originals."""

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import SQLAlchemyError

from ..auth.repository import OwnerRepository
from .document import Document
from .files import DocumentFileStore
from .repository import DocumentRepository


class DocumentQueryService:
    """Keep document reads separate from import task ownership."""

    def __init__(
        self,
        documents: DocumentRepository,
        files: DocumentFileStore,
        *,
        run_sync: Callable[..., Awaitable],
    ) -> None:
        self._documents = documents
        self._files = files
        self._run_sync = run_sync

    async def list(self) -> tuple[Document, ...]:
        """Return the user's catalog with persisted import states."""
        return await self._run_sync(self._documents.list)

    async def download(self, document_id: str) -> tuple[Document, bytes]:
        """Resolve the catalog identity before reading the retained original."""

        def read():
            document = self._documents.get(document_id)
            return document, self._files.read(document.metadata.filename)

        return await self._run_sync(read)

    async def owner_name(self) -> str:
        """Read the current catalog owner's display name, falling back to the ID."""

        def read():
            tenant_id, _, user_id = self._documents.target
            try:
                username = OwnerRepository().get_username(tenant_id, user_id)
            except (SQLAlchemyError, OSError):
                logging.getLogger(__name__).exception(
                    "failed to resolve KG document owner name"
                )
                return user_id
            return username or user_id

        return await self._run_sync(read)

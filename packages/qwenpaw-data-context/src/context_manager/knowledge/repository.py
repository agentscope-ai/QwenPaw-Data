"""Persist user-scoped document records and status in shared SQL."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
import time

from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from context_manager.heartbeat import is_fresh, stale_cutoff
from context_manager.utils.pod import current_pod_name
from ..schema.relational import documents, kg_user_claims as claims
from .document import (
    Document,
    DocumentMetadata,
    DocumentStatus,
    _content_sha256_from_document_id,
)


class DocumentLeaseLost(RuntimeError):
    """The document operation no longer owns its user claim."""


class DocumentRepository:
    """Read and mutate the catalog for one tenant/workspace/user."""

    def __init__(
        self, engine: Engine, *, tenant_id: str, workspace_id: str, user_id: str
    ) -> None:
        self._engine = engine
        self.target = (tenant_id, workspace_id, user_id)
        self._identity = dict(
            zip(("tenant_id", "workspace_id", "user_id"), self.target)
        )
        self._where = tuple(
            documents.c[name] == value for name, value in self._identity.items()
        )
        self._claim_where = tuple(
            claims.c[name] == value for name, value in self._identity.items()
        )

    def claim(self, document_id: str) -> str:
        """One user-level claim. A fresh heartbeat rejects a second graph operation."""
        now = time.time()
        cutoff = stale_cutoff(now)
        insert = sqlite_insert if self._engine.dialect.name == "sqlite" else pg_insert
        with self._engine.begin() as connection:
            epoch = connection.execute(
                insert(claims)
                .values(
                    **self._identity,
                    heartbeat_at=now,
                    executor_pod=current_pod_name() or None,
                    run_epoch=1,
                )
                .on_conflict_do_update(
                    index_elements=list(claims.primary_key.columns),
                    set_={
                        "heartbeat_at": now,
                        "executor_pod": current_pod_name() or None,
                        "run_epoch": claims.c.run_epoch + 1,
                    },
                    where=(
                        claims.c.heartbeat_at.is_(None)
                        | (claims.c.heartbeat_at < cutoff)
                    ),
                )
                .returning(claims.c.run_epoch)
            ).scalar_one_or_none()
            if epoch is None:
                status = connection.execute(
                    select(documents.c.status).where(
                        *self._where,
                        documents.c.document_id == document_id,
                    )
                ).scalar_one_or_none()
                raise FileExistsError(
                    "doc_delete_in_progress"
                    if status == "deleting"
                    else "doc_build_in_progress"
                    if status == "processing"
                    else "doc_operation_in_progress"
                )
            connection.execute(
                update(documents)
                .where(*self._where, documents.c.document_id == document_id)
                .values(run_epoch=int(epoch))
            )
        return str(epoch)

    def renew(self, token: str) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(claims)
                .where(
                    *self._claim_where,
                    claims.c.run_epoch == int(token),
                    claims.c.heartbeat_at.is_not(None),
                )
                .values(heartbeat_at=time.time())
            )
            return result.rowcount == 1

    def release(self, token: str) -> None:
        """Drop this claim. A still-processing document stays for read-time retry."""
        with self._engine.begin() as connection:
            connection.execute(
                update(claims)
                .where(*self._claim_where, claims.c.run_epoch == int(token))
                .values(heartbeat_at=None, executor_pod=None)
            )

    def _require_execution(
        self, connection: Connection, document_id: str, token: str
    ) -> None:
        owned = connection.execute(
            update(claims)
            .where(
                *self._claim_where,
                claims.c.run_epoch == int(token),
                claims.c.heartbeat_at.is_not(None),
            )
            .values(heartbeat_at=time.time())
            .returning(claims.c.run_epoch)
        ).scalar_one_or_none()
        if owned is None:
            raise DocumentLeaseLost("document execution claim was lost")

    def check_execution(self, document_id: str, token: str) -> None:
        with self._engine.begin() as connection:
            self._require_execution(connection, document_id, token)

    def _selection(self):
        return (
            select(
                documents,
                claims.c.heartbeat_at.label("claim_heartbeat_at"),
                claims.c.run_epoch.label("claim_epoch"),
            )
            .select_from(
                documents.outerjoin(
                    claims,
                    and_(
                        claims.c.tenant_id == documents.c.tenant_id,
                        claims.c.workspace_id == documents.c.workspace_id,
                        claims.c.user_id == documents.c.user_id,
                    ),
                )
            )
            .where(*self._where)
        )

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Commit catalog writes together or roll back on caller failure."""
        with self._engine.begin() as connection:
            yield connection

    def create(self, connection: Connection, document: Document, *, token: str) -> None:
        """Insert a filename once in the caller's transaction."""
        details = document.metadata
        self._require_execution(connection, document.document_id, token)
        insert = sqlite_insert if connection.dialect.name == "sqlite" else pg_insert
        result = connection.execute(
            insert(documents)
            .values(
                **self._identity,
                document_id=document.document_id,
                filename=details.filename,
                content_type=details.content_type,
                byte_size=details.byte_size,
                status=details.status.value,
                created_at=details.created_at.astimezone(UTC).isoformat(),
                updated_at=details.updated_at.astimezone(UTC).isoformat(),
                ingest_error=details.ingest_error,
                run_epoch=int(token),
            )
            .on_conflict_do_nothing(
                index_elements=list(documents.primary_key.columns),
            )
            .returning(documents.c.filename)
        )
        if result.scalar_one_or_none() is None:
            raise FileExistsError("document filename already exists for this user")

    def list(self) -> tuple[Document, ...]:
        """Read the catalog in stable creation order."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                self._selection().order_by(
                    documents.c.created_at.desc(),
                    documents.c.filename.desc(),
                )
            ).mappings()
            return tuple(self._document(row) for row in rows)

    def get(self, document_id: str) -> Document:
        """Read a document within this repository's owner scope."""
        _content_sha256_from_document_id(document_id)
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._selection().where(
                        documents.c.document_id == document_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise FileNotFoundError("document does not exist for this user")
        return self._document(row)

    def set_status(
        self,
        document_id: str,
        status: DocumentStatus,
        *,
        token: str,
        error: str | None = None,
    ) -> Document:
        """Update status and error while retaining original metadata."""
        current = self.get(document_id)
        document = replace(
            current,
            metadata=replace(
                current.metadata,
                status=status,
                ingest_error=error,
                updated_at=datetime.now(UTC),
            ),
        )
        with self._engine.begin() as connection:
            self._require_execution(connection, document_id, token)
            result = connection.execute(
                update(documents)
                .where(
                    *self._where,
                    documents.c.document_id == document_id,
                    documents.c.run_epoch == int(token),
                )
                .values(
                    status=status.value,
                    ingest_error=error,
                    updated_at=document.metadata.updated_at.isoformat(),
                )
            )
            if result.rowcount == 0:
                raise FileNotFoundError("document does not exist for this user")
        return document

    def delete(
        self, document_id: str, *, token: str, status: str | None = None
    ) -> None:
        """Remove the catalog row only while it still has this claim and status."""
        conditions = [
            *self._where,
            documents.c.document_id == document_id,
            documents.c.run_epoch == int(token),
        ]
        if status is not None:
            conditions.append(documents.c.status == status)
        with self._engine.begin() as connection:
            self._require_execution(connection, document_id, token)
            connection.execute(delete(documents).where(*conditions))

    @staticmethod
    def _document(row: Mapping[str, object]) -> Document:
        status, error = DocumentStatus(row["status"]), row["ingest_error"]
        heartbeat = row.get("claim_heartbeat_at")
        epoch = row.get("run_epoch")
        claim_epoch = row.get("claim_epoch")
        fresh = (
            heartbeat is not None
            and epoch is not None
            and claim_epoch is not None
            and int(epoch) == int(claim_epoch)
            and is_fresh(heartbeat, now=time.time())
        )
        if status in (DocumentStatus.PROCESSING, DocumentStatus.DELETING) and not fresh:
            if status is DocumentStatus.PROCESSING:
                status = DocumentStatus.FAILED
                error = "document import interrupted; retry import"
            elif not error:
                error = "document deletion interrupted; retry deletion"
        return Document(
            document_id=row["document_id"],
            metadata=DocumentMetadata(
                filename=row["filename"],
                content_type=row["content_type"],
                byte_size=row["byte_size"],
                status=status,
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                ingest_error=error,
            ),
        )

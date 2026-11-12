"""Persist model configuration and usage through the shared SQL engine."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from sqlalchemy import and_, column, inspect, literal_column, select, table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from ..db import CMDatabase, get_database
from ..schema.relational import (
    model_configs,
    model_usages,
    provider_refs,
    providers,
    users,
    workspace_embedding_spaces,
)
from .llm import LlmError
from .usage import ModelUsageEvent

if TYPE_CHECKING:
    from .service import ResolvedModel

ModelChannel = Literal["llm", "embedding"]


class ModelConfigError(LlmError):
    """Report a sanitized model selection or Provider credential failure."""


@dataclass(frozen=True, slots=True)
class ModelConfigRepository:
    """Read and write model configuration in the shared SQL database.

    Persist user selections, Provider references and workspace conventions.
    Return Host Provider credentials in their encrypted storage form.
    """

    tenant_id: str
    user_id: str
    workspace_id: str = field(kw_only=True)

    def read_selections(self) -> dict[str, dict]:
        """Read non-secret model choices for both channels of one user."""
        try:
            with get_database().engine.connect() as conn:
                self._require_user(conn)
                row = (
                    conn.execute(
                        select(model_configs).where(
                            model_configs.c.tenant_id == self.tenant_id,
                            model_configs.c.user_id == self.user_id,
                        )
                    )
                    .mappings()
                    .first()
                    or {}
                )
                refs = dict(
                    conn.execute(
                        select(
                            provider_refs.c.channel, provider_refs.c.provider_id
                        ).where(
                            provider_refs.c.tenant_id == self.tenant_id,
                            provider_refs.c.user_id == self.user_id,
                        )
                    )
                    .tuples()
                    .all()
                )
            return {
                "llm": {
                    "provider_id": refs.get("llm"),
                    "model": row.get("llm_model"),
                    "base_url": row.get("llm_base_url"),
                },
                "embedding": {
                    "provider_id": refs.get("embedding"),
                    "model": row.get("embed_model"),
                    "base_url": row.get("embed_base_url"),
                    "dim": row.get("embed_dim"),
                },
            }
        except SQLAlchemyError as exc:
            raise ModelConfigError("model configuration could not be loaded") from exc

    def read_embedding_space(self) -> dict | None:
        """Read the current workspace convention without changing it."""
        query = select(
            workspace_embedding_spaces.c.embed_model.label("model"),
            workspace_embedding_spaces.c.embed_base_url.label("base_url"),
            workspace_embedding_spaces.c.embed_dim.label("dim"),
        ).where(
            workspace_embedding_spaces.c.tenant_id == self.tenant_id,
            workspace_embedding_spaces.c.workspace_id == self.workspace_id,
        )
        try:
            with get_database().engine.connect() as conn:
                row = conn.execute(query).mappings().first()
            return dict(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise ModelConfigError(
                "workspace embedding convention could not be loaded"
            ) from exc

    def read_model(
        self,
        channel: ModelChannel,
        *,
        selection: dict | None = None,
    ) -> dict | None:
        """Read a Provider, or legacy credentials only when no channel ref exists."""
        prefix = "llm" if channel == "llm" else "embed"
        selected_provider = (
            provider_refs.c.provider_id
            if selection is None
            else selection.get("provider_id")
        )
        # Start from the user so missing choices and missing Providers stay distinguishable.
        source = (
            users.outerjoin(
                model_configs,
                and_(
                    model_configs.c.user_id == users.c.id,
                    model_configs.c.tenant_id == users.c.tenant_id,
                ),
            )
            .outerjoin(
                provider_refs,
                and_(
                    provider_refs.c.user_id == users.c.id,
                    provider_refs.c.tenant_id == users.c.tenant_id,
                    provider_refs.c.channel == channel,
                ),
            )
            .outerjoin(
                providers,
                and_(
                    providers.c.user_id == users.c.id,
                    providers.c.provider_id == selected_provider,
                ),
            )
        )
        try:
            with get_database().engine.connect() as conn:
                row = (
                    conn.execute(
                        select(
                            provider_refs.c.channel.label("bound_channel"),
                            provider_refs.c.provider_id.label("selected_provider"),
                            model_configs.c[f"{prefix}_model"].label("model"),
                            model_configs.c.embed_dim,
                            providers.c.provider_id,
                            providers.c.base_url,
                            providers.c.api_key_enc,
                        )
                        .select_from(source)
                        .where(
                            users.c.id == self.user_id,
                            users.c.tenant_id == self.tenant_id,
                            users.c.status == "active",
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    return None
                result = dict(row)
                result["legacy"] = selection is None and row["bound_channel"] is None
                if result["legacy"]:
                    # Read optional historical columns only for an unbound channel.
                    # A wildcard tolerates new schemas that never had Key columns.
                    legacy = (
                        conn.execute(
                            select(literal_column("*"))
                            .select_from(model_configs)
                            .where(
                                model_configs.c.tenant_id == self.tenant_id,
                                model_configs.c.user_id == self.user_id,
                            )
                        )
                        .mappings()
                        .first()
                        or {}
                    )
                    result.update(
                        base_url=legacy.get(f"{prefix}_base_url"),
                        api_key_enc=legacy.get(f"{prefix}_api_key_enc"),
                    )
                return result
        except SQLAlchemyError as exc:
            raise ModelConfigError("model configuration could not be loaded") from exc

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Commit configuration writes together; roll back if the service rejects them."""
        try:
            with get_database().engine.begin() as conn:
                self._require_user(conn)
                yield conn
        except SQLAlchemyError as exc:
            raise ModelConfigError("model configuration could not be saved") from exc

    def save_selection(
        self,
        connection: Connection,
        channel: ModelChannel,
        config: ResolvedModel,
    ) -> None:
        """Write one channel's model choice and Host Provider reference."""
        prefix = "llm" if channel == "llm" else "embed"
        values = {
            f"{prefix}_model": config.model,
            f"{prefix}_base_url": config.base_url,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": self.user_id,
        }
        if channel == "embedding":
            values["embed_dim"] = config.dim
        insert = sqlite_insert if connection.dialect.name == "sqlite" else pg_insert
        connection.execute(
            insert(model_configs)
            .values(tenant_id=self.tenant_id, user_id=self.user_id, **values)
            .on_conflict_do_update(index_elements=["tenant_id", "user_id"], set_=values)
        )
        connection.execute(
            insert(provider_refs)
            .values(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                channel=channel,
                provider_id=config.provider_id,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "user_id", "channel"],
                set_={"provider_id": config.provider_id},
            )
        )
        # Retire the old credential atomically when this channel adopts a Provider.
        # Do not add legacy columns to newly initialized databases.
        key_column = f"{prefix}_api_key_enc"
        if key_column in {
            item["name"] for item in inspect(connection).get_columns(model_configs.name)
        }:
            legacy = table(
                model_configs.name,
                column("tenant_id"),
                column("user_id"),
                column(key_column),
            )
            connection.execute(
                legacy.update()
                .where(
                    legacy.c.tenant_id == self.tenant_id,
                    legacy.c.user_id == self.user_id,
                )
                .values({key_column: None})
            )

    def adopt_embedding_space(
        self,
        connection: Connection,
        config: ResolvedModel,
    ) -> dict:
        """Insert an absent convention and lock/read the resulting workspace row."""
        insert = sqlite_insert if connection.dialect.name == "sqlite" else pg_insert
        connection.execute(
            insert(workspace_embedding_spaces)
            .values(
                tenant_id=self.tenant_id,
                workspace_id=self.workspace_id,
                embed_model=config.model,
                embed_base_url=config.base_url,
                embed_dim=config.dim,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "workspace_id"])
        )
        row = (
            connection.execute(
                select(workspace_embedding_spaces)
                .where(
                    workspace_embedding_spaces.c.tenant_id == self.tenant_id,
                    workspace_embedding_spaces.c.workspace_id == self.workspace_id,
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        return {
            "model": row["embed_model"],
            "base_url": row["embed_base_url"],
            "dim": row["embed_dim"],
        }

    def update_embedding_space(
        self,
        connection: Connection,
        config: ResolvedModel,
    ) -> None:
        """Update the convention after the service accepts the replacement."""
        connection.execute(
            workspace_embedding_spaces.update()
            .where(
                workspace_embedding_spaces.c.tenant_id == self.tenant_id,
                workspace_embedding_spaces.c.workspace_id == self.workspace_id,
            )
            .values(
                embed_model=config.model,
                embed_base_url=config.base_url,
                embed_dim=config.dim,
            )
        )

    def delete(self) -> None:
        """Delete personal choices and references, preserving Host Provider records."""
        try:
            with get_database().engine.begin() as conn:
                self._require_user(conn)
                for table in (provider_refs, model_configs):
                    conn.execute(
                        table.delete().where(
                            table.c.tenant_id == self.tenant_id,
                            table.c.user_id == self.user_id,
                        )
                    )
        except SQLAlchemyError as exc:
            raise ModelConfigError("model configuration could not be deleted") from exc

    def _require_user(self, conn: Connection) -> None:
        """Check tenant ownership before accessing the user's Provider records."""
        row = conn.execute(
            select(users.c.id).where(
                users.c.id == self.user_id,
                users.c.tenant_id == self.tenant_id,
                users.c.status == "active",
            )
        ).first()
        if row is None:
            raise ModelConfigError(
                "model configuration user is unavailable in this tenant"
            )


class ModelUsageRepository:
    """Borrow independent transactions without owning or creating Host tables."""

    def probe(self) -> None:
        with get_database().engine.connect() as connection:
            CMDatabase.validate_connection(connection, (model_usages,))

    def write(self, events: Sequence[ModelUsageEvent]) -> None:
        if not events:
            return
        rows = [asdict(event) for event in events]
        with get_database().engine.begin() as connection:
            insert = sqlite_insert if connection.dialect.name == "sqlite" else pg_insert
            connection.execute(
                insert(model_usages).on_conflict_do_nothing(index_elements=["id"]),
                rows,
            )

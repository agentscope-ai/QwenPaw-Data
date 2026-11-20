"""Define resolved relational datasource records and stable read failures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..db import get_database
from ..schema.relational import datasource
from ..utils.immutable import freeze_mapping
from ..utils.validation import require_trimmed_text
from .config import (
    MYSQL_PROTOCOL_BACKENDS,
    BigQueryConfig,
    DuckDbConfig,
    MySqlConfig,
    OdpsConfig,
    PostgresConfig,
    RdbBackend,
    RdbTarget,
    SqliteConfig,
)

_RDB_BACKEND_ALIASES = {
    "postgresql": RdbBackend.POSTGRES,
    "postgres": RdbBackend.POSTGRES,
    "holo": RdbBackend.HOLOGRES,
    "hologres": RdbBackend.HOLOGRES,
    "mysql": RdbBackend.MYSQL,
    "starrocks": RdbBackend.STARROCKS,
    "doris": RdbBackend.DORIS,
    "tidb": RdbBackend.TIDB,
    "odps": RdbBackend.ODPS,
    "bigquery": RdbBackend.BIGQUERY,
    "sqlite": RdbBackend.SQLITE,
    "duckdb": RdbBackend.DUCKDB,
}


class DatasourceErrorCode(StrEnum):
    """Classify datasource failures without backend or secret details."""

    NOT_FOUND = "not_found"
    SCOPE_MISMATCH = "scope_mismatch"
    INVALID_RECORD = "invalid_record"


class DatasourceError(RuntimeError):
    """Report one stable read or connection-target failure."""

    def __init__(
        self,
        message: str,
        *,
        code: DatasourceErrorCode,
        datasource_id: str,
    ) -> None:
        """Create one error without connection or credential values."""
        super().__init__(message)
        self.code = code
        self.datasource_id = datasource_id


@dataclass(frozen=True, slots=True)
class Datasource:
    """Describe one scoped relational datasource with its current connection config."""

    tenant_id: str
    workspace_id: str
    datasource_id: str
    datasource_name: str
    datasource_type: str
    config: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        """Validate route metadata and detach the connection config mapping."""
        for field_name in (
            "tenant_id",
            "workspace_id",
            "datasource_id",
            "datasource_name",
            "datasource_type",
        ):
            object.__setattr__(
                self,
                field_name,
                require_trimmed_text(getattr(self, field_name), field_name),
            )
        if self.datasource_type.lower() not in _RDB_BACKEND_ALIASES:
            raise ValueError("datasource_type must identify a supported RDB backend")
        if not isinstance(self.config, Mapping) or not self.config:
            raise ValueError("config must be a non-empty mapping")
        object.__setattr__(
            self,
            "config",
            freeze_mapping(
                self.config,
                field_name="config",
                key_field_name="config property",
            ),
        )

    @property
    def backend(self) -> RdbBackend:
        """Identify the execution backend before resolving connection credentials."""
        return _RDB_BACKEND_ALIASES[self.datasource_type.lower()]

    def to_rdb_target(
        self,
        *,
        user_id: str | None = None,
        runtime_project: str | None = None,
    ) -> RdbTarget:
        """Build one trusted connection target from this relational datasource.

        Saved configurations fully determine the target. ``user_id`` and
        ``runtime_project`` remain accepted for existing callers and are ignored.
        """
        backend = self.backend
        values = self.config
        try:
            if backend in (RdbBackend.POSTGRES, RdbBackend.HOLOGRES):
                config = PostgresConfig(
                    host=values["host"],
                    port=values.get("port", 5432),
                    # 0.3 PostgreSQL configs stored the psycopg key "dbname".
                    database=values["database"]
                    if "database" in values
                    else values["dbname"],
                    user=values["user"],
                    password=values["password"],
                    backend=backend,
                )
            elif backend in MYSQL_PROTOCOL_BACKENDS:
                config = MySqlConfig(
                    host=values["host"],
                    port=values.get("port", 3306),
                    database=values["database"],
                    user=values["user"],
                    password=values["password"],
                    backend=backend,
                )
            elif backend is RdbBackend.ODPS:
                config = OdpsConfig(
                    project=values["project"],
                    endpoint=values["endpoint"],
                    access_key_id=values["access_key_id"],
                    access_key_secret=values["access_key_secret"],
                    sts_token=values.get("sts_token"),
                )
            elif backend is RdbBackend.BIGQUERY:
                config = BigQueryConfig(
                    project=values["project"],
                    credentials_json=values["credentials_json"],
                    location=values.get("location"),
                    maximum_bytes_billed=values.get("maximum_bytes_billed"),
                )
            elif backend is RdbBackend.DUCKDB:
                config = DuckDbConfig(path=values["path"])
            else:
                config = SqliteConfig(
                    path=values["path"],
                    read_only=values.get("read_only", True),
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise DatasourceError(
                "Datasource connection metadata is invalid",
                code=DatasourceErrorCode.INVALID_RECORD,
                datasource_id=self.datasource_id,
            ) from exc
        return RdbTarget(
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            datasource_id=self.datasource_id,
            config=config,
        )


class DatasourceRepository:
    """Read active datasource records from the shared tenant/workspace catalog."""

    def get(
        self,
        datasource_id: str,
        *,
        tenant_id: str,
        workspace_id: str,
    ) -> Datasource:
        try:
            with get_database().engine.connect() as connection:
                row = (
                    connection.execute(
                        select(
                            datasource.c.datasource_name,
                            datasource.c.datasource_type,
                            datasource.c.config,
                        ).where(
                            datasource.c.tenant_id == tenant_id,
                            datasource.c.workspace_id == workspace_id,
                            datasource.c.datasource_id == datasource_id,
                            datasource.c.is_deleted == 0,
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                raise DatasourceError(
                    "Datasource configuration was not found",
                    code=DatasourceErrorCode.NOT_FOUND,
                    datasource_id=datasource_id,
                )
            return Datasource(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                datasource_id=datasource_id,
                datasource_name=row["datasource_name"],
                datasource_type=row["datasource_type"],
                config=json.loads(row["config"]),
            )
        except (SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            raise DatasourceError(
                "Datasource configuration could not be read",
                code=DatasourceErrorCode.INVALID_RECORD,
                datasource_id=datasource_id,
            ) from exc


__all__ = [
    "Datasource",
    "DatasourceRepository",
    "DatasourceError",
    "DatasourceErrorCode",
]

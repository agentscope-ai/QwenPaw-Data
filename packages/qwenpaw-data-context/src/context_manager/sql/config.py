"""Define validated relational connection configuration and routing identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..utils.validation import (
    require_integer,
    require_nonempty_text,
    require_trimmed_text,
)


class RdbBackend(StrEnum):
    """Name the relational protocols supported by Context Manager."""

    POSTGRES = "postgres"
    HOLOGRES = "hologres"
    MYSQL = "mysql"
    STARROCKS = "starrocks"
    DORIS = "doris"
    TIDB = "tidb"
    ODPS = "odps"
    BIGQUERY = "bigquery"
    SQLITE = "sqlite"
    DUCKDB = "duckdb"


MYSQL_PROTOCOL_BACKENDS = (
    RdbBackend.MYSQL,
    RdbBackend.STARROCKS,
    RdbBackend.DORIS,
    RdbBackend.TIDB,
)


class PostgresSslMode(StrEnum):
    """Expose the finite libpq TLS modes accepted by this layer."""

    DISABLE = "disable"
    PREFER = "prefer"
    REQUIRE = "require"
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"


class MySqlSslMode(StrEnum):
    """Expose the finite PyMySQL TLS modes accepted by this layer."""

    DISABLED = "disabled"
    PREFERRED = "preferred"
    REQUIRED = "required"
    VERIFY_CA = "verify-ca"
    VERIFY_IDENTITY = "verify-identity"


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    """Configure PostgreSQL or PostgreSQL-compatible Hologres transport."""

    host: str
    database: str
    user: str
    password: str = field(repr=False)
    port: int = 5432
    connect_timeout: int = 5
    ssl_mode: PostgresSslMode = PostgresSslMode.PREFER
    ssl_root_cert_file: str | None = None
    backend: RdbBackend = RdbBackend.POSTGRES

    def __post_init__(self) -> None:
        """Validate credentials and protocol-specific options."""
        object.__setattr__(self, "host", require_trimmed_text(self.host, "host"))
        object.__setattr__(
            self,
            "database",
            require_trimmed_text(self.database, "database"),
        )
        object.__setattr__(self, "user", require_trimmed_text(self.user, "user"))
        if not isinstance(self.password, str):
            raise TypeError("password must be text")
        if not self.password:
            raise ValueError("password must be non-empty text")
        object.__setattr__(
            self,
            "port",
            require_integer(self.port, "port", minimum=1, maximum=65535),
        )
        object.__setattr__(
            self,
            "connect_timeout",
            require_integer(
                self.connect_timeout,
                "connect_timeout",
                minimum=1,
                maximum=60,
            ),
        )
        if type(self.ssl_mode) is not PostgresSslMode:
            raise TypeError("ssl_mode must be a PostgresSslMode")
        if self.ssl_root_cert_file is not None:
            object.__setattr__(
                self,
                "ssl_root_cert_file",
                require_trimmed_text(
                    self.ssl_root_cert_file,
                    "ssl_root_cert_file",
                ),
            )
            if self.ssl_mode not in (
                PostgresSslMode.VERIFY_CA,
                PostgresSslMode.VERIFY_FULL,
            ):
                raise ValueError("ssl_root_cert_file requires a verifying ssl_mode")
        if type(self.backend) is not RdbBackend:
            raise TypeError("backend must be an RdbBackend")
        if self.backend not in (RdbBackend.POSTGRES, RdbBackend.HOLOGRES):
            raise ValueError("PostgresConfig backend must be postgres or hologres")


@dataclass(frozen=True, slots=True)
class MySqlConfig:
    """Configure one MySQL-protocol connection without arbitrary driver options.

    StarRocks, Doris and TiDB share the MySQL wire protocol and driver; the
    backend keeps their dialect and session-timeout behavior distinct.
    """

    host: str
    database: str
    user: str
    password: str = field(repr=False)
    port: int = 3306
    connect_timeout: int = 5
    charset: str = "utf8mb4"
    ssl_mode: MySqlSslMode = MySqlSslMode.PREFERRED
    ssl_ca_file: str | None = None
    backend: RdbBackend = RdbBackend.MYSQL
    read_timeout: int | None = None

    def __post_init__(self) -> None:
        """Validate credentials and protocol-specific options."""
        object.__setattr__(self, "host", require_trimmed_text(self.host, "host"))
        object.__setattr__(
            self,
            "database",
            require_trimmed_text(self.database, "database"),
        )
        object.__setattr__(self, "user", require_trimmed_text(self.user, "user"))
        if not isinstance(self.password, str):
            raise TypeError("password must be text")
        if not self.password:
            raise ValueError("password must be non-empty text")
        object.__setattr__(
            self,
            "port",
            require_integer(self.port, "port", minimum=1, maximum=65535),
        )
        object.__setattr__(
            self,
            "connect_timeout",
            require_integer(
                self.connect_timeout,
                "connect_timeout",
                minimum=1,
                maximum=60,
            ),
        )
        object.__setattr__(
            self,
            "charset",
            require_trimmed_text(self.charset, "charset"),
        )
        if type(self.ssl_mode) is not MySqlSslMode:
            raise TypeError("ssl_mode must be a MySqlSslMode")
        if self.ssl_ca_file is not None:
            object.__setattr__(
                self,
                "ssl_ca_file",
                require_trimmed_text(self.ssl_ca_file, "ssl_ca_file"),
            )
            if self.ssl_mode not in (
                MySqlSslMode.VERIFY_CA,
                MySqlSslMode.VERIFY_IDENTITY,
            ):
                raise ValueError("ssl_ca_file requires a verifying ssl_mode")
        if type(self.backend) is not RdbBackend:
            raise TypeError("backend must be an RdbBackend")
        if self.backend not in MYSQL_PROTOCOL_BACKENDS:
            raise ValueError(
                "MySqlConfig backend must be mysql, starrocks, doris or tidb"
            )
        if self.read_timeout is not None:
            object.__setattr__(
                self,
                "read_timeout",
                require_integer(
                    self.read_timeout, "read_timeout", minimum=1, maximum=3600
                ),
            )


@dataclass(frozen=True, slots=True)
class OdpsConfig:
    """Configure ODPS with an AccessKey pair or short-lived STS credentials."""

    project: str
    endpoint: str
    access_key_id: str = field(repr=False)
    access_key_secret: str = field(repr=False)
    sts_token: str | None = field(default=None, repr=False)
    backend: RdbBackend = RdbBackend.ODPS

    def __post_init__(self) -> None:
        for name in ("project", "endpoint", "access_key_id", "access_key_secret"):
            require_trimmed_text(getattr(self, name), name)
        if self.backend is not RdbBackend.ODPS:
            raise ValueError("OdpsConfig backend must be odps")


@dataclass(frozen=True, slots=True)
class BigQueryConfig:
    """Configure a billing project and service-account credentials."""

    project: str
    credentials_json: str = field(repr=False)
    location: str | None = None
    maximum_bytes_billed: int | None = None

    def __post_init__(self) -> None:
        require_trimmed_text(self.project, "project")
        require_nonempty_text(self.credentials_json, "credentials_json")
        if self.maximum_bytes_billed is not None:
            if (
                type(self.maximum_bytes_billed) is not int
                or self.maximum_bytes_billed < 1
            ):
                raise ValueError("maximum_bytes_billed must be a positive integer")


@dataclass(frozen=True, slots=True)
class SqliteConfig:
    """Open an existing server-local SQLite file."""

    path: str
    read_only: bool = True
    connect_timeout: int = 5

    def __post_init__(self) -> None:
        require_trimmed_text(self.path, "path")
        if not isinstance(self.read_only, bool):
            raise TypeError("read_only must be a boolean")
        require_integer(self.connect_timeout, "connect_timeout", minimum=1, maximum=60)


@dataclass(frozen=True, slots=True)
class DuckDbConfig:
    """Open an existing server-local DuckDB file in read-only mode."""

    path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", require_trimmed_text(self.path, "path"))


ConnectionConfig = (
    PostgresConfig
    | MySqlConfig
    | OdpsConfig
    | BigQueryConfig
    | SqliteConfig
    | DuckDbConfig
)


@dataclass(frozen=True, slots=True)
class RdbTarget:
    """Bind trusted connection configuration to one scoped datasource route."""

    tenant_id: str
    workspace_id: str
    datasource_id: str
    config: ConnectionConfig

    def __post_init__(self) -> None:
        """Validate route identity and the exact supported config types."""
        object.__setattr__(
            self,
            "tenant_id",
            require_trimmed_text(self.tenant_id, "target tenant_id"),
        )
        object.__setattr__(
            self,
            "workspace_id",
            require_trimmed_text(self.workspace_id, "target workspace_id"),
        )
        object.__setattr__(
            self,
            "datasource_id",
            require_trimmed_text(self.datasource_id, "target datasource_id"),
        )
        if type(self.config) not in (
            PostgresConfig,
            MySqlConfig,
            OdpsConfig,
            BigQueryConfig,
            SqliteConfig,
            DuckDbConfig,
        ):
            raise TypeError("target config must be a supported connection config")

    @property
    def backend(self) -> RdbBackend:
        """Return the effective backend without a registry lookup."""
        if isinstance(self.config, (PostgresConfig, MySqlConfig, OdpsConfig)):
            return self.config.backend
        return {
            BigQueryConfig: RdbBackend.BIGQUERY,
            SqliteConfig: RdbBackend.SQLITE,
            DuckDbConfig: RdbBackend.DUCKDB,
        }[type(self.config)]


__all__ = [
    "BigQueryConfig",
    "ConnectionConfig",
    "DuckDbConfig",
    "MYSQL_PROTOCOL_BACKENDS",
    "MySqlConfig",
    "MySqlSslMode",
    "OdpsConfig",
    "PostgresConfig",
    "PostgresSslMode",
    "RdbBackend",
    "RdbTarget",
    "SqliteConfig",
]

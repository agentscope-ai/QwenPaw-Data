"""PostgreSQL / Hologres Adapter — 复用 ``physical.reflect_postgres_records``。"""
from __future__ import annotations

from typing import Optional, Sequence

from ...contracts.import_models import SourceConfig
from ...secrets.schemas import HologresConnection, PostgresConnection
from ...utils import get_logger
from ..keys import DEFAULT_SCHEMA
from ..physical import (
    ColumnRecord,
    TableRecord,
    reflect_postgres_records,
)
from ..profile import DatasetProfile
from .base import ConnectionTestResult, PhysicalManifest

log = get_logger("graph.adapters.postgres")


class PostgresAdapter:
    """通过 psycopg 连接 PostgreSQL / Hologres，抽取表/列/FK 元数据。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        db_id: str,
        profile: Optional[DatasetProfile] = None,
    ):
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        self._db_id = db_id
        self._profile = profile

    @classmethod
    def from_config(cls, config: SourceConfig, db_id: str) -> "PostgresAdapter":
        conn = config.connection
        assert isinstance(conn, (PostgresConnection, HologresConnection)), (
            f"postgres adapter requires PostgresConnection/HologresConnection, "
            f"got {type(conn).__name__}"
        )
        password = conn.password.get_secret_value()

        return cls(
            host=conn.host,
            port=conn.port,
            database=conn.database,
            user=conn.user,
            password=password,
            db_id=db_id,
        )

    def test_connection(self) -> ConnectionTestResult:
        try:
            import psycopg

            with psycopg.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                dbname=self._database,
                connect_timeout=5,
            ) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
                n = cur.fetchone()[0]
            return ConnectionTestResult(
                success=True,
                message=f"connected to {self._host}:{self._port}/{self._database}",
                tables_found=n,
            )
        except Exception as exc:
            return ConnectionTestResult(success=False, message=str(exc))

    def extract_metadata(self, schemas: Sequence[str]) -> PhysicalManifest:
        schema = schemas[0] if schemas else DEFAULT_SCHEMA
        db_id, sch, tables, columns, fks = reflect_postgres_records(
            db_id=self._db_id,
            schema=schema,
            profile=self._profile,
        )
        return PhysicalManifest(
            db_id=db_id,
            schema=sch,
            tables=tables,
            columns=columns,
            fks=fks,
        )

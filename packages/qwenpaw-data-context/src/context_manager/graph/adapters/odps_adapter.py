"""ODPS (MaxCompute) Adapter — 通过 PyODPS 反射表/列元数据。"""
from __future__ import annotations

from typing import Optional, Sequence

from ...contracts.import_models import SourceConfig
from ...secrets.schemas import OdpsConnection
from ...utils import get_logger
from ..keys import column_key, derive_layer, table_key
from ..physical import ColumnRecord, TableRecord
from .base import ConnectionTestResult, PhysicalManifest

log = get_logger("graph.adapters.odps")

_ODPS_TYPE_MAP = {
    "BIGINT": "bigint",
    "INT": "int",
    "SMALLINT": "smallint",
    "TINYINT": "tinyint",
    "DOUBLE": "double",
    "FLOAT": "float",
    "DECIMAL": "decimal",
    "STRING": "text",
    "VARCHAR": "varchar",
    "CHAR": "char",
    "BOOLEAN": "boolean",
    "DATETIME": "datetime",
    "TIMESTAMP": "timestamp",
    "DATE": "date",
    "BINARY": "binary",
    "ARRAY": "array",
    "MAP": "map",
    "STRUCT": "struct",
    "JSON": "json",
}


class OdpsAdapter:
    """通过 PyODPS SDK 连接 MaxCompute，抽取表/列元数据。"""

    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        project: str,
        endpoint: str,
        db_id: str,
    ):
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._project = project
        self._endpoint = endpoint
        self._db_id = db_id

    @classmethod
    def from_config(cls, config: SourceConfig, db_id: str) -> "OdpsAdapter":
        conn = config.connection
        assert isinstance(conn, OdpsConnection), (
            f"odps adapter requires OdpsConnection, got {type(conn).__name__}"
        )
        ak = conn.access_key_id.get_secret_value()
        sk = conn.access_key_secret.get_secret_value()
        return cls(
            access_key_id=ak,
            access_key_secret=sk,
            project=conn.project,
            endpoint=conn.endpoint,
            db_id=db_id,
        )

    def _get_odps(self):
        from odps import ODPS

        return ODPS(
            self._access_key_id,
            self._access_key_secret,
            project=self._project,
            endpoint=self._endpoint,
        )

    def test_connection(self) -> ConnectionTestResult:
        try:
            o = self._get_odps()
            tables = list(o.list_tables(prefix="", max_items=10))
            return ConnectionTestResult(
                success=True,
                message=f"connected to ODPS project {self._project}",
                tables_found=len(tables),
            )
        except Exception as exc:
            return ConnectionTestResult(success=False, message=str(exc))

    def extract_metadata(self, schemas: Sequence[str]) -> PhysicalManifest:
        o = self._get_odps()
        schema = schemas[0] if schemas else "default"

        tables: list[TableRecord] = []
        columns: list[ColumnRecord] = []

        for tbl in o.list_tables():
            tbl.reload()
            tbl_name = tbl.name
            layer = derive_layer(tbl_name)
            t_key = table_key(self._db_id, schema, tbl_name)

            tables.append(TableRecord(
                name=tbl_name,
                key=t_key,
                db_id=self._db_id,
                schema=schema,
                layer=layer,
                comment=tbl.comment or "",
                row_count=None,
            ))

            for col in tbl.table_schema.columns:
                col_type_raw = str(col.type).upper().split("(")[0].split("<")[0]
                col_type = _ODPS_TYPE_MAP.get(col_type_raw, col_type_raw.lower())
                c_key = column_key(self._db_id, schema, tbl_name, col.name)

                columns.append(ColumnRecord(
                    name=col.name,
                    key=c_key,
                    table_key=t_key,
                    data_type=col_type,
                    is_primary=False,
                    is_nullable=col.nullable if hasattr(col, "nullable") else True,
                    is_partition=col.name in [p.name for p in (tbl.table_schema.partitions or [])],
                    comment=col.comment or "",
                    ordinal=0,
                ))

            # partition columns
            for pcol in (tbl.table_schema.partitions or []):
                if any(c.name == pcol.name for c in columns if c.table_key == t_key):
                    continue
                col_type_raw = str(pcol.type).upper().split("(")[0].split("<")[0]
                col_type = _ODPS_TYPE_MAP.get(col_type_raw, col_type_raw.lower())
                c_key = column_key(self._db_id, schema, tbl_name, pcol.name)
                columns.append(ColumnRecord(
                    name=pcol.name,
                    key=c_key,
                    table_key=t_key,
                    data_type=col_type,
                    is_primary=False,
                    is_nullable=True,
                    is_partition=True,
                    comment=pcol.comment or "",
                    ordinal=0,
                ))

        log.info(
            "ODPS extract_metadata: project=%s tables=%d columns=%d",
            self._project, len(tables), len(columns),
        )
        return PhysicalManifest(
            db_id=self._db_id,
            schema=schema,
            tables=tables,
            columns=columns,
            fks=[],
        )

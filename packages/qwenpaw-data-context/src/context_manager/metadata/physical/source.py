"""Read source catalogs and samples through existing RDB connections."""

import logging
from contextlib import closing
from dataclasses import dataclass
from time import monotonic
from types import SimpleNamespace

from ...sql import RdbBackend
from ...settings import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhysicalTable:
    name: str
    schema: str


@dataclass(frozen=True)
class PhysicalColumn:
    table: str
    name: str
    type: str
    pk: bool = False
    nullable: bool = True
    comment: str = ""


@dataclass(frozen=True)
class PhysicalManifest:
    db_id: str
    tables: tuple[PhysicalTable, ...]
    columns: tuple[PhysicalColumn, ...]


class PhysicalMappingSource:
    """Adapt source catalogs and sampling to the existing autoexplore pipeline.

    Each blocking operation opens and closes its connection on the worker thread.
    Only metadata is cached; connections and credentials remain owned by RDB and
    DatasourceRepository. The ten-minute cache follows develop's discovery behavior.
    """

    def __init__(self, connector, target, cache, cache_key, *, force_refresh=False):
        self._connector = connector
        self._target = target
        self._cache = cache
        self._cache_key = cache_key
        self._force_refresh = force_refresh
        self._settings = get_settings().metadata

    def extract_metadata(self, schemas):
        key = (*self._cache_key, tuple(schemas))
        cached = self._cache.get(key)
        if (
            not self._force_refresh
            and cached
            and monotonic() - cached[0] < self._settings.cache_ttl_seconds
        ):
            return cached[1]
        with self._connector.connect(self._target) as connection:
            if self._target.backend is RdbBackend.BIGQUERY:
                manifest = self._bigquery(connection.native_client, schemas)
            elif self._target.backend is RdbBackend.ODPS:
                manifest = self._odps(connection.native_client, schemas)
            else:
                manifest = self._relational(connection, schemas)
        self._cache[key] = (monotonic(), manifest)
        return manifest

    def execute_sql(self, sql, *, max_rows):
        # Develop's sampling helper prefixes this PG-only setting. RDB applies
        # the same deadline itself, so send only the generated SELECT to query().
        sql = sql.removeprefix("SET statement_timeout = '3000'; ")
        with self._connector.connect(self._target) as connection:
            _, rows, _ = connection.query(
                sql,
                max_rows=max_rows,
                timeout_ms=self._settings.sample_timeout_ms,
            )
        return SimpleNamespace(rows=rows[:max_rows], error=None)

    @staticmethod
    def _rows(connection, sql, parameters=()):
        with connection.transaction(), closing(connection.cursor()) as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def _relational(self, connection, schemas):
        backend = self._target.backend
        if backend is RdbBackend.SQLITE:
            schema = "main"
            tables = self._rows(
                connection,
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name",
            )
        elif backend is RdbBackend.MYSQL:
            schema = schemas[0] if schemas else self._target.config.database
            tables = self._rows(
                connection,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
                (schema,),
            )
        else:
            schema = schemas[0] if schemas else "public"
            tables = self._rows(
                connection,
                "SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v') ORDER BY c.relname",
                (schema,),
            )
        table_records, columns = [], []
        for (name,) in tables:
            table_records.append(PhysicalTable(name, schema))
            if backend is RdbBackend.SQLITE:
                quoted = name.replace('"', '""')
                rows = self._rows(connection, f'PRAGMA main.table_xinfo("{quoted}")')
                columns.extend(
                    PhysicalColumn(
                        name,
                        row[1],
                        (row[2] or "text").lower(),
                        bool(row[5]),
                        not bool(row[3]),
                    )
                    for row in rows
                    if row[6] != 1
                )
            elif backend is RdbBackend.MYSQL:
                rows = self._rows(
                    connection,
                    "SELECT column_name, column_type, column_key, is_nullable, column_comment FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    (schema, name),
                )
                columns.extend(
                    PhysicalColumn(
                        name,
                        col,
                        dtype.lower(),
                        pk == "PRI",
                        nullable == "YES",
                        (comment or "").strip(),
                    )
                    for col, dtype, pk, nullable, comment in rows
                )
            else:
                rows = self._rows(
                    connection,
                    """
                    SELECT a.attname, format_type(a.atttypid, a.atttypmod),
                           EXISTS (SELECT 1 FROM pg_catalog.pg_index i WHERE i.indrelid = c.oid AND i.indisprimary AND a.attnum = ANY(i.indkey)),
                           NOT a.attnotnull, col_description(c.oid, a.attnum)
                    FROM pg_catalog.pg_attribute a
                    JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
                    ORDER BY a.attnum
                """,
                    (schema, name),
                )
                columns.extend(
                    PhysicalColumn(
                        name,
                        col,
                        dtype.lower().replace("character varying", "varchar"),
                        pk,
                        nullable,
                        (comment or "").strip(),
                    )
                    for col, dtype, pk, nullable, comment in rows
                )
        return PhysicalManifest(
            self._target.datasource_id, tuple(table_records), tuple(columns)
        )

    def _odps(self, client, schemas):
        tables, columns = [], []
        schema = schemas[0] if schemas else "default"
        renames = {"string": "text"}
        for table in client.list_tables():
            try:
                table.reload()
                physical_columns = []
                for column in table.table_schema.columns:
                    dtype = str(column.type or "text").strip().lower()
                    physical_columns.append(
                        PhysicalColumn(
                            table.name,
                            column.name,
                            renames.get(dtype, dtype),
                            nullable=bool(getattr(column, "nullable", True)),
                            comment=(column.comment or "").strip(),
                        )
                    )
                for column in table.table_schema.partitions:
                    dtype = str(column.type or "text").strip().lower()
                    physical_columns.append(
                        PhysicalColumn(
                            table.name,
                            column.name,
                            renames.get(dtype, dtype),
                            comment=(column.comment or "").strip(),
                        )
                    )
            except Exception:  # noqa: BLE001 -- develop skips per-table SDK failures.
                # Match develop: an inaccessible table does not discard other tables.
                log.warning(
                    "Physical discovery skipped inaccessible ODPS table %s", table.name
                )
                continue
            tables.append(PhysicalTable(table.name, schema))
            columns.extend(physical_columns)
        return PhysicalManifest(
            self._target.datasource_id, tuple(tables), tuple(columns)
        )

    def _bigquery(self, client, schemas):
        from google.cloud.bigquery import DatasetReference

        tables, columns = [], []
        refs = schemas or [dataset.dataset_id for dataset in client.list_datasets()]
        renames = {
            "integer": "int64",
            "float": "float64",
            "boolean": "bool",
            "record": "struct",
        }

        def fields(table, values, prefix=""):
            for field in values:
                name = prefix + field.name
                dtype = field.field_type.lower()
                dtype = renames.get(dtype, dtype)
                if field.mode == "REPEATED":
                    dtype = f"array<{dtype}>"
                columns.append(
                    PhysicalColumn(
                        table,
                        name,
                        dtype,
                        nullable=field.mode != "REQUIRED",
                        comment=(field.description or "").strip(),
                    )
                )
                if field.field_type.upper() in ("RECORD", "STRUCT"):
                    fields(table, field.fields, name + ".")

        for ref in refs:
            project, dataset = (
                ref.split(".", 1) if "." in ref else (self._target.config.project, ref)
            )
            for item in client.list_tables(DatasetReference(project, dataset)):
                table = client.get_table(item.reference)
                tables.append(PhysicalTable(table.table_id, dataset))
                fields(table.table_id, table.schema)
        return PhysicalManifest(
            self._target.datasource_id, tuple(tables), tuple(columns)
        )

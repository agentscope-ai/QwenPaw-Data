"""UniversalConnector — generic Template-Method adapter for pure SQL sources;
subclasses override the differing reflection hooks."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional, Sequence

from pydantic import BaseModel

from ...ingest import FKInfo
from ...utils import get_logger
from ..keys import DEFAULT_SCHEMA, column_key, derive_layer, table_key
from ..physical import ColumnRecord, TableRecord
from .base import (
    BaseConnector,
    ConnectionTestResult,
    ConnectorError,
    ExecResult,
    PhysicalManifest,
)

if TYPE_CHECKING:
    from ...contracts.import_models import SourceConfig

log = get_logger("graph.adapters.universal")

#: Candidate partition-key column names.
_PARTITION_CANDIDATES = ("ds", "dt", "stat_date", "data_date")


def _detect_partition_key(col_names: Sequence[str]) -> Optional[str]:
    by_lower = {c.lower(): c for c in col_names}
    for cand in _PARTITION_CANDIDATES:
        if cand in by_lower:
            return by_lower[cand]
    return None


class UniversalConnector(BaseConnector):
    """Generic adapter for pure SQL sources: ``inspect`` + ``text()`` + ``SELECT 1``."""

    #: Subclasses must bind their Connection model (the single source of truth
    #: of the form schema).
    connection_model: type[BaseModel] = BaseModel

    def __init__(self, conn: BaseModel, *, db_id: str):
        super().__init__(db_id=db_id)
        self._conn = conn
        self._engine = self._create_engine(conn)

    # ------------------------------------------------------------------ #
    # Construction entries
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, config: "SourceConfig", db_id: str) -> "UniversalConnector":
        conn = getattr(config, "connection", None)
        if not isinstance(conn, cls.connection_model):
            raise ConnectorError(
                f"{cls.__name__} 需要 {cls.connection_model.__name__} 连接配置，"
                f"got {type(conn).__name__}"
            )
        return cls(conn, db_id=db_id)

    @classmethod
    def from_connection(cls, conn: BaseModel, db_id: str) -> "UniversalConnector":
        if not isinstance(conn, cls.connection_model):
            raise ConnectorError(
                f"{cls.__name__} 需要 {cls.connection_model.__name__} 连接配置，"
                f"got {type(conn).__name__}"
            )
        return cls(conn, db_id=db_id)

    # ------------------------------------------------------------------ #
    # Subclass override points
    # ------------------------------------------------------------------ #
    def _engine_url(self, conn: BaseModel):
        """Build the SQLAlchemy URL (must override; use ``URL.create`` for
        password safety)."""
        raise NotImplementedError

    def _connect_args(self) -> dict[str, Any]:
        """Driver-level connect_args (e.g. ``connect_timeout``)."""
        return {}

    def _resolve_schema(self, schemas: Sequence[str]) -> Optional[str]:
        """Resolve the port-level *schemas* argument to the inspect target."""
        return schemas[0] if schemas else None

    def _schema_label(self, resolved: Optional[str]) -> str:
        """The name written to graph-node ``schema``."""
        return resolved or DEFAULT_SCHEMA

    def _load_column_comments(
        self, conn, schema: Optional[str], tables: Sequence[str]
    ) -> dict[tuple[str, str], str]:
        """Bulk column-comment hook; dialects without comments in
        ``get_columns`` override with a catalog query."""
        return {}

    def _post_reflect(
        self,
        schema: Optional[str],
        schema_label: str,
        tables: list[TableRecord],
        columns: list[ColumnRecord],
    ) -> None:
        """Post-reflection enrichment hook (no-op by default)."""
        return None

    # ------------------------------------------------------------------ #
    # Engine lifecycle
    # ------------------------------------------------------------------ #
    def _create_engine(self, conn: BaseModel):
        from sqlalchemy import create_engine

        return create_engine(
            self._engine_url(conn),
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=self._connect_args(),
        )

    def close(self) -> None:
        """Close every connection held by the engine pool."""
        try:
            self._engine.dispose()
        except Exception as exc:  # noqa: BLE001
            log.warning("engine dispose failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Port: test_connection (never raises; failure is an expected outcome)
    # ------------------------------------------------------------------ #
    def test_connection(self) -> ConnectionTestResult:
        from sqlalchemy import inspect, text

        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                insp = inspect(conn)
                n = len(self._list_tables(insp, self._resolve_schema([])))
            url = self._engine.url.render_as_string(hide_password=True)
            return ConnectionTestResult(
                success=True,
                message=f"connected: {url}",
                tables_found=n,
            )
        except Exception as exc:  # noqa: BLE001 — connection failure is expected
            return ConnectionTestResult(success=False, message=str(exc))

    # ------------------------------------------------------------------ #
    # Port: execute_sql
    # ------------------------------------------------------------------ #
    def _normalize_exec_sql(self, sql: str) -> str:
        """Last-hop SQL normalization hook (default: no-op).

        PG-family backends can override this to rewrite MySQL-style
        introspection statements (``SHOW TABLES`` / ``DESC`` …) into catalog
        SELECTs. Implementations must only return read-only equivalents.
        """
        return sql

    def execute_sql(self, sql: str, *, max_rows: int = 200) -> ExecResult:
        from sqlalchemy import text

        t0 = time.time()
        exec_sql = self._normalize_exec_sql(sql)
        try:
            with self._engine.connect() as conn:
                result = conn.execute(text(exec_sql))
                cols = [str(k) for k in result.keys()]
                fetched = result.fetchmany(max_rows + 1)
                truncated = len(fetched) > max_rows
                total = len(fetched)
                rows = [list(r) for r in fetched[:max_rows]]
                rc = result.rowcount
                row_count = rc if rc is not None and rc >= 0 else total
            return ExecResult(
                sql=sql,
                columns=cols,
                rows=rows,
                row_count=row_count,
                truncated=truncated,
                elapsed_ms=(time.time() - t0) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 — SQL failure is expected
            return ExecResult(
                sql=exec_sql,  # shows the rewritten form when normalized
                error=str(exc),
                elapsed_ms=(time.time() - t0) * 1000,
            )

    # ------------------------------------------------------------------ #
    # Port: extract_metadata (Template Method; raises ConnectorError)
    # ------------------------------------------------------------------ #
    def extract_metadata(self, schemas: Sequence[str]) -> PhysicalManifest:
        from sqlalchemy import inspect

        resolved = self._resolve_schema(schemas)
        schema_label = self._schema_label(resolved)
        db = self._db_id
        table_recs: list[TableRecord] = []
        col_recs: list[ColumnRecord] = []
        fks: list[FKInfo] = []
        try:
            with self._engine.connect() as conn:
                insp = inspect(conn)
                table_names = self._list_tables(insp, resolved)
                views = set(self._list_views(insp, resolved))
                all_names = sorted(set(table_names) | views)
                col_comments = self._load_column_comments(conn, resolved, all_names)
                for t in all_names:
                    cols_raw = insp.get_columns(t, schema=resolved)
                    pk_cols = set(self._pk_columns(insp, t, resolved))
                    fks.extend(self._load_fks(insp, t, resolved))
                    comment = self._table_comment(insp, t, resolved)
                    col_names = [str(c.get("name")) for c in cols_raw]
                    partition_col = _detect_partition_key(col_names)
                    ddl = self._table_ddl(
                        insp, t, resolved, cols_raw, pk_cols, is_view=t in views
                    )
                    table_recs.append(
                        TableRecord(
                            key=table_key(db, schema_label, t),
                            db=db,
                            schema=schema_label,
                            name=t,
                            layer=derive_layer(t),
                            partition_key=partition_col,
                            comment=comment,
                            ddl=ddl,
                        )
                    )
                    for c in cols_raw:
                        cname = str(c.get("name"))
                        ctype = self._normalize_type(c)
                        ccomment = (
                            col_comments.get((t, cname))
                            or (c.get("comment") or "").strip()
                        )
                        text = f"{db}.{t}.{cname} ({ctype})"
                        if ccomment:
                            text += f" — {ccomment}"
                        col_recs.append(
                            ColumnRecord(
                                key=column_key(db, schema_label, t, cname),
                                db=db,
                                schema=schema_label,
                                table=t,
                                name=cname,
                                type=ctype,
                                pk=cname in pk_cols,
                                nullable=bool(c.get("nullable", True)),
                                is_partition=(cname == partition_col),
                                comment=ccomment,
                                description=ccomment,
                                text=text,
                            )
                        )
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap into the layer exception
            raise ConnectorError(
                f"反射数据源 {db!r} 元数据失败（schema={schema_label!r}）: {exc}",
                cause=exc,
            ) from exc

        self._post_reflect_safely(resolved, schema_label, table_recs, col_recs)
        log.info(
            "universal reflected: db=%s schema=%s tables=%d columns=%d fks=%d",
            db, schema_label, len(table_recs), len(col_recs), len(fks),
        )
        return PhysicalManifest(
            db_id=db, schema=schema_label,
            tables=table_recs, columns=col_recs, fks=fks,
        )

    def _post_reflect_safely(self, schema, schema_label, tables, columns) -> None:
        """Enrichment-hook failures never break the flow."""
        try:
            self._post_reflect(schema, schema_label, tables, columns)
        except Exception as exc:  # noqa: BLE001
            log.debug("post_reflect hook failed (skipped): %s", exc)

    # ------------------------------------------------------------------ #
    # Reflection steps (default implementations; override as needed)
    # ------------------------------------------------------------------ #
    def _list_tables(self, insp, schema: Optional[str]) -> list[str]:
        return [str(t) for t in insp.get_table_names(schema=schema)]

    def _list_views(self, insp, schema: Optional[str]) -> list[str]:
        try:
            return [str(v) for v in insp.get_view_names(schema=schema)]
        except Exception:  # noqa: BLE001 — some dialects cannot reflect views
            return []

    def _pk_columns(self, insp, table: str, schema: Optional[str]) -> list[str]:
        try:
            info = insp.get_pk_constraint(table, schema=schema) or {}
            return [str(c) for c in (info.get("constrained_columns") or [])]
        except Exception:  # noqa: BLE001
            return []

    def _load_fks(self, insp, table: str, schema: Optional[str]) -> list[FKInfo]:
        out: list[FKInfo] = []
        try:
            raw = insp.get_foreign_keys(table, schema=schema) or []
        except Exception:  # noqa: BLE001
            return out
        for fk in raw:
            ref_schema = fk.get("referred_schema")
            if ref_schema and schema and str(ref_schema) != str(schema):
                continue  # cross-schema references stay out (keys are schema-scoped)
            dst_table = str(fk.get("referred_table") or "")
            for s, d in zip(
                fk.get("constrained_columns") or [],
                fk.get("referred_columns") or [],
            ):
                out.append(
                    FKInfo(
                        src_table=table,
                        src_col=str(s),
                        dst_table=dst_table,
                        dst_col=str(d),
                    )
                )
        return out

    def _table_comment(self, insp, table: str, schema: Optional[str]) -> str:
        try:
            info = insp.get_table_comment(table, schema=schema) or {}
            return (info.get("text") or "").strip()
        except Exception:  # noqa: BLE001 — some dialects have no table comments
            return ""

    def _view_ddl(self, insp, table: str, schema: Optional[str]) -> str:
        try:
            body = (insp.get_view_definition(table, schema=schema) or "").strip()
            return body.rstrip(";") + ";" if body else ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _normalize_type(col: dict[str, Any]) -> str:
        t = str(col.get("type") or "").strip().lower()
        return t if t and t != "null" else "text"

    def _synthesize_ddl(
        self,
        table: str,
        schema: Optional[str],
        cols_raw: Sequence[dict[str, Any]],
        pk_cols: set[str],
    ) -> str:
        """Synthesize CREATE TABLE from column definitions when no DDL exists."""
        if not cols_raw:
            return ""
        quote = self._engine.dialect.identifier_preparer.quote
        label = self._schema_label(schema)
        lines = [f"CREATE TABLE {quote(label)}.{quote(table)} ("]
        for i, c in enumerate(cols_raw):
            suffix = "," if i < len(cols_raw) - 1 else ""
            nn = "" if c.get("nullable", True) else " NOT NULL"
            pk = " PRIMARY KEY" if str(c.get("name")) in pk_cols else ""
            lines.append(
                f"  {quote(str(c.get('name')))} {self._normalize_type(c)}{nn}{pk}{suffix}"
            )
        lines.append(");")
        return "\n".join(lines)

    def _table_ddl(
        self,
        insp,
        table: str,
        schema: Optional[str],
        cols_raw: Sequence[dict[str, Any]],
        pk_cols: set[str],
        *,
        is_view: bool,
    ) -> str:
        if is_view:
            return self._view_ddl(insp, table, schema)
        return self._synthesize_ddl(table, schema, cols_raw, pk_cols)


__all__ = ["UniversalConnector"]

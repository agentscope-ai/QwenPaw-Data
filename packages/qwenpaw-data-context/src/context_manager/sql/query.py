"""Execute scoped typed SQL with bounded results and read-only CSV export."""

from __future__ import annotations

import base64
import math
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from threading import Lock
from time import monotonic

import pymysql
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ..auth import GLOBAL_OWNER_ID, AccessMode, GraphScopeAuthority, ScopeBinding
from ..utils.validation import require_trimmed_text
from .artifact import cleanup_sql_results, save_sql_result
from .config import RdbBackend, RdbTarget
from .connectors import RdbConnector, RdbError
from .datasource import (
    Datasource,
    DatasourceError,
    DatasourceErrorCode,
    DatasourceRepository,
)

_MAX_SQL_LENGTH = 50_000
_PREVIEW_ROWS = 20
_DIALECTS = {
    RdbBackend.POSTGRES: "postgres",
    RdbBackend.HOLOGRES: "postgres",
    RdbBackend.MYSQL: "mysql",
    RdbBackend.SQLITE: "sqlite",
    RdbBackend.BIGQUERY: "bigquery",
    RdbBackend.ODPS: "hive",
    RdbBackend.STARROCKS: "starrocks",
    RdbBackend.DORIS: "doris",
    RdbBackend.TIDB: "mysql",
    RdbBackend.DUCKDB: "duckdb",
}
_FORBIDDEN_EXPRESSIONS = (
    exp.DML,
    exp.DDL,
    exp.Command,
    exp.Transaction,
    exp.Lock,
    exp.Into,
    exp.Grant,
    exp.Revoke,
)


class SqlAdmissionError(ValueError):
    """Report SQL that is malformed or outside the read-only query policy."""


class _UnloadedSdkError(Exception):
    """Stand in for an optional SDK error type whose SDK was never imported."""


def _sdk_error(module: str, name: str) -> type[BaseException]:
    """Resolve an optional SDK error lazily; an unloaded SDK raised nothing."""
    error_type = getattr(sys.modules.get(module), name, None)
    if isinstance(error_type, type) and issubclass(error_type, BaseException):
        return error_type
    return _UnloadedSdkError


def _timeout_marker(error: BaseException) -> str | None:
    """Distinguish confirmed statement timeouts from MySQL transport timeouts."""
    message = str(error).lower()
    code = error.args[0] if error.args else None
    if (isinstance(error, pymysql.Error) and code == 3024) or (
        isinstance(error, _sdk_error("psycopg", "Error"))
        and error.sqlstate == "57014"
        and "statement timeout" in message
    ):
        return (
            "QUERY_TIMEOUT: database reported a statement timeout. This does not "
            "establish excessive data volume. Optimize once without changing the "
            "requested dates, population, metrics or completeness; otherwise report "
            "the timeout. Do not retry identical SQL."
        )
    if isinstance(error, pymysql.Error) and code == 2013 and "timed out" in message:
        return (
            "SQL_READ_TIMEOUT: the client stopped waiting for MySQL; server-side "
            "cancellation is not confirmed. Check connectivity and server load; do "
            "not infer a data gap or retry blindly."
        )
    return None


class SqlExecutionError(RuntimeError):
    """Report execution failures with native database diagnostics when available."""

    @classmethod
    def from_driver(cls, message: str, error: BaseException) -> SqlExecutionError:
        """Preserve driver diagnostics through the connector's lifecycle wrappers."""
        backend = error.backend if isinstance(error, RdbError) else None
        while isinstance(error, RdbError) and error.__cause__ is not None:
            error = error.__cause__
        if (
            backend in (RdbBackend.POSTGRES, RdbBackend.HOLOGRES)
            and isinstance(error, ModuleNotFoundError)
            and error.name == "psycopg"
        ):
            return cls(f"{message}: PostgreSQL/Hologres driver psycopg is not installed")
        google_error = _sdk_error("google.api_core.exceptions", "GoogleAPICallError")
        if not isinstance(
            error,
            (
                _sdk_error("psycopg", "Error"),
                pymysql.Error,
                sqlite3.Error,
                _sdk_error("odps.errors", "ODPSError"),
                google_error,
                _sdk_error("duckdb", "Error"),
            ),
        ):
            return cls(message)
        details = str(error)
        if isinstance(error, _sdk_error("psycopg", "Error")) and error.sqlstate:
            details = f"SQLSTATE {error.sqlstate}: {details}"
        elif isinstance(error, sqlite3.Error):
            code = getattr(error, "sqlite_errorcode", None)
            name = getattr(error, "sqlite_errorname", None)
            if code is not None:
                details = f"SQLite {code} ({name}): {details}"
        elif isinstance(error, google_error) and error.errors:
            details = f"{details}; errors: {error.errors}"
        marker = _timeout_marker(error)
        if marker is not None:
            message = f"{marker} {message}"
        return cls(f"{message}: {details}")


@dataclass(frozen=True, slots=True)
class SqlResult:
    """Return one bounded detached JSON-safe relational result."""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    row_count: int
    truncated: bool
    elapsed_ms: float


class SqlQueryService:
    """Execute typed Workspace SQL and throttle CSV cleanup to once per 60 seconds.

    The cleanup lock is acquired without waiting and held through the scan.
    The monotonic deadline spaces scan starts within this Workspace service.
    """

    def __init__(
        self,
        access: GraphScopeAuthority,
        rdb_connector: RdbConnector,
    ) -> None:
        """Bind the only datasource and connection services used for execution."""
        self._access = access
        self._datasources = DatasourceRepository()
        self._rdb_connector = rdb_connector
        self._cleanup_lock = Lock()
        self._next_cleanup_at = 0.0

    def get_datasource(self, datasource_id: str, *, scope: ScopeBinding) -> Datasource:
        """Read a catalog connection within the caller's authorized scope."""
        datasource_id = require_trimmed_text(datasource_id, "datasource_id")
        self._access.require(scope, AccessMode.READ)
        if scope.zone not in (None, "metadata") or scope.datasource_id not in (
            None,
            datasource_id,
        ):
            raise DatasourceError(
                "Datasource is outside the authorized scope",
                code=DatasourceErrorCode.SCOPE_MISMATCH,
                datasource_id=datasource_id,
            )
        return self._datasources.get(
            datasource_id,
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
        )

    def execute(
        self,
        datasource_id: str,
        sql: str,
        *,
        max_rows: int,
        timeout_ms: int,
        scope: ScopeBinding,
        odps_execution_project: str | None = None,
    ) -> SqlResult:
        """Execute one read-only query within the caller's max_rows budget."""
        target, executable = self._prepare(
            datasource_id,
            sql,
            max_rows,
            timeout_ms,
            scope,
            odps_execution_project=odps_execution_project,
        )
        started = monotonic()
        try:
            with self._rdb_connector.connect(target) as connection:
                description, fetched, _ = connection.query(
                    executable,
                    max_rows=max_rows,
                    timeout_ms=timeout_ms,
                )
                if not description:
                    raise SqlExecutionError("SQL query did not return a row schema")
                columns = self._columns(description)
        except SqlExecutionError:
            raise
        except Exception as exc:
            raise SqlExecutionError.from_driver(
                "Relational SQL execution failed", exc
            ) from exc
        truncated = len(fetched) > max_rows
        rows = tuple(
            tuple(self._json_value(value) for value in row)
            for row in fetched[:max_rows]
        )
        return SqlResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=max(0.0, (monotonic() - started) * 1000),
        )

    def export_csv(
        self,
        datasource_id: str,
        sql: str,
        *,
        max_rows: int,
        timeout_ms: int,
        scope: ScopeBinding,
        odps_execution_project: str | None = None,
    ) -> dict[str, object]:
        """Export at most max_rows rows and return a twenty-row inline preview."""
        target, executable = self._prepare(
            datasource_id,
            sql,
            max_rows,
            timeout_ms,
            scope,
            odps_execution_project=odps_execution_project,
        )
        started = monotonic()
        try:
            with self._rdb_connector.connect(target) as connection:
                description, fetched, row_count = connection.query(
                    executable,
                    max_rows=max_rows,
                    timeout_ms=timeout_ms,
                )
                if not description:
                    raise SqlExecutionError("SQL query did not return a row schema")
                columns = self._columns(description)
            # Drivers without a total count fall back to the fetched row count.
            total = (
                row_count if row_count is not None and row_count >= 0 else len(fetched)
            )
            rows = [
                tuple(self._json_value(value) for value in row)
                for row in fetched[:max_rows]
            ]
            result_id = None
            if rows:
                self._cleanup_results(scope)
                result_id = save_sql_result(columns, rows, scope=scope)
        except SqlExecutionError:
            raise
        except Exception as exc:
            raise SqlExecutionError.from_driver("SQL CSV export failed", exc) from exc
        preview = rows[:_PREVIEW_ROWS]
        return {
            "columns": columns,
            "rows": preview,
            "row_count": len(preview),
            "total_row_count": total,
            "truncated": len(fetched) > max_rows,
            "elapsed_ms": max(0.0, (monotonic() - started) * 1000),
            "result_id": result_id,
        }

    def _cleanup_results(self, scope: ScopeBinding) -> None:
        """Run due cleanup without making concurrent exports wait for the scan."""
        if not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            now = monotonic()
            if now < self._next_cleanup_at:
                return
            self._next_cleanup_at = now + 60
            cleanup_sql_results(scope=scope)
        finally:
            self._cleanup_lock.release()

    def execute_write(
        self,
        datasource_id: str,
        sql: str,
        *,
        statement_type: str,
        max_rows: int,
        timeout_ms: int,
        scope: ScopeBinding,
        odps_execution_project: str | None = None,
    ) -> SqlResult:
        """Execute one typed statement using an administrator-issued capability."""
        self._access.require(scope, AccessMode.WRITE)
        if scope.write_owner_id != GLOBAL_OWNER_ID:
            raise PermissionError("SQL writes require workspace administration")
        statement = self._validate_statement(sql)
        self._bounds(max_rows, timeout_ms)
        datasource = self.get_datasource(datasource_id, scope=scope)
        dialect = _DIALECTS[datasource.backend]
        try:
            expressions = sqlglot.parse(statement, read=dialect)
        except SqlglotError as exc:
            raise SqlAdmissionError("SQL could not be parsed") from exc
        if len(expressions) != 1 or expressions[0] is None:
            raise SqlAdmissionError("Exactly one SQL statement is required")
        expression = expressions[0]
        if statement_type == "create":
            if not isinstance(
                expression, exp.Create
            ) or expression.kind.upper() not in {"TABLE", "VIEW"}:
                raise SqlAdmissionError(
                    "execute_create accepts only CREATE TABLE/VIEW, including CTAS"
                )
            if expression.args.get("replace"):
                raise SqlAdmissionError("CREATE OR REPLACE is not allowed")
        elif statement_type == "modify":
            if (
                not isinstance(expression, (exp.Insert, exp.Update))
                or expression.args.get("with_") is not None
            ):
                raise SqlAdmissionError(
                    "execute_modify accepts only INSERT or UPDATE; WITH-prefixed writes are not allowed"
                )
        else:
            raise SqlAdmissionError("Unknown SQL statement type")
        for node in expression.walk():
            if node is expression:
                continue
            if isinstance(node, _FORBIDDEN_EXPRESSIONS):
                raise SqlAdmissionError(
                    "SQL contains an additional write or forbidden operation"
                )
        project = self._execution_project(datasource, odps_execution_project)
        target = datasource.to_rdb_target(
            user_id=scope.user_id, runtime_project=project
        )
        started = monotonic()
        try:
            with self._rdb_connector.connect(target) as connection:
                description, fetched, affected = connection.execute(
                    statement, max_rows=max_rows, timeout_ms=timeout_ms
                )
                columns = self._columns(description)
                rows = tuple(
                    tuple(self._json_value(value) for value in row)
                    for row in fetched[:max_rows]
                )
        except Exception as exc:
            raise SqlExecutionError.from_driver(
                "Relational SQL write failed; verify the target before retrying", exc
            ) from exc
        return SqlResult(
            columns,
            rows,
            affected if affected is not None else -1,
            len(fetched) > max_rows,
            max(0.0, (monotonic() - started) * 1000),
        )

    def _prepare(
        self,
        datasource_id: str,
        sql: str,
        max_rows: int,
        timeout_ms: int,
        scope: ScopeBinding,
        *,
        odps_execution_project: str | None = None,
    ) -> tuple[RdbTarget, str]:
        """Share admission and scoped datasource resolution across both read modes."""
        statement = self._validate_statement(sql)
        self._bounds(max_rows, timeout_ms)
        datasource = self.get_datasource(datasource_id, scope=scope)
        expression = self._parse_query(statement, dialect=_DIALECTS[datasource.backend])
        project = self._execution_project(datasource, odps_execution_project)
        target = datasource.to_rdb_target(
            user_id=scope.user_id, runtime_project=project
        )
        return target, self._executable(
            statement, expression, target.backend, timeout_ms
        )

    @staticmethod
    def _execution_project(datasource: Datasource, project: str | None) -> str | None:
        if project is not None:
            if not isinstance(project, str) or not project.strip():
                raise SqlAdmissionError("odps_execution_project must be non-empty text")
            project = project.strip()
        if project is not None:
            raise SqlAdmissionError(
                "odps_execution_project overrides are not supported by public ODPS"
            )
        return project

    @staticmethod
    def _validate_statement(sql: str) -> str:
        """Check statement text before resolving its datasource dialect."""
        if not isinstance(sql, str):
            raise SqlAdmissionError("SQL must be text")
        statement = sql.strip()
        if not statement:
            raise SqlAdmissionError("SQL cannot be empty")
        if len(statement) > _MAX_SQL_LENGTH:
            raise SqlAdmissionError("SQL exceeds the maximum length")
        return statement

    @staticmethod
    def _parse_query(statement: str, *, dialect: str) -> exp.Query:
        """Parse and enforce the policy in one explicit backend dialect."""
        try:
            expressions = sqlglot.parse(statement, read=dialect)
        except SqlglotError as exc:
            raise SqlAdmissionError("SQL could not be parsed") from exc
        if len(expressions) != 1 or expressions[0] is None:
            raise SqlAdmissionError("Exactly one SQL statement is required")
        expression = expressions[0]
        if not isinstance(expression, exp.Query):
            raise SqlAdmissionError("Only read-only query statements are allowed")
        if any(isinstance(node, _FORBIDDEN_EXPRESSIONS) for node in expression.walk()):
            raise SqlAdmissionError("SQL contains a forbidden write or lock operation")
        if any(
            isinstance(item, exp.Anonymous)
            and item.name.upper() == "MAX_EXECUTION_TIME"
            for hint in expression.find_all(exp.Hint)
            for item in hint.expressions
        ):
            raise SqlAdmissionError("SQL cannot override execution timeout policy")
        return expression

    @staticmethod
    def _bounds(max_rows: int, timeout_ms: int) -> None:
        """Validate the caller's row budget and the statement timeout."""
        if isinstance(max_rows, bool) or not isinstance(max_rows, int):
            raise SqlAdmissionError("max_rows must be an integer")
        if max_rows < 1:
            raise SqlAdmissionError("max_rows must be positive")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise SqlAdmissionError("timeout_ms must be an integer")
        if not 100 <= timeout_ms <= 30_000:
            raise SqlAdmissionError("timeout_ms must be between 100 and 30000")

    @staticmethod
    def _executable(
        statement: str,
        expression: exp.Query,
        backend: RdbBackend,
        timeout_ms: int,
    ) -> str:
        """Inject a privilege-free MySQL per-query timeout when required."""
        if backend not in (RdbBackend.MYSQL, RdbBackend.TIDB):
            return statement
        executable = expression.copy()
        outer_select = executable.find(exp.Select)
        if outer_select is None:
            raise SqlAdmissionError("MySQL query must contain SELECT")
        timeout_hint = exp.Anonymous(
            this="MAX_EXECUTION_TIME",
            expressions=[exp.Literal.number(timeout_ms)],
        )
        existing_hint = outer_select.args.get("hint")
        expressions = (
            [timeout_hint, *existing_hint.expressions]
            if isinstance(existing_hint, exp.Hint)
            else [timeout_hint]
        )
        outer_select.set("hint", exp.Hint(expressions=expressions))
        try:
            return executable.sql(dialect="mysql")
        except SqlglotError as exc:
            raise SqlAdmissionError("MySQL query could not be rendered") from exc

    @staticmethod
    def _columns(description: Sequence[object]) -> tuple[str, ...]:
        """Normalize psycopg and PyMySQL DB-API column descriptions."""
        columns = []
        for index, item in enumerate(description):
            name = getattr(item, "name", None)
            if name is None and isinstance(item, Sequence) and item:
                name = item[0]
            columns.append(
                name if isinstance(name, str) and name else f"column_{index + 1}"
            )
        return tuple(columns)

    @classmethod
    def _json_value(cls, value: object) -> object:
        """Detach common driver values without losing decimal precision."""
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (date, datetime, time)):
            return value.isoformat()
        if isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")
        if isinstance(value, Mapping):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        if isinstance(value, Sequence):
            return [cls._json_value(item) for item in value]
        return str(value)


__all__ = [
    "SqlAdmissionError",
    "SqlExecutionError",
    "SqlQueryService",
    "SqlResult",
]

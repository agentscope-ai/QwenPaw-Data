"""Own Holo connections, SQL execution, transactions and close lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from threading import Lock
from typing import Any, TypeVar

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..models import QueryDiagnostic, StoreError
from ..runtime import GraphRuntime
from .target import HoloTarget

_T = TypeVar("_T")


class HoloDriver:
    """Own one bounded native connection pool for the Runtime lifetime."""

    def __init__(
        self,
        *,
        max_size: int = 8,
        timeout: float = 30,
        max_idle: float = 300,
        **parameters: object,
    ) -> None:
        parameters["autocommit"] = True
        parameters["options"] = (
            str(parameters.get("options", "")) + " -c search_path=ag_catalog"
        ).strip()
        self._lock = Lock()
        self._closed = False
        self._pool = ConnectionPool(
            kwargs=parameters,
            min_size=0,
            max_size=max_size,
            timeout=timeout,
            max_idle=max_idle,
            max_waiting=64,
            reset=self._reset,
            open=True,
        )

    @staticmethod
    def _reset(connection: psycopg.Connection) -> None:
        # Holo RESET ALL does not restore statement_timeout; reset our setting explicitly.
        connection.execute("RESET statement_timeout")

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Holo driver is closed")
        with self._pool.connection() as connection:
            yield connection

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._pool.close()


class HoloRuntime(GraphRuntime[HoloTarget]):
    """Execute SQL against one exact tenant/workspace graph target."""

    def __init__(self, driver: HoloDriver, target: HoloTarget) -> None:
        if type(target) is not HoloTarget:
            raise TypeError("target must be a HoloTarget")
        if driver is None:
            raise TypeError("driver is required")
        super().__init__(target, driver=driver)

    def _execute(
        self,
        query: str,
        parameters: Mapping[str, object],
        *,
        operation: str,
        write: bool,
        max_rows: int | None = None,
    ) -> Sequence[Mapping[str, object]]:
        if write:
            return self.write_work(
                lambda runtime: runtime.write(query, parameters, operation=operation),
                operation=operation,
            )
        try:
            with (
                self._driver.connection() as connection,
                connection.transaction()
                if operation == "execute_query"
                else nullcontext(),
                connection.cursor(row_factory=dict_row) as cursor,
            ):
                if operation == "execute_query":
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute(query, dict(parameters) if parameters else None)
                if cursor.description is None:
                    return ()
                return tuple(
                    cursor.fetchall()
                    if max_rows is None
                    else cursor.fetchmany(max_rows)
                )
        except psycopg.Error as exc:
            raise HoloRuntime._error(exc, operation) from exc

    def write_work(
        self,
        work: Callable[[GraphRuntime[HoloTarget]], _T],
        *,
        operation: str,
    ) -> _T:
        """Commit only after the callback and its result checks succeed."""
        self._ensure_open(operation)
        committing = False
        try:
            with self._driver.connection() as connection, connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '30s'")
                    cursor.execute("SET LOCAL hg_experimental_enable_transaction = on")
                bound = _BoundRuntime(connection, self.target)
                try:
                    result = work(bound)
                    if (
                        bound._failed
                        or connection.info.transaction_status
                        != TransactionStatus.INTRANS
                    ):
                        raise StoreError(
                            "HoloAge transaction is not committable",
                            operation=operation,
                            code="transaction_failed",
                        )
                    committing = True
                    return result
                except StoreError as exc:
                    if (
                        operation == "atomic"
                        and bound._failed
                        and exc.code not in {"transaction_failed", "commit_unknown"}
                    ):
                        raise StoreError(
                            str(exc),
                            operation=operation,
                            code="transaction_failed",
                            retryable=exc.retryable,
                        ) from exc
                    raise
                finally:
                    bound.close()
        except psycopg.Error as exc:
            raise self._error(exc, operation, committing=committing) from exc

    @staticmethod
    def _error(
        exc: psycopg.Error, operation: str, *, committing: bool = False
    ) -> StoreError:
        """Expose primary query errors; retain full driver details in the cause."""
        state = exc.sqlstate or ""
        retryable = False
        if isinstance(exc, psycopg.OperationalError) and (
            not state or state.startswith("08")
        ):
            code = "commit_unknown" if committing else "unavailable"
        elif state == "23505":
            code = "conflict"
        elif state == "23502" and operation == "create_edges":
            code = "missing_endpoint"
        elif state.startswith("23"):
            code = "constraint_violation"
        elif state in {"40001", "40P01", "55P03", "57014", "57P01", "57P02", "57P03"}:
            code, retryable = "unavailable", state in {"40001", "40P01", "55P03"}
        elif state == "42501":
            code = "permission_denied"
        elif state.startswith(("22", "42")):
            code = "invalid_query"
        else:
            code = "backend_error"
        if operation == "atomic" and code != "commit_unknown":
            code = "transaction_failed"
        diagnostic = None
        if operation == "execute_query":
            details = exc.diag
            if details.message_primary:
                diagnostic = QueryDiagnostic(
                    code=state,
                    message=details.message_primary,
                    position=(
                        int(details.statement_position)
                        if details.statement_position
                        else None
                    ),
                )
        return StoreError(
            "HoloAge operation failed",
            operation=operation,
            code=code,
            retryable=retryable,
            query_diagnostic=diagnostic,
        )

    def prepare_schema(
        self, work: Callable[[Callable[..., Sequence[Mapping[str, object]]]], None]
    ) -> None:
        """Run schema DDL on an independent autocommit connection."""
        self._ensure_open("prepare_schema")
        try:
            with self._driver.connection() as connection:
                # DDL is autocommitted. An expected concurrent-create conflict
                # must not poison subsequent catalog checks as a DML failure would.
                def execute(query: str) -> Sequence[Mapping[str, object]]:
                    with connection.cursor(row_factory=dict_row) as cursor:
                        cursor.execute(query)
                        return tuple(cursor.fetchall()) if cursor.description else ()

                work(execute)
        except psycopg.Error as exc:
            raise self._error(exc, "prepare_schema") from exc


class _BoundRuntime(GraphRuntime[HoloTarget]):
    """Borrow one connection without owning its commit or close."""

    def __init__(self, connection: Any, target: HoloTarget) -> None:
        super().__init__(target)
        self._connection = connection
        self._failed = False

    def fail_transaction(self) -> None:
        self._failed = True

    def _execute(
        self,
        query: str,
        parameters: Mapping[str, object],
        *,
        operation: str,
        write: bool,
        max_rows: int | None = None,
    ) -> Sequence[Mapping[str, object]]:
        if self._failed:
            raise StoreError(
                "transaction is not committable",
                operation=operation,
                code="transaction_failed",
            )
        try:
            with self._connection.cursor(row_factory=dict_row) as cursor:
                if operation == "execute_query":
                    cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute(query, dict(parameters) if parameters else None)
                if cursor.description is None:
                    return ()
                return tuple(
                    cursor.fetchall()
                    if max_rows is None
                    else cursor.fetchmany(max_rows)
                )
        except psycopg.Error as exc:
            self._failed = True
            raise HoloRuntime._error(exc, operation) from exc


__all__ = ["HoloDriver", "HoloRuntime"]

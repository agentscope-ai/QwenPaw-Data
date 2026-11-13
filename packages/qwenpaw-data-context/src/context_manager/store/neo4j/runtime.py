"""Own Neo4j session, transaction, error, and close lifecycles."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from itertools import islice
from typing import Any, TypeVar

from neo4j import Driver, ManagedTransaction, Session, unit_of_work
from neo4j.exceptions import (
    DriverError,
    IncompleteCommit,
    Neo4jError,
    ServiceUnavailable,
)

from ..models import QueryDiagnostic, StoreError
from ..runtime import GraphRuntime
from .target import GraphTarget
from .values import _detach_query_value, _detach_value

_T = TypeVar("_T")
_HEALTH_ACQUISITION_TIMEOUT_SECONDS = 2.0
_QUERY_ACQUISITION_TIMEOUT_SECONDS = 5.0


class Neo4jRuntime(GraphRuntime[GraphTarget]):
    """Execute managed work against one tenant/workspace graph target."""

    def __init__(self, driver: Driver, target: GraphTarget) -> None:
        """Bind a driver and exact graph target without opening a session."""
        if type(target) is not GraphTarget:
            raise TypeError("target must be a GraphTarget")
        if driver is None:
            raise TypeError("driver is required")
        super().__init__(target, driver=driver)

    def write_work(self, work: Callable[[GraphRuntime], _T], *, operation: str) -> _T:
        """Run one callback against a single managed transaction."""
        self._ensure_open(operation)
        try:
            with self._session() as session:

                def execute(tx: ManagedTransaction) -> _T:
                    bound = _BoundRuntime(tx, self.target)
                    try:
                        result = work(bound)
                        if bound._failed:
                            raise StoreError(
                                "transaction is not committable",
                                operation=operation,
                                code="transaction_failed",
                            )
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

                return session.execute_write(execute)
        except (Neo4jError, DriverError) as exc:
            raise self._error(exc, operation) from exc

    def _execute(
        self,
        query: str,
        parameters: Mapping[str, object],
        *,
        operation: str,
        write: bool,
        max_rows: int | None = None,
    ) -> Sequence[Mapping[str, object]]:
        """Execute one prepared query without validation or compilation layers."""
        values = parameters
        try:
            with self._session(
                health=operation == "health" and not write,
                no_retry=operation == "execute_query" and not write,
            ) as session:
                # Readiness checks must report an unavailable backend instead
                # of waiting through the driver's managed-transaction retries.
                if operation == "health" and not write:
                    return _run(session, query, values, max_rows=max_rows)
                executor = session.execute_write if write else session.execute_read

                def execute(tx):
                    return _run(
                        tx,
                        query,
                        values,
                        read_only=operation == "execute_query",
                        max_rows=max_rows,
                    )

                if operation == "execute_query":
                    execute = unit_of_work(timeout=10)(execute)
                return executor(execute)
        except (Neo4jError, DriverError) as exc:
            raise self._error(exc, operation) from exc

    def _session(self, *, health: bool = False, no_retry: bool = False) -> Any:
        """Open one session using the configured database when supplied."""
        options: dict[str, object] = {}
        if self._target.database is not None:
            options["database"] = self._target.database
        if health:
            options["connection_acquisition_timeout"] = _HEALTH_ACQUISITION_TIMEOUT_SECONDS
        if no_retry:
            # User-facing native queries have their own server timeout and
            # should report a disconnected backend instead of driver retries.
            options["max_transaction_retry_time"] = 0
            options["connection_acquisition_timeout"] = _QUERY_ACQUISITION_TIMEOUT_SECONDS
        return self._driver.session(**options)

    @staticmethod
    def _error(exc: Exception, operation: str) -> StoreError:
        """Map failures and expose server diagnostics for submitted queries."""
        code = getattr(exc, "code", None)
        retryable_method = getattr(exc, "is_retryable", None)
        retryable = bool(retryable_method()) if callable(retryable_method) else False
        if isinstance(exc, IncompleteCommit):
            code, retryable = "commit_unknown", False
        elif code == "Neo.ClientError.Schema.ConstraintValidationFailed":
            code = "constraint_violation" if operation == "delete_nodes" else "conflict"
        elif code == "Neo.ClientError.Procedure.ProcedureCallFailed" and any(
            (getattr(exc, "message", "") or "").startswith(
                f"Failed to invoke procedure `db.index.{kind}.queryNodes`: "
                "Caused by: java.lang.IllegalArgumentException: "
                f"There is no such {kind} schema index: "
            )
            for kind in ("fulltext", "vector")
        ):
            # Neo4j wraps missing search indexes in a generic procedure error.
            code = "index_unavailable"
        elif isinstance(exc, ServiceUnavailable) or retryable:
            code = "unavailable"
        elif isinstance(code, str) and code.startswith("Neo.ClientError.Security."):
            code = "permission_denied"
        elif isinstance(code, str) and code.startswith("Neo.ClientError.Statement."):
            code = "invalid_query"
        else:
            code = "backend_error"
        if operation == "atomic" and code != "commit_unknown":
            code = "transaction_failed"
        diagnostic = None
        if operation == "execute_query" and isinstance(exc, Neo4jError) and exc.message:
            diagnostic = QueryDiagnostic(code=exc.code or "", message=exc.message)
        return StoreError(
            "Neo4j operation failed",
            operation=operation,
            code=code,
            retryable=retryable,
            query_diagnostic=diagnostic,
        )


class _BoundRuntime(GraphRuntime[GraphTarget]):
    """Borrow one SDK transaction without owning its commit or Driver."""

    def __init__(self, transaction: ManagedTransaction, target: GraphTarget) -> None:
        super().__init__(target)
        self._transaction = transaction
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
            return _run(
                self._transaction,
                query,
                parameters,
                read_only=operation == "execute_query",
                max_rows=max_rows,
            )
        except (Neo4jError, DriverError) as exc:
            self._failed = True
            raise Neo4jRuntime._error(exc, operation) from exc


def _run(
    transaction: ManagedTransaction | Session,
    query: str,
    parameters: Mapping[str, object],
    *,
    read_only: bool = False,
    max_rows: int | None = None,
) -> Sequence[Mapping[str, object]]:
    """Detach Neo4j records at the transaction boundary."""
    if read_only:
        summary = transaction.run("EXPLAIN " + query, **dict(parameters)).consume()
        if summary.query_type != "r":
            raise StoreError(
                "Native Cypher must be read-only",
                operation="execute_query",
                code="cypher_write_forbidden",
            )
    return tuple(
        {
            name: (_detach_query_value(value) if read_only else _detach_value(value))
            for name, value in record.items()
        }
        for record in islice(transaction.run(query, **dict(parameters)), max_rows)
    )


__all__ = ["Neo4jRuntime"]

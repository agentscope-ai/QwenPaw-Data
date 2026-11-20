"""Share graph execution contracts and ordinary runtime lifecycle."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from threading import Lock
from typing import Generic, Protocol, TypeVar, runtime_checkable

from ..utils.validation import require_nonempty_text
from .models import StoreError


@runtime_checkable
class ExecutionTarget(Protocol):
    """Describe the authorization route shared by graph backends."""

    @property
    def tenant_id(self) -> str: ...

    @property
    def workspace_id(self) -> str: ...


_Target = TypeVar("_Target", bound=ExecutionTarget, covariant=True)
_T = TypeVar("_T")


class GraphRuntime(ABC, Generic[_Target]):
    """Execute native work; own a Driver only when explicitly supplied."""

    def __init__(self, target: _Target, *, driver: object | None = None) -> None:
        self._driver = driver
        self._target = target
        self._close_lock = Lock()
        self._closed = False

    @property
    def target(self) -> _Target:
        return self._target

    def read(
        self,
        query: str,
        parameters: Mapping[str, object],
        *,
        operation: str,
        max_rows: int | None = None,
    ) -> Sequence[Mapping[str, object]]:
        self._ensure_open(operation)
        return self._execute(
            require_nonempty_text(query, "query"),
            dict(parameters),
            operation=operation,
            write=False,
            max_rows=max_rows,
        )

    def write(
        self, query: str, parameters: Mapping[str, object], *, operation: str
    ) -> Sequence[Mapping[str, object]]:
        self._ensure_open(operation)
        return self._execute(
            require_nonempty_text(query, "query"),
            dict(parameters),
            operation=operation,
            write=True,
        )

    def write_work(
        self, work: Callable[[GraphRuntime[_Target]], _T], *, operation: str
    ) -> _T:
        """Reject a second transaction boundary on a borrowed execution scope."""
        raise StoreError("nested atomic work is not supported", operation=operation)

    def fail_transaction(self) -> None:
        """Standalone validation has no active transaction to invalidate."""

    @abstractmethod
    def _execute(
        self,
        query: str,
        parameters: Mapping[str, object],
        *,
        operation: str,
        write: bool,
        max_rows: int | None = None,
    ) -> Sequence[Mapping[str, object]]:
        """Execute through the backend's native transaction API."""

    def close(self) -> None:
        """Close the owned driver once and reject subsequent work."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._driver is not None:
                self._driver.close()

    def _ensure_open(self, operation: str) -> None:
        if self._closed:
            raise StoreError(f"{type(self).__name__} is closed", operation=operation)


__all__ = ["ExecutionTarget", "GraphRuntime"]

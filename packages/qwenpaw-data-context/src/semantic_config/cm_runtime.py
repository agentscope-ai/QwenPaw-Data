"""Bridge existing editor callbacks to the request's current CM runtime.

The binding is request-local and follows asyncio tasks and asyncio.to_thread.
TODO: replace this transitional binding with explicit service injection as the
editor and profiling callback contracts are migrated.
"""

from contextlib import contextmanager
from contextvars import ContextVar

from context_manager.runtime import ContextRuntime

_runtime: ContextVar[ContextRuntime | None] = ContextVar(
    "editor_cm_runtime", default=None
)


@contextmanager
def bind_runtime(runtime: ContextRuntime | None):
    token = _runtime.set(runtime)
    try:
        yield
    finally:
        _runtime.reset(token)


def current_runtime() -> ContextRuntime:
    runtime = _runtime.get()
    if runtime is None or runtime.closed:
        raise RuntimeError("ContextRuntime is required for this operation")
    return runtime

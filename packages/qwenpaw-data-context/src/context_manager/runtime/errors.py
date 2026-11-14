"""Define sanitized runtime selection failures shared across runtime layers."""


class ContextRuntimeUnavailable(RuntimeError):
    """Report a Workspace whose runtime cannot be resolved or constructed."""


__all__ = ["ContextRuntimeUnavailable"]

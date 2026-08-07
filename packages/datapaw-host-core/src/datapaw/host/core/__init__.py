"""Shared host foundation for DataPaw."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import DataPawHost
    from .paths import Paths, resolve_datapaw_home
    from .registry import DataPawHostRegistry

__all__ = [
    "DataPawHost",
    "DataPawHostRegistry",
    "Paths",
    "resolve_datapaw_home",
]


def __getattr__(name: str) -> Any:
    """Load the full host runtime only when a runtime export is requested."""

    if name == "DataPawHost":
        from .core import DataPawHost

        return DataPawHost
    if name in {"Paths", "resolve_datapaw_home"}:
        from .paths import Paths, resolve_datapaw_home

        return {
            "Paths": Paths,
            "resolve_datapaw_home": resolve_datapaw_home,
        }[name]
    if name == "DataPawHostRegistry":
        from .registry import DataPawHostRegistry

        return DataPawHostRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

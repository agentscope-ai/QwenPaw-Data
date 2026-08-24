"""Shared host foundation for QwenPaw Data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import QwenPawDataHost
    from .paths import Paths, resolve_qwenpaw_data_home
    from .registry import QwenPawDataHostRegistry

__all__ = [
    "QwenPawDataHost",
    "QwenPawDataHostRegistry",
    "Paths",
    "resolve_qwenpaw_data_home",
]


def __getattr__(name: str) -> Any:
    """Load the full host runtime only when a runtime export is requested."""

    if name == "QwenPawDataHost":
        from .core import QwenPawDataHost

        return QwenPawDataHost
    if name in {"Paths", "resolve_qwenpaw_data_home"}:
        from .paths import Paths, resolve_qwenpaw_data_home

        return {
            "Paths": Paths,
            "resolve_qwenpaw_data_home": resolve_qwenpaw_data_home,
        }[name]
    if name == "QwenPawDataHostRegistry":
        from .registry import QwenPawDataHostRegistry

        return QwenPawDataHostRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Conflict arbitration protocol — shared types and components.

Usage:

.. code-block:: python

    from context_manager.conflict import (
        ConflictArbiter, ConflictQueue, FastCheckDetector,
        BackgroundScanner, ConflictEvent, ConflictDecision,
    )
    from context_manager.conflict.adapters.kg import KGConflictAdapter
    from context_manager.conflict.adapters.tg import TGConflictAdapter
    from context_manager.conflict.adapters.mg import MGConflictAdapter
"""
from .adapters import ConflictAdapter
from .arbiter import ArbitrationResult, ConflictArbiter
from .detector import BackgroundScanner, FastCheckDetector
from .model import (
    ConflictCandidate,
    ConflictDecision,
    ConflictEntry,
    ConflictEvent,
)
from .queue import ConflictQueue

__all__ = [
    "ArbitrationResult",
    "BackgroundScanner",
    "ConflictAdapter",
    "ConflictArbiter",
    "ConflictCandidate",
    "ConflictDecision",
    "ConflictEntry",
    "ConflictEvent",
    "ConflictQueue",
    "FastCheckDetector",
]

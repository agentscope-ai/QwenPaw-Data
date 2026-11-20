"""Build the shared Knowledge identity judgment prompt."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from ...model import ChatMessage


def knowledge_identity_messages(
    subject: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    *,
    mode: str,
) -> tuple[ChatMessage, ...]:
    """Create one finite same-node judgment request for Knowledge candidates."""
    if mode not in {"batch_deduplication", "graph_resolution"}:
        raise ValueError("unsupported knowledge identity mode")
    if not candidates:
        raise ValueError("identity candidates must not be empty")
    system = """You decide whether one extracted Knowledge Graph node denotes the
same real-world Entity or Event as any supplied candidate. Candidates have
already been restricted to the same node kind, type tag, workspace, and
owner scope. Similar wording or embedding proximity alone is not identity.

Return exactly one JSON object:
{"decision":"match|new|ambiguous","candidate_ids":[],"confidence":0.0}

Use match with exactly one candidate id only when the facts identify the same
real-world node. Use new with no candidate ids when none is the same node. Use
ambiguous with every plausible candidate id when the evidence is insufficient.
Never return an id outside the supplied candidates and never add prose."""
    user = json.dumps(
        {
            "mode": mode,
            "subject": dict(subject),
            "candidates": [dict(candidate) for candidate in candidates],
        },
        ensure_ascii=False,
    )
    return (ChatMessage("system", system), ChatMessage("user", user))


__all__ = ["knowledge_identity_messages"]

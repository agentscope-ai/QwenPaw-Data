# -*- coding: utf-8 -*-
"""Session-scoped identity -> labels/baggage; owns the Langfuse platform keys."""

from __future__ import annotations


class BaseSessionTracer:
    def __init__(self, *, user_id: str, session_id: str) -> None:
        self._user_id = user_id
        self._session_id = session_id

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def labels(self, *, metadata_json: str | None = None) -> dict[str, str]:
        """Session/user label dict. ``langfuse.trace.name`` is left to the step's tracer."""
        labels = {
            "user.id": self._user_id,
            "langfuse.user.id": self._user_id,
            "session.id": self._session_id,
            "langfuse.session.id": self._session_id,
            "gen_ai.conversation.id": self._session_id,
        }
        if metadata_json:
            labels["langfuse.trace.metadata"] = metadata_json
        return labels

    def attach_baggage(self, labels: dict[str, str] | None = None) -> object:
        """Attach labels as baggage (``gen_ai.conversation.id`` excluded)."""
        from opentelemetry import baggage, context

        current = context.get_current()
        for key, value in (labels if labels is not None else self.labels()).items():
            if key == "gen_ai.conversation.id":
                continue
            current = baggage.set_baggage(key, value, context=current)
        return context.attach(current)

# -*- coding: utf-8 -*-
"""One score event. Names live in ``names.Score``. Sending lives in export."""

from __future__ import annotations

from typing import Any


class ScoreEvent:
    """One score. Host queues it; export sends it."""

    def __init__(
        self,
        *,
        id: str,
        name: str,
        data_type: str,
        value: str | float,
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
        chat_id: str = "",
        session_id: str | None = None,
    ) -> None:
        if not id:
            raise ValueError("score id is required")
        if not name:
            raise ValueError("score name is required")
        if not chat_id and not session_id:
            raise ValueError("chat_id or session_id is required")
        self.id = id
        self.name = str(name)
        self.data_type = data_type
        self.value = value
        self.comment = comment
        self.metadata = dict(metadata) if metadata else None
        self.chat_id = chat_id
        self.session_id = session_id

    @classmethod
    def create(
        cls,
        *,
        id: str,
        name: str,
        value: str | float,
        data_type: str = "CATEGORICAL",
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
        chat_id: str = "",
        session_id: str | None = None,
    ) -> ScoreEvent:
        return cls(
            id=id,
            name=name,
            data_type=data_type,
            value=value,
            comment=comment,
            metadata=metadata,
            chat_id=chat_id,
            session_id=session_id,
        )

    @classmethod
    def terminal(
        cls,
        *,
        chat_id: str,
        name: str,
        value: str | float,
        data_type: str = "CATEGORICAL",
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ScoreEvent:
        if not chat_id:
            raise ValueError("chat_id is required")
        return cls(
            id=f"{name}:{chat_id}",
            name=name,
            data_type=data_type,
            value=value,
            comment=comment,
            metadata=metadata,
            chat_id=chat_id,
        )

    @classmethod
    def update(
        cls,
        *,
        id: str,
        name: str,
        value: str | float,
        chat_id: str,
        data_type: str = "CATEGORICAL",
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ScoreEvent:
        if not chat_id:
            raise ValueError("chat_id is required")
        return cls(
            id=id,
            name=name,
            data_type=data_type,
            value=value,
            comment=comment,
            metadata=metadata,
            chat_id=chat_id,
        )

    @property
    def value_text(self) -> str | None:
        return None if isinstance(self.value, (int, float)) else str(self.value)

    @property
    def value_num(self) -> float | None:
        return float(self.value) if isinstance(self.value, (int, float)) else None

    @property
    def metadata_json(self) -> dict[str, Any] | None:
        return self.metadata


__all__ = ["ScoreEvent"]

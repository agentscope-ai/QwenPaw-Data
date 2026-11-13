# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow


@dataclass
class ChatCommand:
    id: str
    chat_id: str
    identity: Identity
    kind: str
    status: str
    created_at: datetime
    steer: dict[str, Any] | None = None
    clarification: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    tool_confirmation: dict[str, Any] | None = None
    ui: dict[str, Any] | None = None

    @classmethod
    def cancel(cls, chat: Chat) -> ChatCommand:
        return cls._new(chat, "cancel")

    @classmethod
    def steer(  # noqa: F811 - factory named after its field
        cls,
        chat: Chat,
        text: str,
        artifact_comments: list[dict[str, Any]],
    ) -> ChatCommand:
        if not text.strip():
            raise ValueError("text is required")
        return cls._new(
            chat,
            "steer",
            steer={"text": text, "artifact_comments": list(artifact_comments)},
        )

    @classmethod
    def clarification(  # noqa: F811 - factory named after its field
        cls,
        chat: Chat,
        *,
        clarification_id: str,
        result: dict[str, Any],
    ) -> ChatCommand:
        return cls._new(
            chat,
            "clarification",
            clarification={"id": clarification_id, "result": result},
        )

    @classmethod
    def plan(  # noqa: F811 - factory named after its field
        cls,
        chat: Chat,
        *,
        reason: str | None,
        plan: dict[str, Any] | None,
    ) -> ChatCommand:
        return cls._new(chat, "plan", plan={"reason": reason, "plan": plan})

    @classmethod
    def tool_confirmation(  # noqa: F811 - factory named after its field
        cls,
        chat: Chat,
        *,
        call_id: str,
        decision: str,
    ) -> ChatCommand:
        if not call_id.strip():
            raise ValueError("call_id is required")
        if decision not in ("approved", "denied"):
            raise ValueError(f"invalid tool confirmation decision: {decision}")
        return cls._new(
            chat,
            "tool_confirmation",
            tool_confirmation={"id": call_id, "decision": decision},
        )

    @classmethod
    def ui(
        cls,
        chat: Chat,
        *,
        call_id: str,
        outcome: dict[str, Any],
    ) -> ChatCommand:
        if not call_id.strip():
            raise ValueError("call_id is required")
        status = outcome.get("status")
        if status == "executed":
            if not isinstance(outcome.get("result"), dict):
                raise ValueError("result is required")
        elif status in ("failed", "rejected"):
            if not isinstance(outcome.get("error"), dict):
                raise ValueError("error is required")
        else:
            raise ValueError(f"invalid UI answer status: {status}")
        return cls._new(chat, "ui", ui={"id": call_id, **outcome})

    @classmethod
    def _new(
        cls,
        chat: Chat,
        kind: str,
        *,
        steer: dict[str, Any] | None = None,
        clarification: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
        tool_confirmation: dict[str, Any] | None = None,
        ui: dict[str, Any] | None = None,
    ) -> ChatCommand:
        return cls(
            id=create_id("cmd"),
            chat_id=chat.id,
            identity=chat.identity,
            kind=kind,
            status="waiting",
            created_at=utcnow(),
            steer=steer,
            clarification=clarification,
            plan=plan,
            tool_confirmation=tool_confirmation,
            ui=ui,
        )

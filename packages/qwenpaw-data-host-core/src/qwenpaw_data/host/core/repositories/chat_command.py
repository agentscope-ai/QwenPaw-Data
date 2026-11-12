# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import ChatCommandRow, ChatRow
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.tracing.records import (
    signal_comment,
    signal_score_name,
    tracer_record_behavior,
)


class ChatCommandRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def add(self, command: ChatCommand) -> None:
        self.db.add(
            ChatCommandRow(
                id=command.id,
                chat_id=command.chat_id,
                user_id=command.identity.user_id,
                tenant_id=command.identity.tenant_id,
                workspace_id=command.identity.workspace_id,
                kind=command.kind,
                status=command.status,
                steer_json=command.steer,
                clarification_json=command.clarification,
                plan_json=command.plan,
                tool_confirmation_json=command.tool_confirmation,
                created_at=command.created_at,
            )
        )
        await self.db.flush()
        await self._tracer_record_signal(command)

    async def _tracer_record_signal(self, command: ChatCommand) -> None:
        """Write the behavior row for a score-worthy command.

        The ChatRow lookup rides the same session as the command write above:
        if the database is gone, that write has already failed, so the lookup
        adds no new failure mode and needs no guard of its own.
        ``tracer_record_behavior`` swallows failures from the rows it appends.
        """
        score_name = signal_score_name(command.kind)
        if score_name is None:
            return
        chat = await self.db.get(ChatRow, command.chat_id)
        if chat is None:
            return
        payload = command.steer or command.clarification or command.plan or {}
        await tracer_record_behavior(
            self.db,
            command.identity,
            session_id=chat.session_id,
            chat_id=command.chat_id,
            kind=score_name,
            target_type="chat_command",
            target_id=command.id,
            payload=payload,
            score_name=score_name,
            comment=signal_comment(command.kind, payload),
        )

    async def clear(self, chat_id: str) -> None:
        await self.db.execute(
            delete(ChatCommandRow).where(ChatCommandRow.chat_id == chat_id)
        )

    async def has_cancel(self, chat_id: str) -> bool:
        row = await self.db.scalar(
            select(ChatCommandRow.id).where(
                ChatCommandRow.chat_id == chat_id,
                ChatCommandRow.kind == "cancel",
            )
        )
        return row is not None

    async def take_steers(self, chat_id: str) -> list[dict[str, Any]]:
        rows = (
            await self.db.scalars(
                select(ChatCommandRow)
                .where(
                    ChatCommandRow.chat_id == chat_id,
                    ChatCommandRow.kind == "steer",
                    ChatCommandRow.status == "waiting",
                )
                .order_by(ChatCommandRow.created_at, ChatCommandRow.id)
            )
        ).all()
        steers: list[dict[str, Any]] = []
        for row in rows:
            if row.steer_json is None:
                raise RuntimeError("CONFLICT: steer command is missing steer_json")
            row.status = "done"
            steers.append(dict(row.steer_json))
        await self.db.flush()
        return steers

    async def take_clarification(
        self,
        chat_id: str,
        clarification_id: str,
    ) -> dict[str, Any] | None:
        rows = (
            await self.db.scalars(
                select(ChatCommandRow)
                .where(
                    ChatCommandRow.chat_id == chat_id,
                    ChatCommandRow.kind == "clarification",
                    ChatCommandRow.status == "waiting",
                )
                .order_by(ChatCommandRow.created_at.desc(), ChatCommandRow.id.desc())
            )
        ).all()
        for row in rows:
            if row.clarification_json is None:
                raise RuntimeError(
                    "CONFLICT: clarification command is missing clarification_json"
                )
            if row.clarification_json["id"] != clarification_id:
                continue
            result = row.clarification_json["result"]
            if not isinstance(result, dict):
                raise RuntimeError("CONFLICT: clarification result is required")
            row.status = "done"
            await self.db.flush()
            return result
        return None

    async def take_plans(self, chat_id: str) -> list[dict[str, Any]]:
        rows = (
            await self.db.scalars(
                select(ChatCommandRow)
                .where(
                    ChatCommandRow.chat_id == chat_id,
                    ChatCommandRow.kind == "plan",
                    ChatCommandRow.status == "waiting",
                )
                .order_by(ChatCommandRow.created_at, ChatCommandRow.id)
            )
        ).all()
        plans: list[dict[str, Any]] = []
        for row in rows:
            if row.plan_json is None:
                raise RuntimeError("CONFLICT: plan command is missing plan_json")
            row.status = "done"
            plans.append(dict(row.plan_json))
        await self.db.flush()
        return plans

    async def take_tool_confirmation(
        self,
        chat_id: str,
        call_id: str,
    ) -> str | None:
        rows = (
            await self.db.scalars(
                select(ChatCommandRow)
                .where(
                    ChatCommandRow.chat_id == chat_id,
                    ChatCommandRow.kind == "tool_confirmation",
                    ChatCommandRow.status == "waiting",
                )
                .order_by(ChatCommandRow.created_at.desc(), ChatCommandRow.id.desc())
            )
        ).all()
        for row in rows:
            if row.tool_confirmation_json is None:
                raise RuntimeError(
                    "CONFLICT: tool_confirmation command is missing payload"
                )
            if row.tool_confirmation_json["id"] != call_id:
                continue
            decision = row.tool_confirmation_json["decision"]
            if decision not in ("approved", "denied"):
                raise RuntimeError("CONFLICT: tool_confirmation decision is invalid")
            row.status = "done"
            await self.db.flush()
            return decision
        return None

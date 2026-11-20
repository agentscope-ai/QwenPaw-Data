# -*- coding: utf-8 -*-
"""Channel binding: ``(identity, channel, external_key)`` → active ``session_id`` pointer.

Keeps IM routing out of ``Session``. One IM conversation owns many sessions
(old ones retained for webui); the active one is whichever this pointer
references. ``/session`` creates a new session and repoints; switching back to
an old session is just a repoint.

The row also carries the inbound send address (``target_type`` / ``send_meta`` /
``display_name``) recorded when the group/person last spoke to the bot. A cron
job created later reads ``send_meta`` straight off this row at fire time, so no
separate ``channel_targets`` table is needed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import ChannelBindingRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.time import utcnow


def _key(
    identity: Identity, channel: str, external_key: str
) -> tuple[str, str, str, str, str]:
    return (
        identity.tenant_id,
        identity.user_id,
        identity.workspace_id,
        channel,
        external_key,
    )


def _to_dict(row: ChannelBindingRow) -> dict[str, Any]:
    return {
        "channel": row.channel,
        "external_key": row.external_key,
        "active_session_id": row.active_session_id,
        "target_type": row.target_type,
        "display_name": row.display_name,
        "send_meta": row.send_meta,
        "updated_at": row.updated_at,
    }


class ChannelBindingRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_active_session_id(
        self, identity: Identity, channel: str, external_key: str
    ) -> str | None:
        """Active session id for a channel conversation, or None if unbound."""
        row = await self.db.get(
            ChannelBindingRow, _key(identity, channel, external_key)
        )
        return row.active_session_id if row is not None else None

    async def point_to(
        self,
        identity: Identity,
        channel: str,
        external_key: str,
        session_id: str,
        *,
        target_meta: dict[str, Any] | None = None,
        display_name: str = "",
    ) -> None:
        """Create or move the binding for ``(identity, channel, external_key)`` → ``session_id``.

        When ``target_meta`` (the output of ``BaseChannel.extract_target_meta``) is
        provided, also refresh the recorded send address so a later cron job can
        read it. ``display_name`` is overwritten only when non-empty.
        """
        now = utcnow()
        row = await self.db.get(
            ChannelBindingRow, _key(identity, channel, external_key)
        )
        if row is None:
            self.db.add(
                ChannelBindingRow(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    workspace_id=identity.workspace_id,
                    channel=channel,
                    external_key=external_key,
                    active_session_id=session_id,
                    target_type=str(target_meta.get("target_type") or "")
                    if target_meta
                    else "",
                    display_name=display_name,
                    send_meta=(target_meta or {}).get("send_meta") or {},
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.active_session_id = session_id
            if target_meta:
                row.target_type = str(target_meta.get("target_type") or "")
                row.send_meta = target_meta.get("send_meta") or {}
            if display_name:
                row.display_name = display_name
            row.updated_at = now
        await self.db.flush()

    async def list_by_channel(
        self, identity: Identity, channel: str
    ) -> list[dict[str, Any]]:
        """All recorded addresses for this user and channel, most recently active first.

        Feeds the ``GET /cron/targets`` picker. Identity-scoped; does not expose
        ``send_meta``.
        """
        rows = (
            await self.db.scalars(
                select(ChannelBindingRow)
                .where(
                    ChannelBindingRow.tenant_id == identity.tenant_id,
                    ChannelBindingRow.user_id == identity.user_id,
                    ChannelBindingRow.workspace_id == identity.workspace_id,
                    ChannelBindingRow.channel == channel,
                )
                .order_by(ChannelBindingRow.updated_at.desc())
            )
        ).all()
        return [
            {
                "external_key": r.external_key,
                "target_type": r.target_type,
                "display_name": r.display_name,
                "last_active_at": r.updated_at,
            }
            for r in rows
        ]

    async def get_target_meta(
        self, identity: Identity, channel: str, external_key: str
    ) -> dict[str, Any] | None:
        """The send address recorded for a target, or None if it never spoke to the bot."""
        row = await self.db.get(
            ChannelBindingRow, _key(identity, channel, external_key)
        )
        if row is None:
            return None
        return {
            "target_type": row.target_type,
            "display_name": row.display_name,
            "send_meta": row.send_meta or {},
        }

    async def exists(self, identity: Identity, channel: str, external_key: str) -> bool:
        """True if this channel conversation has ever been bound for this identity."""
        return (
            await self.db.get(ChannelBindingRow, _key(identity, channel, external_key))
            is not None
        )

# -*- coding: utf-8 -*-
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session


async def access_session(
    db: AsyncSession,
    identity: Identity,
    session_id: str,
    *,
    write: bool = False,
) -> Session:
    from qwenpaw_data.host.core.repositories.session import SessionRepository

    session = await SessionRepository(db, identity).get(session_id)
    allowed = session.can_write(identity) if write else session.can_read(identity)
    if not allowed:
        raise LookupError("session not found")
    return session

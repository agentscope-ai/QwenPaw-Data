# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import AsyncIterator

from qwenpaw_data_auth import Principal
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.auth_middleware import (
    get_identity as _get_identity,
    get_principal as _get_principal,
)
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.identity import Identity


async def get_db() -> AsyncIterator[AsyncSession]:
    """Route transaction; use Depends(get_db, scope="function").

    Commit/rollback must finish before HTTP success is sent. Request-scoped
    cleanup runs too late: immediate follow-up requests can miss new rows and
    commit failures can no longer replace an already-sent success response.
    Streams and background tasks must acquire their own sessions.
    """
    async with get_session_factory()() as db:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


def get_principal() -> Principal:
    return _get_principal()


def get_identity() -> Identity:
    return _get_identity()

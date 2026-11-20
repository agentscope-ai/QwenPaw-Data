# -*- coding: utf-8 -*-
"""System settings — channel config API.

Route prefix ``/api/system/channel-config`` for config CRUD/test, plus a
``/api/channels/reload`` ops route to trigger an in-process reconnect.

Endpoints:
- GET  /system/channel-config/             read all channel configs (secrets masked)
- PUT  /system/channel-config/{channel}    update a single channel's config (then reload)
- POST /system/channel-config/{channel}/test   channel connectivity test
- GET  /system/channel-config/{channel}/qrcode         fetch QR-code login image (wechat)
- GET  /system/channel-config/{channel}/qrcode/status  poll QR-code login status (wechat)
- POST /channels/reload                    re-read DB -> stop old -> rebuild -> start
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from qwenpaw_data.host.core.api.deps import get_identity
from qwenpaw_data.host.core.api.models.channel_config import (
    ChannelConfigPayload,
    ChannelTestResult,
)
from qwenpaw_data.host.core.channels.channel_config_store import (
    ChannelConfigStore,
    ChannelIdConflictError,
    username_of,
    get_channel_config_store,
)
from qwenpaw_data.host.core.channels.wechat.qrcode_auth import wechat_qrcode_auth_handler
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.settings import get_settings

logger = logging.getLogger("qwenpaw_data.channels.api")

router = APIRouter(tags=["channels"])


# ---- channel-config endpoints (prefix /system/channel-config) ----

config_router = APIRouter(prefix="/system/channel-config", tags=["channel-config"])


async def _visible_config(store: ChannelConfigStore) -> dict[str, Any]:
    allowed = get_settings().catalog.channel_ids
    masked = await store.get_masked()
    return {key: value for key, value in masked.items() if key in allowed}


@config_router.get("/")
async def get_channel_config(
    _user: Identity = Depends(get_identity),
) -> dict[str, Any]:
    return await _visible_config(get_channel_config_store(_user))


@config_router.put("/{channel}")
async def update_channel_config(
    channel: str,
    payload: ChannelConfigPayload,
    request: Request,
    _user: Identity = Depends(get_identity),
) -> dict[str, Any]:
    store = get_channel_config_store(_user)
    masked = await _visible_config(store)
    if channel not in masked:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    try:
        updated = await store.update_channel(
            channel, payload.model_dump(exclude_none=False)
        )
    except ChannelIdConflictError as exc:
        # Conflict: 409 + sanitized message (no owner user_id leak to the client).
        logger.warning(
            "channel config rejected (id conflict): %s.%s=%r already bound to user %s/%s "
            "(attempted by user %s/%s)",
            exc.channel,
            exc.field,
            exc.value,
            exc.owner.user_id,
            username_of(exc.owner),
            _user.user_id,
            username_of(_user),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        logger.error("failed to update channel config: %s", channel, exc_info=True)
        raise HTTPException(
            status_code=500, detail="failed to update channel config"
        ) from None
    # In-process reload: re-read DB -> stop old channel -> rebuild -> start.
    # host-core owns the channels now, so no cross-process HTTP callback is needed.
    channel_manager = getattr(request.app.state, "channel_manager", None)
    if channel_manager is not None:
        await channel_manager.reload(_user, channel)

    return {"channel": channel, "config": updated}


@config_router.post("/{channel}/test")
async def test_channel_connection(
    channel: str,
    _user: Identity = Depends(get_identity),
) -> ChannelTestResult:
    store = get_channel_config_store(_user)
    masked = await _visible_config(store)
    if channel not in masked:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    result = await store.test_channel(channel)
    return ChannelTestResult(**result)


# ---- QR-code login endpoints (wechat only) ----
#
# When a bot_token is not pre-configured, the UI fetches a QR code here and
# polls the status endpoint until the user scans & confirms. On success the
# returned bot_token is written back via the PUT config endpoint above.


@config_router.get("/{channel}/qrcode")
async def get_channel_qrcode(
    channel: str,
    _user: Identity = Depends(get_identity),
) -> dict[str, Any]:
    """Return ``{qrcode_img, poll_token}`` for QR-code-based channel login."""
    if channel != "wechat":
        raise HTTPException(
            status_code=404,
            detail=f"QR code login not supported for channel: {channel}",
        )
    result = await wechat_qrcode_auth_handler.fetch_qrcode()
    return {"qrcode_img": result.qrcode_img, "poll_token": result.poll_token}


@config_router.get("/{channel}/qrcode/status")
async def get_channel_qrcode_status(
    channel: str,
    token: str = Query(..., description="poll_token from the qrcode endpoint"),
    _user: Identity = Depends(get_identity),
) -> dict[str, Any]:
    """Return ``{status, credentials}`` for QR-code-based channel login."""
    if channel != "wechat":
        raise HTTPException(
            status_code=404,
            detail=f"QR code login not supported for channel: {channel}",
        )
    result = await wechat_qrcode_auth_handler.poll_status(token)
    return {"status": result.status, "credentials": result.credentials}


# ---- channels ops endpoint ----


@router.post("/channels/reload")
async def reload_channels(
    request: Request,
    _user: Identity = Depends(get_identity),
) -> dict[str, Any]:
    mgr = request.app.state.channel_manager
    return await mgr.reload()

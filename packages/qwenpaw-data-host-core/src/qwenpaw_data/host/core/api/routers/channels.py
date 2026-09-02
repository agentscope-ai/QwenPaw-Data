# -*- coding: utf-8 -*-
"""System settings — channel config API.

Endpoints:
- GET  /system/channel-config/             read all channel configs (secrets masked)
- PUT  /system/channel-config/{channel}    update a single channel's config (then reload)
- POST /system/channel-config/{channel}/test   channel connectivity test
- GET  /system/channel-config/{channel}/qrcode         fetch QR-code login image (wechat)
- GET  /system/channel-config/{channel}/qrcode/status  poll QR-code login status (wechat)
- POST /channels/reload                    re-read store -> stop old -> rebuild -> start
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.models.channel_config import (
    ChannelConfigPayload,
    ChannelTestResult,
)
from qwenpaw_data.host.core.channels.config import (
    ChannelIdConflictError,
    apply_channel_update,
    check_channel_id_conflict,
    mask_channel,
    mask_config,
    test_channel_config,
)
from qwenpaw_data.host.core.domain.identity import Identity

logger = logging.getLogger("qwenpaw_data.channels.api")

router = APIRouter(tags=["channels"])


# ---- channel-config endpoints (prefix /system/channel-config) ----

config_router = APIRouter(prefix="/system/channel-config", tags=["channel-config"])


@config_router.get("/")
async def get_channel_config(
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    return mask_config(await state.channel_configs.load(identity.user_id))


@config_router.put("/{channel}")
async def update_channel_config(
    channel: str,
    payload: ChannelConfigPayload,
    request: Request,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    config = await state.channel_configs.load(identity.user_id)
    if channel not in config:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    try:
        updated = apply_channel_update(
            config, channel, payload.model_dump(exclude_none=False)
        )
        await check_channel_id_conflict(
            state.channel_configs, channel, updated, identity.user_id
        )
        await state.channel_configs.save(identity.user_id, updated)
    except ChannelIdConflictError as exc:
        # Conflict: 409 + sanitized message (no owner user_id leak to the client).
        logger.warning(
            "channel config rejected (id conflict): %s.%s=%r already bound to "
            "user %s (attempted by user %s)",
            exc.channel, exc.field, exc.value,
            exc.owner_user_id, identity.user_id,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        logger.error("failed to update channel config: %s", channel, exc_info=True)
        raise HTTPException(
            status_code=500, detail="failed to update channel config"
        ) from None
    # In-process reload: re-read store -> stop old channel -> rebuild -> start.
    channel_manager = getattr(request.app.state, "channel_manager", None)
    if channel_manager is not None:
        await channel_manager.reload(identity.user_id, channel)

    return {"channel": channel, "config": mask_channel(channel, updated[channel])}


@config_router.post("/{channel}/test")
async def test_channel_connection(
    channel: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> ChannelTestResult:
    config = await state.channel_configs.load(identity.user_id)
    if channel not in config:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    return ChannelTestResult(**test_channel_config(config, channel))


# ---- QR-code login endpoints (wechat only) ----
#
# When a bot_token is not pre-configured, the UI fetches a QR code here and
# polls the status endpoint until the user scans & confirms. On success the
# returned bot_token is written back via the PUT config endpoint above.


@config_router.get("/{channel}/qrcode")
async def get_channel_qrcode(
    channel: str,
    _identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    """Return ``{qrcode_img, poll_token}`` for QR-code-based channel login."""
    if channel != "wechat":
        raise HTTPException(
            status_code=404,
            detail=f"QR code login not supported for channel: {channel}",
        )
    from qwenpaw_data.host.core.channels.wechat.qrcode_auth import (
        wechat_qrcode_auth_handler,
    )

    result = await wechat_qrcode_auth_handler.fetch_qrcode()
    return {"qrcode_img": result.qrcode_img, "poll_token": result.poll_token}


@config_router.get("/{channel}/qrcode/status")
async def get_channel_qrcode_status(
    channel: str,
    token: str = Query(..., description="poll_token from the qrcode endpoint"),
    _identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    """Return ``{status, credentials}`` for QR-code-based channel login."""
    if channel != "wechat":
        raise HTTPException(
            status_code=404,
            detail=f"QR code login not supported for channel: {channel}",
        )
    from qwenpaw_data.host.core.channels.wechat.qrcode_auth import (
        wechat_qrcode_auth_handler,
    )

    result = await wechat_qrcode_auth_handler.poll_status(token)
    return {"status": result.status, "credentials": result.credentials}


# ---- channels ops endpoint ----


@router.post("/channels/reload")
async def reload_channels(
    request: Request,
    _identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    mgr = request.app.state.channel_manager
    if mgr is None:
        raise HTTPException(status_code=503, detail="channel manager not running")
    return await mgr.reload()

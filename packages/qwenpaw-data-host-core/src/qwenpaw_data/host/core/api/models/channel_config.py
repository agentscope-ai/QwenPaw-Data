# -*- coding: utf-8 -*-
"""Channel config request/response models."""
from __future__ import annotations

from qwenpaw_data.host.core.api.models.common import ApiModel


class ChannelConfigPayload(ApiModel):
    """Config payload for a single channel. All fields optional; empty/None means no change."""

    enabled: bool | None = None
    streaming_enabled: bool | None = None
    # feishu
    app_id: str | None = None
    app_secret: str | None = None
    encrypt_key: str | None = None
    verification_token: str | None = None
    # dingtalk
    client_id: str | None = None
    client_secret: str | None = None
    card_template_id: str | None = None
    card_template_key: str | None = None
    # wecom
    bot_id: str | None = None
    secret: str | None = None
    # wechat
    bot_token: str | None = None


class ChannelTestResult(ApiModel):
    success: bool
    message: str

# -*- coding: utf-8 -*-
"""Channel configuration logic: defaults, secret masking, update semantics.

Persistence lives behind the ``ChannelConfigStore`` protocol (JSON / SQL);
this module holds the pure logic both the API routes and the manager share.
"""
from __future__ import annotations

import copy
from typing import Any

# Secret fields per channel (masked on read; empty/masked secret left unchanged on write).
SECRET_FIELDS = {
    "feishu": ("app_secret", "encrypt_key", "verification_token"),
    "dingtalk": ("client_secret",),
    "wecom": ("secret",),
    "wechat": ("bot_token",),
}

# Required non-secret fields per channel (used by test_channel validation).
REQUIRED_FIELDS = {
    "feishu": ("app_id", "app_secret"),
    "dingtalk": ("client_id", "client_secret"),
    "wecom": ("bot_id", "secret"),
    "wechat": ("bot_token",),
}

# The field that uniquely identifies a running channel instance per type — used by the
# reverse lookup (channel's own id → owning user) and the cross-user dedup check.
IDENTITY_FIELD = {
    "feishu": "app_id",
    "dingtalk": "client_id",
    "wecom": "bot_id",
    "wechat": "bot_token",
}


class ChannelIdConflictError(Exception):
    """Raised when saving a channel whose platform id is already owned by another user.

    Prevents two users from binding the same feishu app_id / wecom bot_id / dingtalk
    client_id — otherwise the reverse lookup routes inbound messages to the wrong owner.
    """

    def __init__(self, channel: str, field: str, value: str, owner_user_id: str) -> None:
        self.channel = channel
        self.field = field
        self.value = value
        self.owner_user_id = owner_user_id
        super().__init__()

    def __str__(self) -> str:
        """Sanitized message — omits the owning user_id."""
        return f"{self.channel}.{self.field}={self.value!r} already bound to another user"


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) > 8:
        return key[:4] + "****" + key[-4:]
    return "****"


def mask_channel(channel_key: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a masked copy of the channel config."""
    masked = dict(cfg)
    for f in SECRET_FIELDS.get(channel_key, ()):
        if f in masked:
            masked[f] = _mask_key(str(masked[f] or ""))
    return masked


def mask_config(config: dict[str, Any]) -> dict[str, Any]:
    """Masked copy of a full per-user channel config."""
    return {k: mask_channel(k, dict(v or {})) for k, v in config.items()}


def initial_config() -> dict[str, Any]:
    """Default empty config (all channels disabled)."""
    return {
        "feishu": {
            "enabled": False,
            "app_id": "",
            "app_secret": "",
            "encrypt_key": "",
            "verification_token": "",
            "streaming_enabled": True,
        },
        "dingtalk": {
            "enabled": False,
            "client_id": "",
            "client_secret": "",
            "card_template_id": "",
            "card_template_key": "content",
            "streaming_enabled": True,
        },
        "wecom": {
            "enabled": False,
            "bot_id": "",
            "secret": "",
            "streaming_enabled": True,
        },
        "wechat": {
            "enabled": False,
            "bot_token": "",
            "streaming_enabled": False,
        },
    }


def apply_channel_update(
    config: dict[str, Any], key: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Return a new full config with one channel updated (non-null override).

    Secret field defense: the masked value echoed back by the frontend must
    not be written back as a new value — otherwise the real secret would be
    overwritten by the mask string. A secret field is skipped when the
    incoming value is empty, **or equals the masked form of the current real
    value**.
    """
    data = copy.deepcopy(config) if config else initial_config()
    current = dict(data.get(key) or {})

    id_field = IDENTITY_FIELD.get(key)
    if id_field and isinstance(payload.get(id_field), str):
        payload = {**payload, id_field: payload[id_field].strip()}
    current_masked = mask_channel(key, current)
    for k, v in payload.items():
        if v is None:
            continue
        if k in SECRET_FIELDS.get(key, ()):
            s = str(v) if v is not None else ""
            # Empty -> unchanged; masked value echoed back -> unchanged (defensive)
            if not s.strip() or s == current_masked.get(k, ""):
                continue
        current[k] = v
    data[key] = current
    return data


def test_channel_config(config: dict[str, Any], key: str) -> dict[str, Any]:
    """Connectivity test: validates required fields are present."""
    required = REQUIRED_FIELDS.get(key, ())
    cfg = dict(config.get(key) or {})
    missing = [f for f in required if not str(cfg.get(f) or "").strip()]
    if missing:
        return {
            "success": False,
            "message": f"missing required fields: {', '.join(missing)}",
        }
    return {
        "success": True,
        "message": f"config ok for {key} (real connectivity test in channel impl)",
    }


async def find_user_by_channel_id(
    configs: Any, channel_type: str, channel_id_value: str
) -> str | None:
    """Resolve the owning user for a running channel by its own platform id.

    Scans every user's config ``[channel_type][IDENTITY_FIELD[channel_type]]``
    and returns the first user whose value equals ``channel_id_value``.
    """
    id_field = IDENTITY_FIELD.get(channel_type)
    if not id_field or not channel_id_value:
        return None
    for user_id in await configs.list_user_ids():
        cfg = (await configs.load(user_id)).get(channel_type) or {}
        if str(cfg.get(id_field) or "") == channel_id_value:
            return user_id
    return None


async def check_channel_id_conflict(
    configs: Any, channel_type: str, config: dict[str, Any], self_user_id: str
) -> None:
    """Raise ChannelIdConflictError when another user already owns this platform id."""
    id_field = IDENTITY_FIELD.get(channel_type)
    if not id_field:
        return
    value = str((config.get(channel_type) or {}).get(id_field) or "")
    if not value:
        return
    for user_id in await configs.list_user_ids():
        if user_id == self_user_id:
            continue
        cfg = (await configs.load(user_id)).get(channel_type) or {}
        if str(cfg.get(id_field) or "") == value:
            raise ChannelIdConflictError(channel_type, id_field, value, user_id)

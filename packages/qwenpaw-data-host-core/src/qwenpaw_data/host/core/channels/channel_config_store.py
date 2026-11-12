# -*- coding: utf-8 -*-
"""Channel config persistence store backed by the ``channels`` DB table.

Each Identity (tenant_id/user_id/workspace_id) maps to one row in the ``channels``
table (composite primary key); the ``config_json`` column holds that identity's channel
configuration. ``get_masked()`` returns masked config for the UI; ``load()`` returns the
real secrets for the channel runtime. ``update_channel(key, payload)`` does a non-null
override with secret defense. ``test_channel(key)`` validates required fields.

The store is stateless: every read/write hits the DB, so there is no stale in-memory cache
to reload. A row is created lazily on the first ``update_channel`` write; reads against an
identity with no row return ``_initial_config()`` defaults.
"""

from __future__ import annotations

import os
import copy
import logging
import threading
from typing import Any

from sqlalchemy import func, or_, select

from qwenpaw_data_auth.database import get_db, query_one

from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.db.tables import ChannelRow
from qwenpaw_data.host.core.domain.identity import Identity

log = logging.getLogger("qwenpaw_data.channels.channel_config_store")


class ChannelIdConflictError(Exception):
    """Raised when saving a channel whose platform id is already owned by a different identity.

    Prevents two identities from binding the same feishu app_id / wecom bot_id / dingtalk
    client_id — otherwise the reverse lookup (find_identity_by_channel_id) routes inbound
    messages to the wrong owner.
    """

    def __init__(self, channel: str, field: str, value: str, owner: Identity) -> None:
        self.channel = channel
        self.field = field
        self.value = value
        self.owner = owner
        super().__init__()

    def __str__(self) -> str:
        """Sanitized message — omits the owning user_id."""
        return (
            f"{self.channel}.{self.field}={self.value!r} already bound to another user"
        )


# Secret fields per channel (masked on read; empty/masked secret left unchanged on write).
_SECRET_FIELDS = {
    "feishu": ("app_secret", "encrypt_key", "verification_token"),
    "dingtalk": ("client_secret",),
    "wecom": ("secret",),
    "wechat": ("bot_token",),
}

# Required non-secret fields per channel (used by test_channel validation).
_REQUIRED_FIELDS = {
    "feishu": ("app_id", "app_secret"),
    "dingtalk": ("client_id", "client_secret"),
    "wecom": ("bot_id", "secret"),
    "wechat": ("bot_token",),
}

# The field that uniquely identifies a running channel instance per type — used by the
# reverse lookup (channel's own id → owning Identity) so the IM /datasource path can resolve
# whose access token to forward to CM.
_IDENTITY_FIELD = {
    "feishu": "app_id",
    "dingtalk": "client_id",
    "wecom": "bot_id",
    "wechat": "bot_token",
}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) > 8:
        return key[:4] + "****" + key[-4:]
    return "****"


def _mask_channel(channel_key: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a masked copy of the channel config."""
    masked = dict(cfg)
    for field in _SECRET_FIELDS.get(channel_key, ()):
        if field in masked:
            masked[field] = _mask_key(str(masked[field] or ""))
    return masked


def _initial_config() -> dict[str, Any]:
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


class ChannelConfigStore:
    """Reads/writes one identity's channel configuration in the ``channels`` table.

    One row per Identity (tenant_id/user_id/workspace_id composite PK); the
    ``config_json`` column holds the channel config dict.
    """

    def __init__(self, identity: Identity):
        self.identity = identity

    def _pk(self) -> tuple[str, str, str]:
        return (
            self.identity.tenant_id,
            self.identity.user_id,
            self.identity.workspace_id,
        )

    async def _read_config(self, db) -> dict[str, Any]:
        """Return this identity's config, or ``_initial_config()`` if no row exists yet."""
        row = await db.get(ChannelRow, self._pk())
        if row is not None and row.config_json:
            return copy.deepcopy(row.config_json)
        return _initial_config()

    def owner_identity(self) -> Identity:
        """The owning Identity of this store (the row's primary key)."""
        return self.identity

    # ---- public read ----

    async def load(self) -> dict[str, Any]:
        """For channel runtime: return the real channel config (including secrets)."""
        async with get_session_factory()() as db:
            return await self._read_config(db)

    async def get_masked(self) -> dict[str, Any]:
        """For the UI: all channel secrets masked."""
        data = await self.load()
        return {k: _mask_channel(k, v) for k, v in data.items()}

    # ---- public write ----

    async def update_channel(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update a single channel's config (non-null override; secret left unchanged when empty/masked-echoed).

        Secret field defense: the masked value echoed back by the frontend ``get_masked()``
        must not be written back as a new value — otherwise the real
        secret would be overwritten by the mask string. So a secret field is skipped when the
        incoming value is empty, **or equals the masked form of the current real value**.
        """
        async with get_session_factory()() as db:
            row = await db.get(ChannelRow, self._pk())
            if row is not None and row.config_json:
                data = copy.deepcopy(row.config_json)
            else:
                data = _initial_config()
            current = dict(data.get(key) or {})

            field = _IDENTITY_FIELD.get(key)
            if field and isinstance(payload.get(field), str):
                payload = {**payload, field: payload[field].strip()}
            current_masked = _mask_channel(key, current)
            for k, v in payload.items():
                if v is None:
                    continue
                if k in _SECRET_FIELDS.get(key, ()):
                    s = str(v) if v is not None else ""
                    # Empty -> unchanged; masked value echoed back -> unchanged (defensive)
                    if not s.strip() or s == current_masked.get(k, ""):
                        continue
                current[k] = v
            data[key] = current
            # Dedup: reject if the identity field (app_id/bot_id/client_id) is already owned
            # by a different identity.
            if field:
                effective = str(current.get(field) or "")
                if effective:
                    owner = await _find_conflicting_owner(
                        db, key, field, effective, self.identity
                    )
                    if owner is not None:
                        raise ChannelIdConflictError(key, field, effective, owner)
            if row is None:
                db.add(
                    ChannelRow(
                        tenant_id=self.identity.tenant_id,
                        user_id=self.identity.user_id,
                        workspace_id=self.identity.workspace_id,
                        config_json=data,
                    )
                )
            else:
                row.config_json = data
            await db.commit()
        log.info("Channel config updated: %s", key)
        masked = await self.get_masked()
        return masked[key]

    # ---- connection test ----

    async def test_channel(self, key: str) -> dict[str, Any]:
        """Connectivity test. The first version validates required fields are present (real platform connectivity is added when the channel is implemented)."""
        required = _REQUIRED_FIELDS.get(key, ())
        config = await self.load()
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


_store_instances: dict[Identity, ChannelConfigStore] = {}
_store_lock = threading.Lock()


def get_channel_config_store(identity: Identity) -> ChannelConfigStore:
    """Return the per-identity config store, creating it on first access."""
    cached = _store_instances.get(identity)
    if cached is not None:
        return cached
    with _store_lock:
        cached = _store_instances.get(identity)
        if cached is not None:
            return cached
        store = ChannelConfigStore(identity)
        _store_instances[identity] = store
        return store


def reset_channel_config_stores() -> None:
    """Drop the in-process store cache."""
    with _store_lock:
        _store_instances.clear()


async def iter_identities() -> list[Identity]:
    """Return the Identities that have a channel configuration row in the DB.

    Used by the channel manager to start every user's enabled channels and by the reverse
    lookup. One identity per ``channels`` row.
    """
    out: list[Identity] = []
    async with get_session_factory()() as db:
        rows = (await db.scalars(select(ChannelRow))).all()
        for row in rows:
            out.append(
                Identity(
                    user_id=row.user_id,
                    tenant_id=row.tenant_id,
                    workspace_id=row.workspace_id,
                )
            )
    return sorted(out, key=lambda i: i.user_id)


async def find_identity_by_channel_id(
    channel_type: str, channel_id_value: str
) -> Identity | None:
    """Resolve the owning Identity for a running channel by its own platform id.

    Scans every ``channels`` row's ``config_json[channel_type][_IDENTITY_FIELD[channel_type]]``
    and returns the first Identity whose value equals ``channel_id_value``.
    """
    field = _IDENTITY_FIELD.get(channel_type)
    if not field or not channel_id_value:
        return None
    async with get_session_factory()() as db:
        rows = (await db.scalars(select(ChannelRow))).all()
        for row in rows:
            cfg = (row.config_json or {}).get(channel_type) or {}
            if str(cfg.get(field) or "") == channel_id_value:
                return Identity(
                    user_id=row.user_id,
                    tenant_id=row.tenant_id,
                    workspace_id=row.workspace_id,
                )
    return None


def _config_field_expr(db: Any, channel_type: str, field: str):
    """Dialect-aware text extraction of ``config_json[channel_type][field]``.

    SQLite has ``json_extract(json, '$.a.b')``; PostgreSQL does not (only
    ``json_extract_path_text(json, 'a', 'b')``), so pick per dialect.
    """
    if db.get_bind().dialect.name == "postgresql":
        return func.json_extract_path_text(ChannelRow.config_json, channel_type, field)
    return func.json_extract(ChannelRow.config_json, f"$.{channel_type}.{field}")


async def _find_conflicting_owner(
    db: Any, channel_type: str, field: str, value: str, self_identity: Identity
) -> Identity | None:
    """Find another identity that already owns ``value`` for ``channel_type``.

    Pushes the JSON lookup into the DB instead of loading every row into Python.
    Excludes ``self_identity``.
    """
    stmt = (
        select(ChannelRow)
        .where(
            _config_field_expr(db, channel_type, field) == value,
            or_(
                ChannelRow.tenant_id != self_identity.tenant_id,
                ChannelRow.user_id != self_identity.user_id,
                ChannelRow.workspace_id != self_identity.workspace_id,
            ),
        )
        .limit(1)
    )
    row = (await db.scalars(stmt)).first()
    if row is None:
        return None
    return Identity(
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        workspace_id=row.workspace_id,
    )


def username_of(identity: Identity) -> str:
    """Best-effort username for identity."""
    dsn = os.environ.get("QWENPAW_DATA_CONTROL_PLANE_DSN", "")
    try:
        with get_db(dsn) as conn:
            row = query_one(
                conn, "SELECT username FROM users WHERE id = ?", (identity.user_id,)
            )
        return str(row["username"]) if row else identity.user_id
    except Exception:
        return identity.user_id

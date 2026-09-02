# -*- coding: utf-8 -*-
"""API-key at-rest protection for stored provider credentials.

With ``QWENPAW_DATA_PREFS_MASTER_SECRET`` set (hex, >=32 bytes) keys are
Fernet-encrypted; without it they are stored as-is with a one-time
warning — acceptable for single-user local homes, not for shared hosts.
"""

from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

MASTER_SECRET_ENV = "QWENPAW_DATA_PREFS_MASTER_SECRET"
_ENC = "ENC:"
_warned_plaintext = False


def _fernet():
    raw_hex = (os.environ.get(MASTER_SECRET_ENV) or "").strip()
    if not raw_hex:
        return None
    try:
        raw = bytes.fromhex(raw_hex)
    except ValueError as exc:
        raise ValueError(f"{MASTER_SECRET_ENV} must be hex") from exc
    if len(raw) < 32:
        raise ValueError(f"{MASTER_SECRET_ENV} must be at least 32 bytes")
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dep ships in [service]
        raise RuntimeError(
            f"{MASTER_SECRET_ENV} is set but the cryptography package is "
            "not installed",
        ) from exc
    return Fernet(base64.urlsafe_b64encode(raw[:32]))


def encrypt_api_key(plaintext: str) -> str:
    global _warned_plaintext
    if not plaintext.strip():
        raise ValueError("api_key is required")
    if plaintext.startswith(_ENC):
        return plaintext
    fernet = _fernet()
    if fernet is None:
        if not _warned_plaintext:
            logger.warning(
                "storing provider api_key without encryption; set %s to "
                "encrypt credentials at rest",
                MASTER_SECRET_ENV,
            )
            _warned_plaintext = True
        return plaintext
    return _ENC + fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_api_key(value: str) -> str:
    if not value:
        raise ValueError("api_key is required")
    if not value.startswith(_ENC):
        return value
    fernet = _fernet()
    if fernet is None:
        raise ValueError(
            f"api_key is encrypted but {MASTER_SECRET_ENV} is not set",
        )
    try:
        return fernet.decrypt(value[len(_ENC) :].encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError("failed to decrypt api_key") from exc


def mask_api_key(api_key: str, *, prefix: str = "") -> str:
    if not api_key:
        return ""
    head = prefix if prefix and api_key.startswith(prefix) else api_key[:2]
    return f"{head}******"

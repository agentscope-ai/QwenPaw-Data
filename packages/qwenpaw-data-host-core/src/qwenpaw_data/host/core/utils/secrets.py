# -*- coding: utf-8 -*-
from __future__ import annotations

import base64

from cryptography.fernet import Fernet

from qwenpaw_data.host.core.settings import get_settings

_ENC = "ENC:"


def _fernet() -> Fernet:
    raw = get_settings().security.master_key()
    return Fernet(base64.urlsafe_b64encode(raw[:32]))


def encrypt_api_key(plaintext: str) -> str:
    if not plaintext.strip():
        raise ValueError("api_key is required")
    if plaintext.startswith(_ENC):
        return plaintext
    return _ENC + _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_api_key(value: str) -> str:
    if not value:
        raise ValueError("api_key is required")
    if not value.startswith(_ENC):
        return value
    try:
        return _fernet().decrypt(value[len(_ENC) :].encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def mask_api_key(api_key: str, *, prefix: str = "") -> str:
    if not api_key:
        return ""
    head = prefix if prefix and api_key.startswith(prefix) else api_key[:2]
    return f"{head}******"

"""Remove credentials from structured fields and formatted log output."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from copy import copy

_REDACTED = "[REDACTED]"
_SECRET_FIELD_NAMES = frozenset(
    {
        "access_key",
        "access_key_id",
        "access_key_secret",
        "api_key",
        "auth",
        "authorization",
        "client_secret",
        "llm_api_key",
        "neo4j_password",
        "passphrase",
        "passwd",
        "password",
        "pem",
        "private_key",
        "private_key_passphrase",
        "pwd",
        "secret",
        "token",
    }
)
_SECRET_FIELD_ALTERNATION = "|".join(
    sorted((re.escape(name) for name in _SECRET_FIELD_NAMES), key=len, reverse=True)
)
_PEM_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?"
    r"-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*[\"']?)"
    r"(?:bearer|basic)\s+[^\s,;&}\]\"']+",
)
_BEARER_PATTERN = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
)
_QUOTED_SECRET_PATTERN = re.compile(
    rf"(?P<prefix>[\"']?(?<![A-Za-z0-9])(?:{_SECRET_FIELD_ALTERNATION})\b[\"']?"
    rf"\s*[:=]\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_UNQUOTED_SECRET_PATTERN = re.compile(
    rf"(?P<prefix>[\"']?(?<![A-Za-z0-9])(?:{_SECRET_FIELD_ALTERNATION})\b[\"']?"
    rf"\s*[:=]\s*)(?P<value>(?!\[REDACTED\])[^\s,;&}}\]\"']+)",
    re.IGNORECASE,
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]+:)"
    r"(?P<password>[^\s/@]+)(?P<suffix>@)",
)
_ACCESS_KEY_PATTERNS = (
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{12,20}"),
    re.compile(r"LTAI[A-Za-z0-9]{12,30}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)
_ORIGINAL_HANDLER_FORMAT = logging.Handler.format


def redact_credentials(value: str) -> str:
    """Return one log-safe string while preserving non-secret context."""
    if not value:
        return value
    redacted = _PEM_PATTERN.sub(_REDACTED, value)
    redacted = _AUTHORIZATION_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _QUOTED_SECRET_PATTERN.sub(
        r"\g<prefix>\g<quote>[REDACTED]\g<quote>",
        redacted,
    )
    redacted = _UNQUOTED_SECRET_PATTERN.sub(
        r"\g<prefix>[REDACTED]",
        redacted,
    )
    redacted = _CREDENTIAL_URL_PATTERN.sub(
        r"\g<prefix>[REDACTED]\g<suffix>",
        redacted,
    )
    for pattern in _ACCESS_KEY_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)


def _is_secret_field(value: object) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    return any(
        normalized == name or normalized.endswith(f"_{name}")
        for name in _SECRET_FIELD_NAMES
    )


def _redact_fields(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: (_REDACTED if _is_secret_field(key) else _redact_fields(item))
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_fields(item) for item in value)
    if isinstance(value, list):
        return [_redact_fields(item) for item in value]
    return value


def _format_redacted(handler: logging.Handler, record: logging.LogRecord) -> str:
    """Preserve formatter inputs and redact a handler-local record copy."""
    safe_record = copy(record)
    for name, value in record.__dict__.items():
        if name in {"msg", "args", "exc_info", "exc_text"}:
            continue
        safe_record.__dict__[name] = (
            _REDACTED if _is_secret_field(name) else _redact_fields(value)
        )
    return redact_credentials(_ORIGINAL_HANDLER_FORMAT(handler, safe_record))


def install_credential_redaction() -> None:
    """Install redaction for handlers using the standard format method."""
    logging.Handler.format = _format_redacted


__all__ = [
    "install_credential_redaction",
    "redact_credentials",
]

"""Offline credential conversion; no settings, environment, SQL or key generation.

Both keys must be supplied explicitly by the eventual importer. Never reuse the
runtime helpers here: they pass existing ENC values through on encryption and
return an empty credential on decryption failure. Neither is safe for migration.
This module is not an import command and does not persist converted credentials.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .legacy_json import MigrationError, _component


@dataclass(frozen=True)
class ConvertedProviders:
    # Endpoints can contain secrets too. Only counts belong in a public report.
    providers: dict[str, dict[str, Any]] = field(repr=False)
    plaintext_count: int
    encrypted_count: int


class CredentialRewriter:
    """Authenticate old ciphertext, then always encrypt under the target key.

    Old releases accepted >=32 raw key bytes and used the first 32; current
    settings require exactly 32. Keys are raw bytes, not hex or Fernet strings.
    No fallback to ambient secrets is permitted, even when keys happen to match.
    """

    def __init__(
        self, *, source_key: bytes | None, target_key: bytes, max_bytes: int = 65536
    ) -> None:
        if type(target_key) is not bytes or len(target_key) != 32:
            raise MigrationError("Target master key must contain exactly 32 raw bytes")
        if source_key is not None and (
            type(source_key) is not bytes or len(source_key) < 32
        ):
            raise MigrationError("Legacy master key must contain at least 32 raw bytes")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise MigrationError("Credential size limit must be a positive integer")
        self._source = (
            None
            if source_key is None
            else Fernet(base64.urlsafe_b64encode(source_key[:32]))
        )
        self._target = Fernet(base64.urlsafe_b64encode(target_key))
        self._max_bytes = max_bytes

    def rewrite(self, value: Any) -> str:
        """Never return plaintext, a failed-decryption placeholder or old token."""
        if not isinstance(value, str) or not value:
            raise MigrationError("Legacy credential must be a non-empty string")
        # Bound before encoding or decrypting attacker-controlled legacy data.
        if len(value) > self._max_bytes:
            raise MigrationError("Legacy credential exceeds the configured size limit")
        if not value.strip():
            raise MigrationError("Legacy credential must be a non-empty string")
        try:
            raw = value.encode("utf-8")
        except UnicodeError:
            raise MigrationError("Legacy credential is not valid UTF-8") from None
        if len(raw) > self._max_bytes:
            raise MigrationError("Legacy credential exceeds the configured size limit")
        if value.startswith("ENC:"):
            if self._source is None:
                raise MigrationError(
                    "Encrypted credentials require an explicit legacy master key"
                )
            try:
                raw = self._source.decrypt(raw[4:])
                if not raw.decode("utf-8").strip():
                    raise ValueError("empty credential")
            except (InvalidToken, UnicodeError, ValueError):
                raise MigrationError(
                    "Cannot authenticate or decode legacy credential"
                ) from None
        return "ENC:" + self._target.encrypt(raw).decode("ascii")

    def rewrite_providers(self, providers: Any) -> ConvertedProviders:
        """Convert every provider or fail without returning a partial result.

        Unknown record fields are refused rather than silently discarded. This
        validates stored shape, not provider availability or network reachability.
        """
        if not isinstance(providers, dict):
            raise MigrationError("Legacy providers must be an object")
        converted, plaintext, encrypted = {}, 0, 0
        for provider_id, record in providers.items():
            _component(provider_id)
            if (
                not isinstance(record, dict)
                or "api_key_enc" not in record
                or set(record) - {"api_key_enc", "base_url"}
                or (
                    record.get("base_url") is not None
                    and not isinstance(record["base_url"], str)
                )
            ):
                raise MigrationError("Invalid or unsupported legacy provider record")
            value = record["api_key_enc"]
            converted[provider_id] = {
                "api_key_enc": self.rewrite(value),
                "base_url": record.get("base_url"),
            }
            if value.startswith("ENC:"):
                encrypted += 1
            else:
                plaintext += 1
        return ConvertedProviders(converted, plaintext, encrypted)

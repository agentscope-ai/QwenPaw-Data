"""
HMAC token issuer and verifier with key-rotation support.

Token format: ``base64url(json_payload).hex(hmac_sha256_signature)``

The payload is a JSON object; the signature covers the raw base64url bytes.
Verification tries each key in order — the first match wins, which enables
seamless key rotation by prepending the new key to the comma-separated list.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
import uuid
from dataclasses import dataclass, field


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class TokenIssuer:
    secret: str = field(repr=False)

    def _issue(self, payload: dict) -> str:
        # An empty key signs tokens anyone can forge.
        if not self.secret:
            raise ValueError("A signing secret is required")
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=False)
        payload_b64 = _b64url_encode(raw.encode())
        sig = _sign(payload_b64, self.secret)
        return f"{payload_b64}.{sig}"

    def issue_access(
        self,
        user_id: str,
        tenant_id: str,
        active_workspace_id: str,
        role: str,
        ttl: int = 86400,
    ) -> str:
        now = time.time()
        payload = {
            "sub": user_id,
            "tenant_id": tenant_id,
            "active_ws": active_workspace_id,
            "role": role,
            "exp": now + ttl,
            "iat": now,
            "jti": uuid.uuid4().hex,
            "typ": "access",
        }
        return self._issue(payload)

    def issue_refresh(
        self,
        user_id: str,
        tenant_id: str,
        ttl: int = 604800,
    ) -> str:
        now = time.time()
        payload = {
            "sub": user_id,
            "tenant_id": tenant_id,
            "exp": now + ttl,
            "iat": now,
            "jti": uuid.uuid4().hex,
            "typ": "refresh",
        }
        return self._issue(payload)

    def issue_platform_access(
        self,
        user_id: str,
        ttl: int = 86400,
    ) -> str:
        now = time.time()
        payload = {
            "sub": user_id,
            "platform": True,
            "role": "platform_admin",
            "exp": now + ttl,
            "iat": now,
            "jti": uuid.uuid4().hex,
            "typ": "access",
        }
        return self._issue(payload)

    def issue_authorization_code(
        self,
        user_id: str,
        redirect_uri: str,
        ttl: int = 30,
    ) -> str:
        now = time.time()
        payload = {
            "sub": user_id,
            "redirect_uri": redirect_uri,
            "exp": now + ttl,
            "iat": now,
            "jti": uuid.uuid4().hex,
            "typ": "authorization_code",
        }
        return self._issue(payload)


class TokenVerifyError(Exception):
    pass


@dataclass
class TokenVerifier:
    secrets: list[str] = field(repr=False)

    def verify(self, token: str) -> dict:
        # Bound untrusted input and require exactly one separator.
        if not isinstance(token, str) or len(token) > 16384:
            raise TokenVerifyError("Malformed token")
        parts = token.split(".")
        if len(parts) != 2:
            raise TokenVerifyError("Malformed token")

        payload_b64, sig = parts

        for secret in self.secrets:
            # An empty key would accept tokens anyone can sign.
            if not secret:
                continue
            expected = _sign(payload_b64, secret)
            try:
                matched = hmac.compare_digest(expected, sig)
            except (TypeError, ValueError, UnicodeError) as exc:
                raise TokenVerifyError("Malformed token") from exc
            if matched:
                break
        else:
            raise TokenVerifyError("Invalid signature")

        try:
            raw = _b64url_decode(payload_b64)
            payload = json.loads(raw)
        except (json.JSONDecodeError, Exception) as exc:
            raise TokenVerifyError(f"Invalid payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise TokenVerifyError("Invalid payload")

        # A missing or non-finite lifetime would make the token valid forever.
        for name in ("iat", "exp"):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise TokenVerifyError("Invalid token lifetime")
        now = time.time()
        exp = payload["exp"]
        if now >= exp:
            raise TokenVerifyError("Token expired")
        if payload["iat"] > now + 30 or exp <= payload["iat"]:
            raise TokenVerifyError("Invalid token lifetime")
        if not isinstance(payload.get("sub"), str) or not payload["sub"]:
            raise TokenVerifyError("Invalid subject")
        if payload.get("typ") not in {"access", "refresh", "authorization_code"}:
            raise TokenVerifyError("Invalid token type")
        if not isinstance(payload.get("jti"), str) or not payload["jti"]:
            raise TokenVerifyError("Invalid token id")

        return payload

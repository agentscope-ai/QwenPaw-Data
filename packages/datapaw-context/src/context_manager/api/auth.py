"""API token 认证中间件。

支持一个向后兼容的全权限 ``DATAPAW_API_TOKEN``，以及
``DATAPAW_API_KEYS`` 中带独立 scope 的 API key。配置任一凭证后，除健康
检查等豁免路径外的所有 HTTP 请求必须携带匹配的 Bearer token。

前端将 token 保存在 sessionStorage 的 ``datapaw_auth_token`` 键中，
请求时自动附带（见 frontend/src/services/authHeaders.ts）。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("api.auth")

SCOPE_QUERY = "query"
SCOPE_WRITE = "write"
SCOPE_MANAGE = "manage"
SCOPE_CREDENTIALS = "credentials:manage"
ALL_SCOPES = frozenset({SCOPE_QUERY, SCOPE_WRITE, SCOPE_MANAGE, SCOPE_CREDENTIALS})

API_KEYS_ENV = "DATAPAW_API_KEYS"
CLIENT_API_TOKEN_ENV = "DATAPAW_CLIENT_API_TOKEN"
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# 只允许精确匹配的存活检查与浏览器自动请求免认证。
AUTH_EXEMPT_PATHS = frozenset(
    {"/api/health", "/api/auth/status", "/health", "/favicon.ico"}
)


@dataclass(frozen=True)
class AuthPrincipal:
    """Authenticated caller identity carried through REST and MCP."""

    subject: str
    scopes: frozenset[str]
    credential_id: str

    def has_scopes(self, required: frozenset[str]) -> bool:
        return required.issubset(self.scopes)


@dataclass(frozen=True)
class _ApiKey:
    identifier: str
    scopes: frozenset[str]
    token: str = field(default="", repr=False)
    sha256: str = field(default="", repr=False)


def _config_error(message: str) -> ValueError:
    return ValueError(f"Invalid {API_KEYS_ENV}: {message}")


@lru_cache(maxsize=16)
def _parse_api_keys(raw: str) -> tuple[_ApiKey, ...]:
    if not raw.strip():
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _config_error("must be valid JSON") from exc
    if not isinstance(payload, list):
        raise _config_error("must be a JSON array")
    if len(payload) > 100:
        raise _config_error("cannot contain more than 100 keys")

    records: list[_ApiKey] = []
    identifiers: set[str] = set()
    fingerprints: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise _config_error(f"entry {index} must be an object")
        unknown_fields = set(item) - {"id", "scopes", "token", "sha256"}
        if unknown_fields:
            raise _config_error(
                f"entry {index} contains unknown fields: {sorted(unknown_fields)}"
            )
        identifier = item.get("id")
        if not isinstance(identifier, str) or not _KEY_ID_RE.fullmatch(identifier):
            raise _config_error(f"entry {index} has an invalid id")
        if identifier in identifiers:
            raise _config_error(f"duplicate id {identifier!r}")

        scopes_value = item.get("scopes")
        if not isinstance(scopes_value, list) or not scopes_value:
            raise _config_error(f"entry {index} scopes must be a non-empty array")
        if not all(isinstance(scope, str) for scope in scopes_value):
            raise _config_error(f"entry {index} contains a non-string scope")
        scopes = frozenset(scopes_value)
        unknown = scopes - ALL_SCOPES
        if unknown:
            raise _config_error(f"entry {index} contains unknown scopes: {sorted(unknown)}")

        token = item.get("token", "")
        digest = item.get("sha256", "")
        if not isinstance(token, str) or not isinstance(digest, str):
            raise _config_error(f"entry {index} token/sha256 must be strings")
        token = token.strip()
        digest = digest.strip().lower()
        if bool(token) == bool(digest):
            raise _config_error(f"entry {index} must set exactly one of token or sha256")
        if token and len(token) < 16:
            raise _config_error(f"entry {index} token must contain at least 16 characters")
        if digest and not _SHA256_RE.fullmatch(digest):
            raise _config_error(f"entry {index} sha256 must be 64 lowercase hex characters")

        fingerprint = digest or hashlib.sha256(token.encode("utf-8")).hexdigest()
        if fingerprint in fingerprints:
            raise _config_error(f"entry {index} duplicates another credential")
        identifiers.add(identifier)
        fingerprints.add(fingerprint)
        records.append(_ApiKey(identifier, scopes, token=token, sha256=digest))
    return tuple(records)


def get_configured_api_token() -> str:
    """读取当前配置的 API token（未配置返回空串）。"""
    return (os.environ.get("DATAPAW_API_TOKEN") or "").strip()


def get_client_api_token() -> str:
    """Credential used by local CLI/service clients, separate from key registry."""
    return (
        (os.environ.get(CLIENT_API_TOKEN_ENV) or "").strip()
        or get_configured_api_token()
    )


def get_configured_api_keys() -> tuple[_ApiKey, ...]:
    return _parse_api_keys(os.environ.get(API_KEYS_ENV) or "")


def is_authentication_configured() -> bool:
    return bool(get_configured_api_token() or get_configured_api_keys())


def extract_bearer_token(auth_header: str | None) -> str:
    """Return a well-formed Bearer credential, otherwise an empty string."""
    scheme, separator, credential = (auth_header or "").partition(" ")
    if separator and scheme.lower() == "bearer":
        return credential.strip()
    return ""


def authenticate_api_token(candidate: str) -> AuthPrincipal | None:
    """Authenticate a candidate against legacy and scoped credentials."""
    if not candidate:
        return None
    expected = get_configured_api_token()
    if expected and secrets.compare_digest(candidate, expected):
        return AuthPrincipal("legacy-admin", ALL_SCOPES, "legacy")

    candidate_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    matched: _ApiKey | None = None
    for record in get_configured_api_keys():
        valid = (
            secrets.compare_digest(candidate, record.token)
            if record.token
            else secrets.compare_digest(candidate_digest, record.sha256)
        )
        if valid and matched is None:
            matched = record
    if matched is None:
        return None
    return AuthPrincipal(matched.identifier, matched.scopes, matched.identifier)


def is_valid_api_token(candidate: str) -> bool:
    return authenticate_api_token(candidate) is not None


def configured_bearer_headers() -> dict[str, str]:
    """Build service-to-service auth headers without exposing an empty token."""
    token = get_client_api_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _is_loopback_hostname(hostname: str | None) -> bool:
    host = (hostname or "").strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def internal_callback_auth_headers(callback_url: str) -> dict[str, str]:
    """Authenticate only the exact loopback callback owned by this service.

    User-supplied callbacks can point at third-party systems. Never forward the
    DataPaw API credential to those destinations.
    """
    parsed = urlparse(callback_url)
    trusted_origins = {
        origin.strip().rstrip("/").lower()
        for origin in (
            os.environ.get("DATAPAW_INTERNAL_CALLBACK_ORIGINS")
            or "http://127.0.0.1:8765,http://localhost:8765"
        ).split(",")
        if origin.strip()
    }
    origin = f"{parsed.scheme}://{parsed.netloc}".lower()
    if (
        parsed.scheme in {"http", "https"}
        and _is_loopback_hostname(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and origin in trusted_origins
        and parsed.path == "/api/semantic-config/weave-task/callback"
    ):
        return configured_bearer_headers()
    return {}


def install_api_token_auth(
    app: Starlette,
    *,
    enforce_scopes: bool = False,
    allowed_origins: list[str] | None = None,
) -> bool:
    """按环境变量在 ``app`` 上安装 Bearer token 认证中间件。

    返回是否启用了认证。token 在每个请求时读取，便于测试与热更新。
    注意：应在 CORS 中间件 *之前* 调用本函数（Starlette 后添加的
    中间件在外层），保证 401 响应仍带 CORS 头。
    """
    # Parse once during app construction so malformed registries fail closed at
    # startup rather than silently disabling authentication.
    enabled = is_authentication_configured()
    from .security import PRIVILEGED_SCOPES, SecurityControls

    controls = SecurityControls.from_env(allowed_origins)
    app.state.security_controls = controls
    if not enabled:
        log.warning(
            "未配置 DataPaw API 凭证：API 无认证运行，仅适合绑定 127.0.0.1 "
            "的本机部署；对外暴露前必须配置 DATAPAW_API_TOKEN 或 DATAPAW_API_KEYS。"
        )

    async def api_token_auth_middleware(request: Request, call_next):
        path = request.url.path
        started_at = time.monotonic()
        client_ip = controls.client_ip(request)
        incoming_request_id = (request.headers.get("x-request-id") or "").strip()
        request_id = (
            incoming_request_id
            if _REQUEST_ID_RE.fullmatch(incoming_request_id)
            else uuid4().hex
        )
        response_headers = {"X-Request-ID": request_id}

        host_rejection = controls.host_rejection(request)
        if host_rejection:
            controls.audit.emit(
                "host_rejected",
                outcome="denied",
                reason=host_rejection,
                status=400,
                request_id=request_id,
                client_ip=client_ip,
                method=request.method,
                path=path,
            )
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid Host header"},
                headers=response_headers,
            )

        if path in AUTH_EXEMPT_PATHS or request.method == "OPTIONS":
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response

        origin_rejection = controls.origin_rejection(request)
        if origin_rejection:
            controls.audit.emit(
                "csrf_rejected",
                outcome="denied",
                reason=origin_rejection,
                status=403,
                request_id=request_id,
                client_ip=client_ip,
                method=request.method,
                path=path,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin request rejected"},
                headers=response_headers,
            )

        if is_authentication_configured():
            retry_after = controls.auth_failures.retry_after(client_ip)
            if retry_after:
                controls.audit.emit(
                    "auth_rate_limited",
                    outcome="denied",
                    status=429,
                    request_id=request_id,
                    client_ip=client_ip,
                    method=request.method,
                    path=path,
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many authentication failures"},
                    headers={**response_headers, "Retry-After": str(retry_after)},
                )
            credential = extract_bearer_token(request.headers.get("authorization"))
            principal = authenticate_api_token(credential)
            if principal is None:
                controls.auth_failures.record_failure(client_ip)
                controls.audit.emit(
                    "auth_failure",
                    outcome="denied",
                    reason="invalid_token" if credential else "missing_token",
                    status=401,
                    request_id=request_id,
                    client_ip=client_ip,
                    method=request.method,
                    path=path,
                )
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid API token"},
                    headers={**response_headers, "WWW-Authenticate": "Bearer"},
                )
        else:
            principal = AuthPrincipal("local-unsecured", ALL_SCOPES, "local")
        request.state.auth_principal = principal

        required: frozenset[str] = frozenset()
        if enforce_scopes and (path.startswith("/api/") or path.startswith("/mcp/")):
            from .authorization import required_scopes_for_request

            required = required_scopes_for_request(request.method, path)
            if required is None:
                log.error(
                    "request denied: no authorization policy for %s %s",
                    request.method,
                    path,
                )
                controls.audit.emit(
                    "authorization_denied",
                    outcome="denied",
                    reason="unclassified_route",
                    status=403,
                    request_id=request_id,
                    client_ip=client_ip,
                    principal=principal.subject,
                    method=request.method,
                    path=path,
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "No authorization policy for this endpoint"},
                    headers=response_headers,
                )
            if not principal.has_scopes(required):
                scope_header = " ".join(sorted(required))
                controls.audit.emit(
                    "authorization_denied",
                    outcome="denied",
                    reason="insufficient_scope",
                    status=403,
                    request_id=request_id,
                    client_ip=client_ip,
                    principal=principal.subject,
                    method=request.method,
                    path=path,
                    required_scopes=sorted(required),
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Insufficient scope",
                        "required_scopes": sorted(required),
                    },
                    headers={
                        **response_headers,
                        "WWW-Authenticate": 'Bearer error="insufficient_scope", '
                        f'scope="{scope_header}"'
                    },
                )

        bucket_name, rate = controls.consume_rate(
            principal=principal.credential_id,
            client_ip=client_ip,
            required_scopes=required,
        )
        response_headers.update(
            {
                "RateLimit-Limit": str(rate.limit),
                "RateLimit-Remaining": str(rate.remaining),
            }
        )
        if not rate.allowed:
            controls.audit.emit(
                "request_rate_limited",
                outcome="denied",
                status=429,
                request_id=request_id,
                client_ip=client_ip,
                principal=principal.subject,
                method=request.method,
                path=path,
                rate_bucket=bucket_name,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={**response_headers, "Retry-After": str(rate.retry_after)},
            )

        try:
            response = await call_next(request)
        except Exception:
            if required & PRIVILEGED_SCOPES:
                controls.audit.emit(
                    "privileged_request",
                    outcome="failure",
                    status=500,
                    request_id=request_id,
                    client_ip=client_ip,
                    principal=principal.subject,
                    method=request.method,
                    path=path,
                    required_scopes=sorted(required),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
            raise
        for header, value in response_headers.items():
            response.headers[header] = value
        if required & PRIVILEGED_SCOPES:
            controls.audit.emit(
                "privileged_request",
                outcome="success" if response.status_code < 400 else "failure",
                status=response.status_code,
                request_id=request_id,
                client_ip=client_ip,
                principal=principal.subject,
                method=request.method,
                path=path,
                required_scopes=sorted(required),
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=api_token_auth_middleware)

    if enabled:
        log.info("API token authentication and scoped principal resolution enabled")
    return enabled

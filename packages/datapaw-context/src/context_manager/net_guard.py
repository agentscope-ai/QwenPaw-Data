"""SSRF-safe outbound HTTP callbacks.

Callbacks are deny-by-default. External destinations must match an exact
``DATAPAW_CALLBACK_ALLOWLIST`` origin (scheme, host and port). The built-in
loopback weave callback is allowed only on its fixed path. Hostnames are
resolved and validated immediately before the request, then an aiohttp
resolver pins the connection to those exact IPs so DNS rebinding cannot swap
in an internal address between validation and connect.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver

__all__ = [
    "CallbackRequestError",
    "CallbackUrlError",
    "ensure_safe_callback_url",
    "post_safe_callback",
]

_DEFAULT_INTERNAL_CALLBACK_ORIGINS = (
    "http://127.0.0.1:8765",
    "http://localhost:8765",
)
_INTERNAL_CALLBACK_PATH = "/api/semantic-config/weave-task/callback"
_MAX_URL_LENGTH = 2048


class CallbackUrlError(ValueError):
    """A callback URL or callback policy is invalid."""


class CallbackRequestError(RuntimeError):
    """A validated callback could not be sent within its safety limits."""


@dataclass(frozen=True)
class _CallbackTarget:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class CallbackResult:
    status_code: int


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise CallbackUrlError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise CallbackUrlError(f"{name} must be between {minimum} and {maximum}")
    return value


def _canonical_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise CallbackUrlError(
            "callback allowlist entries must be exact HTTP(S) origins"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise CallbackUrlError(
            "callback allowlist origin has an invalid port"
        ) from exc
    hostname = parsed.hostname.lower()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if parsed.scheme == "http" else 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{rendered_host}{suffix}"


def _allowed_origins() -> frozenset[str]:
    origins: set[str] = set()
    for entry in (os.environ.get("DATAPAW_CALLBACK_ALLOWLIST") or "").split(","):
        if entry.strip():
            origins.add(_canonical_origin(entry))
    return frozenset(origins)


def _internal_callback_urls() -> frozenset[str]:
    raw = os.environ.get("DATAPAW_INTERNAL_CALLBACK_ORIGINS")
    values = raw.split(",") if raw else _DEFAULT_INTERNAL_CALLBACK_ORIGINS
    urls: set[str] = set()
    for value in values:
        if not value.strip():
            continue
        origin = _canonical_origin(value)
        hostname = urlsplit(origin).hostname or ""
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = hostname == "localhost"
        if not is_loopback:
            raise CallbackUrlError(
                "DATAPAW_INTERNAL_CALLBACK_ORIGINS must contain only loopback origins"
            )
        urls.add(f"{origin}{_INTERNAL_CALLBACK_PATH}")
    return frozenset(urls)


def _normalize_callback_url(url: str) -> tuple[str, str, int, bool]:
    value = (url or "").strip()
    if not value or len(value) > _MAX_URL_LENGTH:
        raise CallbackUrlError("callback_url is empty or too long")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise CallbackUrlError("callback_url must use http:// or https://")
    if not parsed.hostname:
        raise CallbackUrlError("callback_url has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise CallbackUrlError("callback_url must not contain credentials")
    if parsed.fragment:
        raise CallbackUrlError("callback_url must not contain a fragment")
    try:
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
    except ValueError as exc:
        raise CallbackUrlError("callback_url has an invalid port") from exc

    hostname = parsed.hostname.lower()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if parsed.scheme == "http" else 443
    suffix = f":{port}" if port != default_port else ""
    origin = f"{parsed.scheme}://{rendered_host}{suffix}"
    normalized = urlunsplit(
        (
            parsed.scheme,
            origin.removeprefix(f"{parsed.scheme}://"),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )
    internal = normalized in _internal_callback_urls()
    if not internal and origin not in _allowed_origins():
        raise CallbackUrlError(
            "callback origin is not allowed; add the exact origin to "
            "DATAPAW_CALLBACK_ALLOWLIST"
        )
    return normalized, hostname, port, internal


def _resolve_target(url: str) -> _CallbackTarget:
    normalized, hostname, port, internal = _normalize_callback_url(url)
    try:
        infos = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise CallbackUrlError(
            f"callback_url hostname cannot be resolved: {hostname}"
        ) from exc

    addresses: list[str] = []
    literal_host = False
    try:
        literal_host = ipaddress.ip_address(hostname).compressed == hostname
    except ValueError:
        pass

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        # Only the fixed built-in callback path, or an explicitly allowlisted
        # private IP literal, may reach a non-public address. A hostname entry
        # can therefore never rebind to loopback/private/metadata space.
        private_literal = literal_host and _canonical_origin(
            f"{urlsplit(normalized).scheme}://{rendered_ip(address)}:{port}"
        ) in _allowed_origins()
        if internal and not address.is_loopback:
            raise CallbackUrlError(
                f"internal callback resolves outside loopback ({address})"
            )
        if not address.is_global and not internal and not private_literal:
            raise CallbackUrlError(
                f"callback_url resolves to a non-public address ({address})"
            )
        rendered = str(address)
        if rendered not in addresses:
            addresses.append(rendered)
    if not addresses:
        raise CallbackUrlError("callback_url did not resolve to any address")
    return _CallbackTarget(normalized, hostname, port, tuple(addresses))


def rendered_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    return f"[{address}]" if address.version == 6 else str(address)


def ensure_safe_callback_url(url: str) -> str:
    """Validate policy and current DNS answers, returning a normalized URL."""
    return _resolve_target(url).url


class _PinnedResolver(AbstractResolver):
    """Resolve exactly one expected hostname to a previously validated IP set."""

    def __init__(self, hostname: str, addresses: tuple[str, ...]) -> None:
        self.hostname = hostname.lower()
        self.addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        if host.lower() != self.hostname:
            raise OSError("pinned callback resolver received an unexpected hostname")
        results: list[dict[str, Any]] = []
        for value in self.addresses:
            address = ipaddress.ip_address(value)
            address_family = (
                socket.AF_INET6 if address.version == 6 else socket.AF_INET
            )
            if family not in {socket.AF_UNSPEC, address_family}:
                continue
            results.append(
                {
                    "hostname": host,
                    "host": value,
                    "port": port,
                    "family": address_family,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not results:
            raise OSError("no pinned callback address matches the requested family")
        return results

    async def close(self) -> None:
        return None


async def _read_limited(response: aiohttp.ClientResponse, maximum: int) -> None:
    content_length = response.content_length
    if content_length is not None and content_length > maximum:
        raise CallbackRequestError("callback response exceeds the configured limit")
    received = 0
    async for chunk in response.content.iter_chunked(min(16_384, maximum + 1)):
        received += len(chunk)
        if received > maximum:
            raise CallbackRequestError("callback response exceeds the configured limit")


async def post_safe_callback(
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> CallbackResult:
    """POST a callback using DNS pinning and strict resource limits."""
    target = _resolve_target(url)
    max_request = _env_int(
        "DATAPAW_CALLBACK_MAX_REQUEST_BYTES",
        65_536,
        minimum=1_024,
        maximum=1_048_576,
    )
    max_response = _env_int(
        "DATAPAW_CALLBACK_MAX_RESPONSE_BYTES",
        65_536,
        minimum=1_024,
        maximum=1_048_576,
    )
    total_timeout = _env_int(
        "DATAPAW_CALLBACK_TIMEOUT_SECONDS",
        10,
        minimum=1,
        maximum=60,
    )
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > max_request:
        raise CallbackRequestError("callback request exceeds the configured limit")

    resolver = _PinnedResolver(target.hostname, target.addresses)
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=True,
        ttl_dns_cache=None,
        limit=4,
    )
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    timeout = aiohttp.ClientTimeout(total=total_timeout)
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            trust_env=False,
            auto_decompress=False,
        ) as client:
            async with client.post(
                target.url,
                data=body,
                headers=request_headers,
                allow_redirects=False,
            ) as response:
                await _read_limited(response, max_response)
                return CallbackResult(response.status)
    except CallbackRequestError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise CallbackRequestError(f"callback request failed: {type(exc).__name__}") from exc

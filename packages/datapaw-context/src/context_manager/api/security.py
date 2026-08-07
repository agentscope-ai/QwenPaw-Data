"""HTTP security controls: origin checks, throttling, trusted proxies, audit."""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from datapaw.context.paths import security_audit_log_path

from ..secrets.redact import CredentialRedactFilter

log = logging.getLogger("api.security")

DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
DEFAULT_ALLOWED_HOSTS = (
    "localhost",
    "127.0.0.1",
    "::1",
    # Default DockerWorkspace reaches the loopback-bound API through this
    # standard host-gateway alias.  The API still requires bearer auth when
    # configured; this only makes the default Host-header policy internally
    # consistent with the default workspace backend.
    "host.docker.internal",
)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PRIVILEGED_SCOPES = frozenset({"write", "manage", "credentials:manage"})


def _normalize_origin(value: str) -> str:
    origin = value.strip()
    if origin in {"*", "null"}:
        raise ValueError("CORS origins must be explicit; '*' and 'null' are forbidden")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"invalid CORS origin: {origin!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid CORS origin port: {origin!r}") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if parsed.scheme == "http" else 443
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{host}{port_suffix}"


def configured_cors_origins(raw: str | None = None) -> list[str]:
    """Parse and validate an explicit origin allowlist."""
    source = raw if raw is not None else os.environ.get("DATAPAW_CORS_ORIGINS")
    values = source.split(",") if source else list(DEFAULT_CORS_ORIGINS)
    origins = [_normalize_origin(value) for value in values if value.strip()]
    if not origins:
        raise ValueError("CORS origin allowlist cannot be empty")
    return list(dict.fromkeys(origins))


def configured_api_hosts(raw: str | None = None) -> list[str]:
    """Parse an explicit Host allowlist used to resist DNS rebinding."""
    source = raw if raw is not None else os.environ.get("DATAPAW_API_ALLOWED_HOSTS")
    values = source.split(",") if source else list(DEFAULT_ALLOWED_HOSTS)
    hosts: list[str] = []
    for value in values:
        host = value.strip().lower()
        if not host:
            continue
        if (
            host == "*"
            or any(character in host for character in "/@?#")
            or "://" in host
            or any(character.isspace() for character in host)
        ):
            raise ValueError(f"invalid DATAPAW_API_ALLOWED_HOSTS entry: {value!r}")
        try:
            ip_literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            ip_literal = None
        if ip_literal is not None:
            hosts.append(str(ip_literal))
            continue
        parsed = urlsplit(f"//{host}")
        if not parsed.hostname:
            raise ValueError(f"invalid DATAPAW_API_ALLOWED_HOSTS entry: {value!r}")
        try:
            if parsed.port is not None:
                raise ValueError("host entries must not include ports")
        except ValueError as exc:
            raise ValueError(f"invalid DATAPAW_API_ALLOWED_HOSTS entry: {value!r}") from exc
        hosts.append(parsed.hostname.lower())
    if not hosts:
        raise ValueError("API Host allowlist cannot be empty")
    return list(dict.fromkeys(hosts))


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 1_000_000,
) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _trusted_proxy_networks(
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in (os.environ.get("DATAPAW_TRUSTED_PROXIES") or "").split(","):
        if not value.strip():
            continue
        try:
            networks.append(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid DATAPAW_TRUSTED_PROXIES entry: {value!r}") from exc
    if len(networks) > 100:
        raise ValueError("DATAPAW_TRUSTED_PROXIES cannot contain more than 100 entries")
    return tuple(networks)


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int = 0


class FailureLimiter:
    """Bounded sliding-window penalty box for failed authentication."""

    def __init__(self, limit: int, window_seconds: int, *, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, events: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        with self._lock:
            events = self._events.get(key)
            if not events:
                return 0
            self._prune(events, current)
            if not events:
                self._events.pop(key, None)
                return 0
            self._events.move_to_end(key)
            if len(events) < self.limit:
                return 0
            return max(1, math.ceil(self.window_seconds - (current - events[0])))

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            events = self._events.setdefault(key, deque())
            self._prune(events, current)
            events.append(current)
            self._events.move_to_end(key)
            while len(self._events) > self.max_keys:
                self._events.popitem(last=False)


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """Bounded per-principal token buckets with a one-window full refill."""

    def __init__(self, *, max_keys: int = 10_000) -> None:
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def consume(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
        now: float | None = None,
    ) -> RateDecision:
        current = time.monotonic() if now is None else now
        refill_per_second = limit / window_seconds
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(float(limit), current)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, current - bucket.updated_at)
                bucket.tokens = min(float(limit), bucket.tokens + elapsed * refill_per_second)
                bucket.updated_at = current
                self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)

            if bucket.tokens < 1.0:
                retry = max(1, math.ceil((1.0 - bucket.tokens) / refill_per_second))
                return RateDecision(False, limit, 0, retry)
            bucket.tokens -= 1.0
            return RateDecision(True, limit, max(0, int(bucket.tokens)))


class _RestrictedRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        Path(self.baseFilename).chmod(0o600)
        return stream


class SecurityAudit:
    """Append-only JSONL audit writer with rotation and restrictive permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._logger: logging.Logger | None = None
        self._lock = threading.Lock()

    def _get_logger(self) -> logging.Logger:
        if self._logger is not None:
            return self._logger
        with self._lock:
            if self._logger is not None:
                return self._logger
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.touch(mode=0o600, exist_ok=True)
            self.path.chmod(0o600)
            audit_logger = logging.getLogger(f"datapaw.security.audit.{self.path}")
            audit_logger.setLevel(logging.INFO)
            audit_logger.propagate = False
            if not audit_logger.handlers:
                handler = _RestrictedRotatingFileHandler(
                    self.path,
                    maxBytes=10 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                handler.addFilter(CredentialRedactFilter())
                audit_logger.addHandler(handler)
            self._logger = audit_logger
            return audit_logger

    def emit(self, event: str, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
        }
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, str):
                payload[key] = value.replace("\r", " ").replace("\n", " ")[:500]
            elif isinstance(value, (bool, int, float, list)):
                payload[key] = value
            else:
                payload[key] = str(value)[:500]
        try:
            self._get_logger().info(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
        except OSError:
            log.exception("failed to write security audit event %s", event)


@dataclass
class SecurityControls:
    allowed_origins: frozenset[str]
    allowed_hosts: frozenset[str]
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    auth_failures: FailureLimiter
    rate_limiter: TokenBucketLimiter
    audit: SecurityAudit
    rate_window_seconds: int
    scope_limits: dict[str, int]

    @classmethod
    def from_env(cls, allowed_origins: list[str] | None = None) -> "SecurityControls":
        origins = (
            configured_cors_origins()
            if allowed_origins is None
            else configured_cors_origins(",".join(allowed_origins))
        )
        audit_path = Path(
            os.environ.get("DATAPAW_SECURITY_AUDIT_LOG") or security_audit_log_path()
        ).expanduser()
        return cls(
            allowed_origins=frozenset(origins),
            allowed_hosts=frozenset(configured_api_hosts()),
            trusted_proxies=_trusted_proxy_networks(),
            auth_failures=FailureLimiter(
                _env_int("DATAPAW_AUTH_FAILURE_LIMIT", 10),
                _env_int("DATAPAW_AUTH_FAILURE_WINDOW_SECONDS", 60),
            ),
            rate_limiter=TokenBucketLimiter(),
            audit=SecurityAudit(audit_path),
            rate_window_seconds=_env_int("DATAPAW_RATE_LIMIT_WINDOW_SECONDS", 60),
            scope_limits={
                "query": _env_int("DATAPAW_RATE_LIMIT_QUERY", 120),
                "write": _env_int("DATAPAW_RATE_LIMIT_WRITE", 60),
                "manage": _env_int("DATAPAW_RATE_LIMIT_MANAGE", 30),
                "credentials:manage": _env_int("DATAPAW_RATE_LIMIT_CREDENTIALS", 20),
                "authenticated": _env_int("DATAPAW_RATE_LIMIT_AUTHENTICATED", 60),
            },
        )

    def client_ip(self, request: Any) -> str:
        direct = request.client.host if request.client else "unknown"
        try:
            direct_ip = ipaddress.ip_address(direct)
        except ValueError:
            return direct

        def trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return any(address in network for network in self.trusted_proxies)

        if not trusted(direct_ip):
            return direct
        forwarded = request.headers.get("x-forwarded-for") or ""
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if not hops:
            return direct
        current = direct_ip
        for hop in reversed(hops):
            if not trusted(current):
                break
            try:
                current = ipaddress.ip_address(hop)
            except ValueError:
                return str(current)
        return str(current)

    def host_rejection(self, request: Any) -> str | None:
        raw_host = (request.headers.get("host") or "").strip()
        if not raw_host:
            return "missing_host"
        if any(character in raw_host for character in "/@?#"):
            return "invalid_host"
        try:
            parsed = urlsplit(f"//{raw_host}")
            hostname = parsed.hostname
            parsed.port
        except ValueError:
            return "invalid_host"
        if not hostname:
            return "invalid_host"
        return None if hostname.lower() in self.allowed_hosts else "host_not_allowed"

    def origin_rejection(self, request: Any) -> str | None:
        if request.method.upper() not in UNSAFE_METHODS:
            return None
        origin = request.headers.get("origin")
        if origin:
            try:
                normalized = _normalize_origin(origin)
            except ValueError:
                return "invalid_origin"
            host = request.headers.get("host") or ""
            try:
                request_origin = _normalize_origin(f"{request.url.scheme}://{host}")
            except ValueError:
                request_origin = ""
            return (
                None
                if normalized == request_origin or normalized in self.allowed_origins
                else "origin_not_allowed"
            )
        fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            return "cross_site_without_allowed_origin"
        return None

    def consume_rate(
        self,
        *,
        principal: str,
        client_ip: str,
        required_scopes: frozenset[str],
    ) -> tuple[str, RateDecision]:
        if "credentials:manage" in required_scopes:
            bucket_name = "credentials:manage"
        elif "manage" in required_scopes:
            bucket_name = "manage"
        elif "write" in required_scopes:
            bucket_name = "write"
        elif "query" in required_scopes:
            bucket_name = "query"
        else:
            bucket_name = "authenticated"
        decision = self.rate_limiter.consume(
            f"{principal}:{client_ip}:{bucket_name}",
            limit=self.scope_limits[bucket_name],
            window_seconds=self.rate_window_seconds,
        )
        return bucket_name, decision


__all__ = [
    "DEFAULT_ALLOWED_HOSTS",
    "DEFAULT_CORS_ORIGINS",
    "FailureLimiter",
    "PRIVILEGED_SCOPES",
    "SecurityControls",
    "TokenBucketLimiter",
    "configured_api_hosts",
    "configured_cors_origins",
]

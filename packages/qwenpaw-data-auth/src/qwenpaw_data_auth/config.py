"""Auth service configuration."""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger("qwenpaw_data_auth.config")


def runtime_home() -> Path:
    """QWENPAW_DATA_HOME: generated secrets and the default local database."""
    raw = os.environ.get("QWENPAW_DATA_HOME") or str(Path.home() / ".qwenpaw-data")
    if raw != raw.strip():
        raise ValueError("QWENPAW_DATA_HOME must be trimmed when configured")
    home = Path(raw).expanduser()
    if not home.is_absolute():
        raise ValueError("QWENPAW_DATA_HOME must be an absolute path")
    return home.resolve()


def local_master_secret(home: Path) -> str:
    """Return the generated preferences key that Host and Context share for one home."""
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = home / "preferences.key"
    if key_path.exists():
        return key_path.read_text().strip()
    with tempfile.NamedTemporaryFile(
        mode="w", dir=home, prefix=".preferences-key.", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(secrets.token_hex(32))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        # Publish only a complete key, without replacing another process's key.
        try:
            os.link(temporary, key_path)
        except FileExistsError:
            pass
        return key_path.read_text().strip()
    finally:
        temporary.unlink(missing_ok=True)


def runtime_mode() -> str:
    """``local`` (the default) or ``multi-user``; only this selects login."""
    return os.environ.get("QWENPAW_DATA_AUTH_MODE", "local").strip().lower()


# Login cookie and temporary-access lifetime (24h).
ACCESS_TOKEN_TTL_DEFAULT = 86400
# Refresh-token lifetime (7d); also the max for QWENPAW_DATA_ACCESS_TOKEN_TTL.
REFRESH_TOKEN_TTL_DEFAULT = 604800


def _bounded_int_env(name: str, default: int, maximum: int) -> int:
    """Read a positive int from the environment, clamped to ``maximum``; invalid values use ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning(
            "%s must be positive, got %r; using default %s", name, raw, default
        )
        return default
    if value > maximum:
        logger.warning("%s=%r exceeds the %s ceiling; clamping", name, raw, maximum)
        return maximum
    return value


@dataclass(frozen=True)
class AuthConfig:
    secret_keys: list[str] = field(default_factory=list, repr=False)
    control_plane_dsn: str = field(default="", repr=False)
    # Machine-to-machine credential for POST /api/auth/temporary-access.  Kept
    # separate from ``secret_keys`` on purpose: that key signs tokens and never
    # travels over the wire, whereas this one is sent on every service call.
    service_secret: str = field(default="", repr=False)
    auth_url: str = ""
    cm_base_url: str = "http://127.0.0.1:8765"
    access_token_ttl: int = ACCESS_TOKEN_TTL_DEFAULT
    refresh_token_ttl: int = REFRESH_TOKEN_TTL_DEFAULT
    cors_allowed_origins: list[str] = field(default_factory=list)
    cookie_domain: str | None = None
    cookie_secure: bool = True
    env: str = "development"
    platform_bootstrap_token: str = field(default="", repr=False)
    allow_legacy_first_user_registration: bool = False
    authorization_redirect_origins: list[str] = field(default_factory=list)
    workspace_endpoint_allowed_hosts: list[str] = field(default_factory=list)
    # Static machine credential (QWENPAW_DATA_API_TOKEN). It identifies the
    # trusted local principal and never signs tokens.
    api_token: str = field(default="", repr=False)
    # ``local`` serves one loopback user without login; ``multi-user`` requires
    # sign-in and RBAC. Generated runtime secrets never select the mode.
    mode: Literal["local", "multi-user"] = field(default_factory=runtime_mode)
    home: Path = field(default_factory=runtime_home)

    def __post_init__(self) -> None:
        if self.mode not in {"local", "multi-user"}:
            raise ValueError("QWENPAW_DATA_AUTH_MODE must be local or multi-user")
        if str(self.home) != str(self.home).strip():
            raise ValueError("QWENPAW_DATA_HOME must be trimmed when configured")
        home = Path(self.home).expanduser()
        if not home.is_absolute():
            raise ValueError("QWENPAW_DATA_HOME must be an absolute path")
        object.__setattr__(self, "home", home.resolve())

    @staticmethod
    def from_env() -> "AuthConfig":
        secret_raw = os.environ.get("QWENPAW_DATA_AUTH_SECRET", "")
        secrets = [s.strip() for s in secret_raw.split(",") if s.strip()]
        env = os.environ.get("QWENPAW_DATA_ENV", "development")
        cors_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "")
        cors = [o.strip() for o in cors_raw.split(",") if o.strip()]
        redirect_raw = os.environ.get("AUTH_ALLOWED_REDIRECT_ORIGINS", cors_raw)
        redirect_origins = [
            origin.strip().rstrip("/")
            for origin in redirect_raw.split(",")
            if origin.strip()
        ]
        endpoint_hosts_raw = os.environ.get(
            "QWENPAW_DATA_WORKSPACE_ENDPOINT_ALLOWED_HOSTS",
            "127.0.0.1,localhost" if env == "development" else "",
        )

        return AuthConfig(
            secret_keys=secrets,
            service_secret=os.environ.get("QWENPAW_DATA_SERVICE_SECRET", "").strip(),
            api_token=os.environ.get("QWENPAW_DATA_API_TOKEN", "").strip(),
            mode=runtime_mode(),
            auth_url=os.environ.get("QWENPAW_DATA_AUTH_URL", "").strip().rstrip("/"),
            cm_base_url=os.environ.get("QWENPAW_DATA_CM_BASE_URL", "http://127.0.0.1:8765")
            .strip()
            .rstrip("/"),
            # Unified across host/sc/profiling/auth: every control-plane store
            # lives in the single database selected by QWENPAW_DATA_DB_URL.
            # database.py normalises the value (async driver / bare path /
            # sqlite:///) to a sync URL.
            control_plane_dsn=os.environ.get("QWENPAW_DATA_DB_URL", ""),
            access_token_ttl=_bounded_int_env(
                "QWENPAW_DATA_ACCESS_TOKEN_TTL",
                ACCESS_TOKEN_TTL_DEFAULT,
                REFRESH_TOKEN_TTL_DEFAULT,
            ),
            refresh_token_ttl=REFRESH_TOKEN_TTL_DEFAULT,
            cors_allowed_origins=cors,
            cookie_domain=os.environ.get("AUTH_COOKIE_DOMAIN"),
            cookie_secure=env != "development",
            env=env,
            platform_bootstrap_token=os.environ.get(
                "QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN", ""
            ),
            allow_legacy_first_user_registration=os.environ.get(
                "QWENPAW_DATA_ALLOW_LEGACY_FIRST_USER_REGISTRATION", ""
            ).lower()
            in {"1", "true", "yes", "on"},
            authorization_redirect_origins=redirect_origins,
            workspace_endpoint_allowed_hosts=[
                host.strip().lower()
                for host in endpoint_hosts_raw.split(",")
                if host.strip()
            ],
        )

    @property
    def local_mode(self) -> bool:
        """Single-user loopback mode: no login, one local administrator."""
        return self.mode == "local"

    @property
    def auth_enabled(self) -> bool:
        """Whether consumers should present normal multi-user login.

        Only an explicit ``multi-user`` mode enables it; generated runtime
        secrets exist in both modes and never turn login on.
        """
        return not self.local_mode

    @property
    def service_tokens_enabled(self) -> bool:
        """Whether services may ask Auth to co-sign an access token."""
        return bool(self.secret_keys and self.service_secret)

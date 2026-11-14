"""Persistent runtime bootstrap and narrowly scoped loopback authentication."""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import insert, select
from sqlalchemy.engine import make_url

from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import (get_db, init_schema, new_id, query_one,
                                        role_bindings, tenants, users, utcnow_iso, workspaces)
from qwenpaw_data_auth.password import hash_password
from qwenpaw_data_auth.principal import DEFAULT_PRINCIPAL

_bootstrap_lock = threading.RLock()


@contextmanager
def _runtime_lock(home: Path):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = home / "auth"
    if directory.is_symlink():
        raise RuntimeError("Auth runtime directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    with _bootstrap_lock:
        fd = os.open(directory / "bootstrap.lock", flags, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory
        finally:
            os.close(fd)


def _runtime_secrets(directory: Path) -> dict:
    path = directory / "secrets.json"
    if path.is_symlink():
        raise OSError("Auth secrets must not be a symlink")
    if path.exists():
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RuntimeError("Auth secrets must be a regular file")
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            data = json.load(stream)
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or len(data[key]) < 43 for key in ("signing_secret", "service_secret")):
            raise RuntimeError("Invalid persisted auth secrets; refusing to rotate silently")
        return data
    data = {"version": 1, "signing_secret": secrets.token_urlsafe(48), "service_secret": secrets.token_urlsafe(48)}
    fd, temporary = tempfile.mkstemp(prefix=".secrets-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data


def prepare_auth_config(cfg: AuthConfig | None = None) -> AuthConfig:
    """Resolve persistent secrets and the local DB, without creating an identity."""
    cfg = cfg or AuthConfig.from_env()
    with _runtime_lock(cfg.home) as directory:
        saved = _runtime_secrets(directory)
    dsn = cfg.control_plane_dsn or str(cfg.home / "control_plane.db")
    if "://" not in dsn:
        path = Path(dsn).expanduser()
        dsn = str(path if path.is_absolute() else cfg.home / path)
    else:
        url = make_url(dsn)
        if url.get_backend_name() == "sqlite" and url.database not in {None, "", ":memory:"}:
            path = Path(url.database).expanduser()
            if not path.is_absolute():
                path = cfg.home / path
            dsn = url.set(database=str(path)).render_as_string(hide_password=False)
    return replace(cfg, control_plane_dsn=dsn,
                   secret_keys=cfg.secret_keys or [saved["signing_secret"]],
                   service_secret=cfg.service_secret or saved["service_secret"])


def bootstrap_local(cfg: AuthConfig | None = None) -> AuthConfig:
    """Idempotently seed the reserved local admin, tenant, workspace and binding.

    No reusable password is stored or returned. Existing account status, role
    removals and password changes are never undone on restart. Multi-user mode
    initializes schema/secrets only and never seeds a local administrator.
    """
    cfg = prepare_auth_config(cfg)
    with _runtime_lock(cfg.home):
        init_schema(cfg.control_plane_dsn)
        if not cfg.local_mode:
            return cfg
        with get_db(cfg.control_plane_dsn) as conn:
            if conn._c.dialect.name == "sqlite":
                conn.execute("BEGIN IMMEDIATE")
            elif "hologres.aliyuncs.com" not in (conn._c.engine.url.host or ""):
                conn.execute("SELECT pg_advisory_xact_lock(716234019)")
            tenant = query_one(conn, select(tenants).where(tenants.c.id == "default"))
            if not tenant:
                conn.execute(insert(tenants).values(id="default", name="Default Tenant", neo4j_database="neo4j"))
            workspace = query_one(conn, select(workspaces).where(workspaces.c.id == "default"))
            if workspace and workspace["tenant_id"] != "default":
                raise RuntimeError("Reserved local workspace belongs to another tenant")
            if not workspace:
                conn.execute(insert(workspaces).values(id="default", tenant_id="default", name="Default Workspace", description="Local workspace", instance_status="ready"))
            user = query_one(conn, select(users).where(users.c.id == DEFAULT_PRINCIPAL.user_id))
            if user and user["tenant_id"] != "default":
                raise RuntimeError("Reserved local identity belongs to another tenant")
            if not user:
                conn.execute(insert(users).values(
                    id=DEFAULT_PRINCIPAL.user_id, tenant_id="default", username="local_admin",
                    display_name="Local Administrator", password_hash=hash_password(secrets.token_urlsafe(48)),
                    password_changed_at=utcnow_iso(), active_workspace_id="default"))
                conn.execute(insert(role_bindings).values(id=new_id(), user_id=DEFAULT_PRINCIPAL.user_id,
                    role="tenant_admin", scope_type="tenant", scope_id="default"))
            conn.commit()
    return cfg


def _loopback_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _local_hostname(value: str | None) -> bool:
    return bool(value and (value.lower() == "localhost" or _loopback_ip(value)))


def _safe_local_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        return (url.scheme in {"http", "https"} and _local_hostname(url.hostname)
                and url.username is None and url.password is None and url.port != 0)
    except ValueError:
        return False


def is_safe_loopback_request(request: Request) -> bool:
    """Use the actual ASGI peer, not forwarded headers; defend against rebinding."""
    if not request.client or not _loopback_ip(request.client.host):
        return False
    if any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")):
        return False
    hosts = request.headers.getlist("host")
    if len(hosts) != 1 or any(character in hosts[0] for character in "/?#\\\\ \t\r\n"):
        return False
    if not _safe_local_url(f"{request.url.scheme}://{hosts[0]}"):
        return False
    for name in ("origin", "referer"):
        values = request.headers.getlist(name)
        if len(values) > 1 or (values and not _safe_local_url(values[0])):
            return False
    return request.headers.get("sec-fetch-site", "") not in {"cross-site"}


def service_secret_ok(request: Request, cfg: AuthConfig) -> bool:
    values = request.headers.getlist("x-qwenpaw-data-service-secret")
    return bool(len(values) == 1 and values[0] and cfg.service_secret
                and secrets.compare_digest(values[0].encode(), cfg.service_secret.encode()))


def _local_payload() -> dict:
    return {"sub": DEFAULT_PRINCIPAL.user_id, "tenant_id": "default", "active_ws": "default",
            "typ": "access", "iat": time.time(), "local": True}


def request_identity_payload(request: Request, cfg: AuthConfig, *, allow_machine: bool = False) -> dict:
    """Fail closed on supplied credentials; delegation requires an opted-in path.

    API-token access is a local identity credential, not a token signing key.
    A validated service secret may delegate X-User-Id only with allow_machine.
    """
    from qwenpaw_data_auth.authentication import AuthenticationError
    from qwenpaw_data_auth.token import TokenVerifier, TokenVerifyError

    authorization = request.headers.get("authorization")
    cookie = request.cookies.get("qwenpaw_data_access")
    service_supplied = "x-qwenpaw-data-service-secret" in request.headers
    api_supplied = request.headers.get("x-qwenpaw-data-api-token")
    api_candidate = api_supplied
    if authorization is not None and authorization.lower().startswith("bearer "):
        api_candidate = authorization[7:]
    api_valid = bool(cfg.api_token and api_candidate and secrets.compare_digest(api_candidate.encode(), cfg.api_token.encode()))
    if len(request.headers.getlist("authorization")) > 1 or len(request.headers.getlist("x-qwenpaw-data-api-token")) > 1:
        raise AuthenticationError("Ambiguous credentials")
    if service_supplied:
        if not allow_machine or not service_secret_ok(request, cfg):
            raise AuthenticationError("Invalid or unauthorized service credential")
        if authorization is not None or cookie is not None or api_supplied is not None:
            raise AuthenticationError("Conflicting credentials")
        delegated = request.headers.get("x-user-id")
        if delegated:
            with get_db(cfg.control_plane_dsn) as conn:
                user = query_one(conn, select(users).where(users.c.id == delegated))
            if not user:
                raise AuthenticationError("Delegated user not found", 403)
            return {"sub": user["id"], "tenant_id": user["tenant_id"], "typ": "access", "iat": time.time(),
                    "active_ws": request.headers.get("x-qwenpaw-data-workspace-id") or user.get("active_workspace_id") or "",
                    "machine": True}
        if not cfg.local_mode:
            raise AuthenticationError("A delegated user is required", 403)
        return {**_local_payload(), "machine": True}
    if "x-user-id" in request.headers:
        raise AuthenticationError("User delegation requires an authorized machine path")
    if api_supplied is not None and (not cfg.api_token or not secrets.compare_digest(api_supplied.encode(), cfg.api_token.encode())):
        raise AuthenticationError("Invalid API token")
    if api_valid:
        if not cfg.local_mode:
            raise AuthenticationError("Local identity is disabled in multi-user mode", 403)
        if cookie is not None or (authorization is not None and api_supplied is not None and authorization[7:] != api_supplied):
            raise AuthenticationError("Conflicting credentials")
        if not allow_machine and not is_safe_loopback_request(request):
            raise AuthenticationError("Local access requires a safe loopback client", 403)
        return {**_local_payload(), "machine": allow_machine}
    if authorization is not None or cookie is not None:
        raw = authorization[7:] if authorization is not None and authorization.lower().startswith("bearer ") else cookie if authorization is None else ""
        try:
            payload = TokenVerifier(cfg.secret_keys).verify(raw or "")
        except TokenVerifyError as exc:
            raise AuthenticationError("Invalid token") from exc
        if payload.get("typ") != "access":
            raise AuthenticationError("Access token required")
        return payload
    if not cfg.local_mode:
        raise AuthenticationError("Not authenticated")
    if cfg.api_token:
        raise AuthenticationError("API token required")
    if not is_safe_loopback_request(request):
        raise AuthenticationError("Local access requires a safe loopback client", 403)
    return _local_payload()

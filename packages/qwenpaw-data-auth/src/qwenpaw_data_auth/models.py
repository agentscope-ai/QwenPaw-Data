"""
Pydantic models for control-plane entities.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Tenant(BaseModel):
    id: str
    name: str
    neo4j_database: str
    status: str = "active"
    onboarding_state: str = "ready"
    onboarding_error: Optional[str] = None
    created_at: Optional[datetime] = None


class Workspace(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str = ""
    status: str = "active"
    endpoint_ref: Optional[str] = None
    secret_ref: Optional[str] = None
    instance_status: str = "pending"
    instance_version: Optional[str] = None
    created_at: Optional[datetime] = None


class User(BaseModel):
    id: str
    tenant_id: str
    username: str
    status: str = "active"
    created_at: Optional[datetime] = None
    token_revoked_before: Optional[datetime] = None
    failed_login_count: int = 0
    locked_until: Optional[datetime] = None
    password_changed_at: Optional[datetime] = None
    force_password_change: bool = False


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_\-]+$")
    password: str = Field(min_length=8)


class RoleBinding(BaseModel):
    id: str
    user_id: str
    role: str
    scope_type: str
    scope_id: str
    created_at: Optional[datetime] = None


class RoleBindingCreate(BaseModel):
    user_id: str
    role: str
    scope_type: str
    scope_id: str


class AuthorizationCode(BaseModel):
    code: str
    user_id: str
    redirect_uri: str
    expires_at: datetime
    used: bool = False
    created_at: Optional[datetime] = None


class AuditLogEntry(BaseModel):
    id: str
    timestamp: datetime
    event_type: str
    actor_id: Optional[str] = None
    actor_ip: str = ""
    target_type: str = ""
    target_id: str = ""
    detail: Optional[dict] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class SwitchWorkspaceRequest(BaseModel):
    workspace_id: str


class TemporaryAccessRequest(BaseModel):
    """Service-to-service request for an access token. Auth resolves ``role``; omitted ``ttl`` uses ``access_token_ttl``."""

    user_id: str
    tenant_id: str
    workspace_id: str
    ttl: Optional[int] = None


class ExchangeCodeRequest(BaseModel):
    code: str
    redirect_uri: str


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str = Field(min_length=8)

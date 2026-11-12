"""
QwenPawData unified authentication library.

Provides identity, token, permission, and control-plane primitives
shared by Auth service, Host, and CM.
"""

from qwenpaw_data_auth.principal import Principal, DEFAULT_PRINCIPAL
from qwenpaw_data_auth.token import TokenIssuer, TokenVerifier, TokenVerifyError
from qwenpaw_data_auth.password import (
    hash_password,
    verify_password,
    validate_password_strength,
)
from qwenpaw_data_auth.permissions import check_permission, PERMISSION_MATRIX
from qwenpaw_data_auth.control_plane import ControlPlaneReader
from qwenpaw_data_auth.audit import log_event
from qwenpaw_data_auth.authentication import (
    AuthenticationError,
    authenticate_access_token,
    authenticate_request,
    extract_access_token,
    principal_from_payload,
    resolve_current_authorization,
)
from qwenpaw_data_auth.authorization import (
    AuthorizationContext,
    authorize_request,
    require_admin,
    require_platform_admin,
    require_tenant_admin,
    require_workspace_access,
)
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.local import (
    bootstrap_local,
    is_safe_loopback_request,
    prepare_auth_config,
)

__all__ = [
    "Principal",
    "DEFAULT_PRINCIPAL",
    "TokenIssuer",
    "TokenVerifier",
    "TokenVerifyError",
    "hash_password",
    "verify_password",
    "validate_password_strength",
    "check_permission",
    "PERMISSION_MATRIX",
    "ControlPlaneReader",
    "log_event",
    "AuthenticationError",
    "authenticate_access_token",
    "authenticate_request",
    "extract_access_token",
    "principal_from_payload",
    "resolve_current_authorization",
    "AuthorizationContext",
    "authorize_request",
    "require_admin",
    "require_platform_admin",
    "require_tenant_admin",
    "require_workspace_access",
    "AuthConfig",
    "bootstrap_local",
    "is_safe_loopback_request",
    "prepare_auth_config",
]

"""Project Auth context for aligned packages that still require Principal.

TODO(cm-security-compat): delete this entire adapter after semantic_config and
profiling consume AuthorizationContext instead of qwenpaw_data_auth.Principal.
Keep this module as a pure projection without authorization policy.
"""

from __future__ import annotations

from qwenpaw_data_auth import Principal
from qwenpaw_data_auth.authorization import AuthorizationContext

from ..auth import active_target


def project_aligned_principal(context: AuthorizationContext) -> Principal:
    """Project trusted Auth fields for existing semantic/profiling consumers."""
    tenant_id, workspace_id = active_target(context)
    return Principal(
        user_id=context.user_id,
        tenant_id=tenant_id,
        workspace_ids=sorted(context.workspace_ids),
        active_workspace_id=workspace_id,
        role=context.active_workspace_role,
    )


__all__ = ["project_aligned_principal"]

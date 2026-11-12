"""ACS Agent Sandbox workspace over the native E2B protocol.

The sandbox is claimed from a prewarmed SandboxSet; the user's OSS directory is
mounted at claim time, and ACS's CredentialProvider scopes STS to that subPath.
"""

from __future__ import annotations

import json

from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import get_settings

from .cloud_workspace import CloudE2BWorkspace, user_oss_path


class AcsE2BWorkspace(CloudE2BWorkspace):
    def __init__(self, *, paths: UserAgentPaths, workspace_id: str) -> None:
        acs = get_settings().workspace.acs
        volume = {
            "pvName": acs.oss_pv,
            "mountPath": "/qwenpaw-data",
            "subPath": user_oss_path(paths),
            "attributes": {"credentialProviderName": acs.credential_provider},
        }
        super().__init__(
            paths=paths,
            workspace_id=workspace_id,
            template=acs.template,
            api_key=acs.api_key,
            domain=acs.domain,
            metadata={
                "e2b.agents.kruise.io/csi-volume-config": json.dumps([volume]),
                "security.agents.kruise.io/agent-name": acs.agent_name,
            },
        )

"""FC's E2B-compatible workspace with a per-user RAM role and OSS mount."""

from __future__ import annotations

import json

from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import get_settings

from .cloud_workspace import CloudE2BWorkspace, user_oss_path


def _oss_metadata(paths: UserAgentPaths) -> dict[str, str]:
    settings = get_settings().workspace
    oss = settings.oss
    oss.require()
    return {
        "fc.sandbox.auth.role": settings.fc.role_arn_pattern.replace(
            "{uid}", paths.user_id
        ),
        "fc.sandbox.storage.oss": json.dumps(
            {
                "mountPoints": [
                    {
                        "bucketName": oss.bucket,
                        "bucketPath": "/" + user_oss_path(paths),
                        "mountDir": "/qwenpaw-data",
                        "endpoint": oss.endpoint,
                        "readOnly": False,
                    }
                ]
            }
        ),
    }


class FcE2BWorkspace(CloudE2BWorkspace):
    def __init__(self, *, paths: UserAgentPaths, workspace_id: str) -> None:
        fc = get_settings().workspace.fc
        metadata = _oss_metadata(paths)
        vpc_config = fc.vpc_config()
        if vpc_config is not None:
            metadata["fc.sandbox.network.vpc"] = json.dumps(vpc_config)
        super().__init__(
            paths=paths,
            workspace_id=workspace_id,
            template=fc.template_id,
            api_key=fc.api_key,
            domain=fc.domain,
            metadata=metadata,
        )
        # FC's control plane is not the SDK default ``api.{domain}``.
        self.api_url = fc.api_url

    def _api_opts(self) -> dict[str, str]:
        return {**super()._api_opts(), "api_url": self.api_url}

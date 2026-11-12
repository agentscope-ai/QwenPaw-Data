"""Context Manager integration: HTTP discovery client and MCP helpers."""

from qwenpaw_data.host.core.integrations.cm.client import (
    CMClientError,
    CMLockedError,
    CMDataset,
    CMDatasetList,
    CMDatasource,
    CMDatasourceList,
    ContextManagerClient,
    fetch_datasource_type,
    resolve_cm_base_url,
)
from qwenpaw_data.host.core.integrations.cm.mcp import (
    ensure_workspace_cm_mcp,
    is_cm_mcp_client,
    is_cm_mcp_name,
    project_workspace_mcp_client,
    rewrite_loopback_url_for_docker,
    set_cm_mcp_headers,
)
from qwenpaw_data.host.core.integrations.cm.sql_artifact import (
    SqlArtifactError,
    is_execute_sql_tool,
    materialize_execute_sql_result,
)

__all__ = [
    "CMClientError",
    "CMLockedError",
    "CMDataset",
    "CMDatasetList",
    "CMDatasource",
    "CMDatasourceList",
    "ContextManagerClient",
    "fetch_datasource_type",
    "ensure_workspace_cm_mcp",
    "is_cm_mcp_client",
    "is_cm_mcp_name",
    "project_workspace_mcp_client",
    "resolve_cm_base_url",
    "rewrite_loopback_url_for_docker",
    "set_cm_mcp_headers",
    "SqlArtifactError",
    "is_execute_sql_tool",
    "materialize_execute_sql_result",
]

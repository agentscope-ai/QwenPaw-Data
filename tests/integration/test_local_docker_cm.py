"""Opt-in: local DataBridge + GAAP SQLite fixture + Docker, without an LLM."""

from __future__ import annotations

import csv
import io
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.skipif(
    os.getenv("QWENPAW_DATA_GAAP_CM_E2E") != "1",
    reason="requires a local, authenticated DataBridge with gaap-sqlite-demo and a Docker-shared temp dir",
)
async def test_docker_tools_and_csv_through_host(tmp_path):
    from agentscope.message import ToolCallBlock, ToolResultState
    from agentscope.state import AgentState
    from qwenpaw_data.host.core.agent.middleware.sql_artifact import (
        SqlArtifactMiddleware,
    )
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.integrations.cm.local_mcp import use_host_cm
    from qwenpaw_data.host.core.paths import UserAgentPaths
    from qwenpaw_data.host.core.runtime.builder.data_agent import DataAgentBuilder
    from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
    from qwenpaw_data.host.core.runtime.context import RunContext
    from qwenpaw_data.host.core.workspace.workspace_manager import (
        QwenPawDataWorkspaceManager,
    )

    identity = Identity(
        tenant_id="acceptance", workspace_id="isolated", user_id=uuid.uuid4().hex
    )
    paths = UserAgentPaths.from_identity(
        home=tmp_path, identity=identity, agent_id="default", session_id="smoke"
    )
    manager = QwenPawDataWorkspaceManager()
    try:
        async with manager.lease(paths) as workspace:
            assert use_host_cm(workspace)
            client = await ChatRuntime(None, "smoke")._setup_mcp(
                workspace,
                paths=paths,
                session_id="smoke",
                chat_id="smoke",
                envelope=SimpleNamespace(
                    stream=SimpleNamespace(mcp_connect=AsyncMock())
                ),
                access_token=None,
                mcp_headers={},
                datasource_id="gaap-sqlite-demo",
                dataset_names=["dws_gaap_di"],
            )
            assert client is not None
            assert not await workspace.list_mcps(agent_id="default", session_id="smoke")
            ctx = RunContext(
                "smoke",
                "smoke",
                workspace,
                paths,
                SimpleNamespace(tool_timeout_enabled=True, tool_timeout_seconds=30),
                identity,
                None,
                host_cm=client,
            )
            toolkit = await DataAgentBuilder().build_toolkit(run_context=ctx)

            async def call(name, **arguments):
                event = ToolCallBlock(
                    id="check",
                    name=f"mcp__context-manager__{name}",
                    input=json.dumps(arguments),
                )
                chunks = [item async for item in toolkit.call_tool(event, AgentState())]
                output = await SqlArtifactMiddleware(run_context=ctx)._rewrite(
                    event.name, chunks[-1], None
                )
                assert output.state != ToolResultState.ERROR, output.content[0].text
                return json.loads(output.content[0].text)

            assert "metadata" in (await call("get_graph_schema"))["zones"]
            opts = {"datasource_id": "gaap-sqlite-demo"}
            assert (await call("execute_read", sql="SELECT 1", **opts))["rows"] == [[1]]
            result = await call(
                "execute_read",
                sql="SELECT COUNT(*), MIN(ds), MAX(ds) FROM dws_gaap_di",
                **opts,
            )
            assert "download_url" not in result
            content = await workspace.get_backend().read_file(result["file_path"])
            rows = list(
                csv.reader(
                    io.StringIO(
                        content.decode() if isinstance(content, bytes) else content
                    )
                )
            )
            assert rows[1:] == [["475", "2026-03-01", "2026-03-15"]]
    finally:
        await manager.stop()

"""Prompt-only SOP generation: no agent, workspace, MCP or execution tools."""

from __future__ import annotations

import asyncio

from agentscope.message import SystemMsg, UserMsg
from agentscope.model import ChatModelBase

from qwenpaw_data.host.core.domain.sop import SOP


async def generate_sop(
    model: ChatModelBase, prompt: str, *, datasource_id: str | None
) -> SOP:
    """Generate and validate a portable plan without running any plan steps."""
    response = await asyncio.wait_for(
        model.generate_structured_output(
            messages=[
                SystemMsg(
                    name="system",
                    content=(
                        "Create a reusable analysis SOP, not an execution result. "
                        "Return name, description, expected_outcome and nodes. Each node has "
                        "node_id, name, description, expected_outcome and deps referencing node IDs. "
                        "Use a nonempty acyclic graph, with stable, unique node IDs. "
                        "Do not claim to have queried data or executed tools. "
                        "You have no datasource schema or data beyond what the user supplies. "
                        "Include discovery/validation steps where facts are missing."
                    ),
                ),
                UserMsg(
                    name="user",
                    content=(
                        f"Datasource identifier (not a schema): {datasource_id or 'unspecified'}\n\n{prompt}"
                    ),
                ),
            ],
            structured_model=SOP.model_json_schema(),
        ),
        timeout=60,
    )
    plan = SOP.from_dict(response.content)
    if not plan.nodes:
        raise ValueError("planning returned an empty SOP")
    return plan

"""Skill guides must only reference MCP tools that actually exist.

Regression test for issue #19: ``bi-semantic-layer-guide`` advertised tool
names (``get_dataset_columns``, ``get_metric_info``, ...) that were missing
from or renamed in the CM MCP server, so agents called non-existent tools at
runtime. Every backticked ``name(...)`` reference in the skill guide must be
a registered MCP tool.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_GUIDE = (
    ROOT
    / "packages"
    / "qwenpaw-data-skills"
    / "skills"
    / "runtime"
    / "bi-semantic-layer-guide"
    / "SKILL.md"
)

_TOOL_REFERENCE = re.compile(r"`([a-z][a-z0-9_]*)\(")


def _documented_tools(path: Path) -> set[str]:
    return set(_TOOL_REFERENCE.findall(path.read_text(encoding="utf-8")))


async def test_semantic_guide_references_only_registered_mcp_tools() -> None:
    from context_manager.mcp import cm_server

    documented = _documented_tools(SEMANTIC_GUIDE)
    assert documented, "expected the semantic guide to reference MCP tools"

    registered = {tool.name for tool in await cm_server.mcp.list_tools()}
    missing = sorted(documented - registered)
    assert not missing, (
        f"SKILL.md references MCP tools that are not registered: {missing}; "
        f"registered tools: {sorted(registered)}"
    )

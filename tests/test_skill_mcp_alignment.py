"""Bundled skill guidance must match the real native CM registration.

The inventory comes from ``context_manager.mcp.graph`` / ``.sql`` registering on
a bare FastMCP server; no Context runtime is resolved or started.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "packages/qwenpaw-data-skills/skills"
NATIVE_TOOLS = frozenset({
    "search_graph_fulltext", "search_graph_vector", "get_graph_schema", "query_graph",
    "execute_read", "execute_create", "execute_modify",
})
SQL_TOOLS = ("execute_read", "execute_create", "execute_modify")
LEGACY_TOOLS = frozenset({
    "search_context", "get_domain_overview", "list_domains", "list_metrics",
    "search_metrics", "get_metric", "get_north_star_metrics", "list_dimensions",
    "get_dimension", "list_dimensions_of_metric", "get_dimension_hierarchy",
    "get_dimension_values", "list_datasets", "get_dataset", "get_dataset_columns",
    "execute_sql", "search_event", "explore_entity", "recall_experience",
    "list_metrics_of_dimension",
})
# Together these must document the complete native inventory.
CM_GUIDES = ("domains/cm-skills/SKILL.md", "domains/cm-skills/references/mcp-tools.md")
# Guides outside cm-skills that route work through specific native tools.
ROUTING_GUIDES = (
    "planning/analysis-plan-builder/SKILL.md",
    "workflows/fetch-data/SKILL.md",
    "domains/query-odps/references/value-discovery.md",
)
MARKDOWN = sorted(SKILLS.rglob("*.md"))
# Source docs write tools as `name` or `name(...)`; both spellings count.
_BACKTICKED_NAME = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)(?=[(`])")
# Anything shaped like a CM tool must be native, even if it is not a known legacy name.
_TOOL_SHAPED = re.compile(r"(?:search_graph|get_graph|execute)_\w+|query_graph\w*")
_PRIVATE = re.compile(
    "|".join((
        "odps_execution_project", r"\bODPS_(?:BUC|INTERNAL)\b", r"\bAuthX\b",
        r"\bep" + r"aas\b", r"\bBU" + r"C_[A-Z]",
    )),
    re.I,
)
_DOCUMENTED_SQL_LIMIT = re.compile(r"`(max_rows|timeout_ms)=(\d+)`（(\d+)–(\d+)）")


def _tool_mentions(text: str) -> set[str]:
    names = set(_BACKTICKED_NAME.findall(text))
    return {
        name for name in names
        if name in NATIVE_TOOLS or name in LEGACY_TOOLS or _TOOL_SHAPED.fullmatch(name)
    }


def _read(relative: str) -> str:
    path = SKILLS / relative
    assert path.is_file(), f"required bundled guide missing: {path}"
    return path.read_text(encoding="utf-8")


def _unused_resolver(*_args):
    raise AssertionError("tool listing must not resolve or start a Context runtime")


async def _registered_tools() -> dict:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("skill-alignment")
    for module_name, registration in (
        ("context_manager.mcp.graph", "register_graph_tools"),
        ("context_manager.mcp.sql", "register_sql_tools"),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            pytest.fail(f"native CM registration cannot be imported: {module_name}: {exc}")
        register = getattr(module, registration, None)
        assert callable(register), f"native CM registration missing: {module_name}.{registration}"
        register(server, _unused_resolver)
    return {tool.name: tool for tool in await server.list_tools()}


def test_tool_reference_pattern_matches_source_spellings() -> None:
    sample = (
        "先 `search_graph_fulltext`，再 `query_graph(cypher=...)`；"
        "取数用 `execute_read`，SQL 里的 `count(*)` 与 `datasource_id` 不是工具。"
    )
    assert _tool_mentions(sample) == {"search_graph_fulltext", "query_graph", "execute_read"}
    assert _tool_mentions("旧接口 `list_metrics()` 与 `execute_sql`") == {"list_metrics", "execute_sql"}
    assert _tool_mentions("```python\nexecute_read\n```") == set()


@pytest.mark.asyncio
async def test_native_inventory_matches_actual_registration() -> None:
    registered = await _registered_tools()
    assert set(registered) == NATIVE_TOOLS, f"native inventory drift: {sorted(registered)}"
    for guide in CM_GUIDES:
        documented = _tool_mentions(_read(guide))
        assert documented == NATIVE_TOOLS, f"{guide} inventory drift: {sorted(documented)}"


@pytest.mark.asyncio
async def test_documented_sql_parameters_match_registration() -> None:
    registered = await _registered_tools()
    text = _read("domains/cm-skills/references/mcp-tools.md")
    documented = {
        name: (int(default), int(low), int(high))
        for name, default, low, high in _DOCUMENTED_SQL_LIMIT.findall(text)
    }
    assert set(documented) == {"max_rows", "timeout_ms"}, documented
    for tool_name in SQL_TOOLS:
        schema = registered[tool_name].inputSchema
        assert {"datasource_id", "sql"} <= set(schema.get("required", ())), tool_name
        for param, expected in documented.items():
            prop = schema["properties"][param]
            actual = (prop.get("default"), prop.get("minimum"), prop.get("maximum"))
            assert actual == expected, (tool_name, param, actual)


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.relative_to(SKILLS).as_posix())
def test_guides_reference_only_native_tools_and_no_private_modes(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    mentions = _tool_mentions(text)
    assert mentions <= NATIVE_TOOLS, f"non-native tool guidance: {sorted(mentions - NATIVE_TOOLS)}"
    legacy = {name for name in LEGACY_TOOLS if re.search(rf"\b{name}\b", text)}
    assert not legacy, f"legacy App tool names: {sorted(legacy)}"
    assert not _PRIVATE.search(text), "private execution/auth mode mentioned"


@pytest.mark.parametrize("guide", ROUTING_GUIDES)
def test_routing_guides_name_native_tools(guide: str) -> None:
    assert _tool_mentions(_read(guide)), f"{guide} no longer names a native CM tool"


def test_cm_guides_cover_identity_semantics_and_sql_artifacts() -> None:
    text = _read("domains/cm-skills/SKILL.md")
    reference = _read("domains/cm-skills/references/mcp-tools.md")
    for marker in ("MetricCaliber", "DatasetField", "datasource_id", "个人图", "管理员", "确认"):
        assert marker in text, marker
    for marker in (
        "download_url", "file_path", "truncated=true", "CSV", "max_rows", "SELECT", "AccessKey",
    ):
        assert marker in reference, marker

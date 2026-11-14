from __future__ import annotations

import json
import inspect
from pathlib import Path
import shutil
import uuid

import pytest

from agentscope.workspace import LocalWorkspace

from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import SkillStoreRepo
from qwenpaw_data.host.core.utils.skill import discover_builtin_skills
from qwenpaw_data.host.core.workspace import prepare_workspace_dirs, stable_workspace_id


def _paths(tmp_path: Path, *, agent_id: str = "agent-a", session_id: str = "session-a"):
    return UserAgentPaths(
        home=tmp_path,
        tenant_id="tenant-a",
        workspace_id="ws-a",
        user_id="user-a",
        agent_id=agent_id,
        session_id=session_id,
    )


def _local_workspace(paths: UserAgentPaths) -> LocalWorkspace:
    prepare_workspace_dirs(paths)
    return LocalWorkspace(
        workdir=str(paths.workspace),
        workspace_id=stable_workspace_id(paths),
    )


def test_discover_builtin_skills_from_package() -> None:
    skills = discover_builtin_skills()
    skill_root = Path(__file__).resolve().parents[2] / "qwenpaw-data-skills" / "skills"
    relative_paths = {
        item.src_dir.relative_to(skill_root).as_posix() for item in skills
    }

    assert len(skills) == 32
    assert relative_paths >= {
        "meta/skill-hub-guide",
        "workflows/fetch-data",
        "atomic/bi-report-generation",
        "domains/query-odps",
        "domains/cm-skills",
    }
    assert not any(path.startswith("runtime/") for path in relative_paths)
    assert all((item.src_dir / "SKILL.md").is_file() for item in skills)


def test_user_agent_paths_use_shared_agent_workspace(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)

    assert paths.user_root == tmp_path / "tenant-a" / "ws-a" / "user-a"
    assert paths.agent_root == tmp_path / "tenant-a" / "ws-a" / "user-a" / "agent-a"
    assert paths.workspace == paths.agent_root / "workspace"
    assert paths.skills_root == paths.workspace / "skills"
    assert paths.skills == paths.skills_root / "agent-a"
    assert paths.artifacts_root == paths.workspace / "artifacts"
    assert paths.artifact_dir == paths.artifacts_root / "session-a"


def test_session_create_rejects_unsafe_agent_id() -> None:
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.domain.session import Session

    identity = Identity(user_id="u1", tenant_id="t1", workspace_id="w1")
    with pytest.raises(ValueError):
        Session.create(identity=identity, agent_id="../escape")


@pytest.mark.asyncio
async def test_local_workspace_seeds_qwenpaw_data_skills(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = _local_workspace(paths)

    try:
        await workspace.initialize()
        skills = await workspace.list_skills(agent_id=paths.agent_id)
    finally:
        close = getattr(workspace, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    skill_names = {skill.name for skill in skills}
    assert {
        "skill-hub-guide",
        "fetch-data",
        "cm-skills",
    } <= skill_names
    assert (paths.skills / "skill-hub-guide" / "SKILL.md").is_file()
    assert (paths.skills / "fetch-data" / "SKILL.md").is_file()
    for reference in (
        "mcp-tools.md",
        "neo4j.md",
        "holoage.md",
    ):
        assert (paths.skills / "cm-skills" / "references" / reference).is_file()


@pytest.mark.asyncio
async def test_local_workspace_bash_uses_workspace_cwd(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = _local_workspace(paths)
    marker = f"artifacts/cwd-check-{uuid.uuid4().hex}.txt"
    workspace_marker = paths.workspace / marker
    process_cwd_marker = Path.cwd() / marker

    try:
        await workspace.initialize()
        tools = await workspace.list_tools()
        bash = next(tool for tool in tools if tool.name == "Bash")

        stream = await bash(
            command=f"mkdir -p artifacts && printf workspace > {marker}",
            description="write cwd marker",
        )
        chunks = [chunk async for chunk in stream]

        assert chunks[-1].state == "running"
        assert workspace_marker.read_text(encoding="utf-8") == "workspace"
        assert not process_cwd_marker.exists()
    finally:
        await workspace.close()
        if process_cwd_marker.exists():
            process_cwd_marker.unlink()


@pytest.mark.asyncio
async def test_local_workspace_search_tools_with_explicit_path(tmp_path: Path) -> None:
    """Local Glob/Grep default to process cwd; callers must pass workspace path."""
    paths = _paths(tmp_path)
    workspace = _local_workspace(paths)
    token = f"workspace-search-{uuid.uuid4().hex}"
    artifact = paths.workspace / "artifacts" / token / "result.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(f"{token}\n", encoding="utf-8")
    workdir = str(paths.workspace)

    await workspace.initialize()
    try:
        tools = await workspace.list_tools()
        glob = next(tool for tool in tools if tool.name == "Glob")
        grep = next(tool for tool in tools if tool.name == "Grep")

        glob_chunk = await glob(pattern="**/*.txt", path=workdir)
        glob_text = "\n".join(block.text for block in glob_chunk.content)
        assert token in glob_text
        assert "result.txt" in glob_text

        if shutil.which("rg") is None:
            pytest.skip("ripgrep is required for Grep")
        grep_chunk = await grep(pattern=token, path=workdir)
        grep_text = "\n".join(block.text for block in grep_chunk.content)
        assert token in grep_text
    finally:
        await workspace.close()


@pytest.mark.asyncio
async def test_local_workspace_loads_existing_mcp(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.workspace.mkdir(parents=True)
    (paths.workspace / ".mcp").write_text(
        json.dumps(
            [
                {
                    "name": "context-manager",
                    "is_stateful": False,
                    "mcp_config": {
                        "type": "http_mcp",
                        "url": "http://127.0.0.1:8000/mcp",
                        "headers": {},
                        "timeout": 2400.0,
                    },
                    "enable_tools": None,
                    "disable_tools": None,
                    "execution_timeout": None,
                },
            ],
        ),
        encoding="utf-8",
    )
    workspace = _local_workspace(paths)

    try:
        await workspace.initialize()
        mcps = await workspace.list_mcps()
    finally:
        close = getattr(workspace, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    assert [mcp.name for mcp in mcps] == ["context-manager"]


@pytest.mark.asyncio
async def test_user_agent_workspace_resources_are_shared_and_isolated(
    tmp_path: Path,
) -> None:
    first = UserAgentPaths(
        tmp_path, "tenant-a", "ws-a", "user-a", "agent-a", "session-a"
    )
    second = UserAgentPaths(
        tmp_path, "tenant-a", "ws-a", "user-a", "agent-a", "session-b"
    )
    other_agent = UserAgentPaths(
        tmp_path, "tenant-a", "ws-a", "user-a", "agent-b", "session-a"
    )
    other_user = UserAgentPaths(
        tmp_path, "tenant-a", "ws-a", "user-b", "agent-a", "session-a"
    )

    first.workspace.mkdir(parents=True)
    (first.workspace / ".mcp").write_text(
        json.dumps(
            [
                {
                    "name": "shared-mcp",
                    "is_stateful": False,
                    "mcp_config": {
                        "type": "http_mcp",
                        "url": "http://127.0.0.1:8000/mcp",
                        "headers": {},
                        "timeout": 30.0,
                    },
                    "enable_tools": None,
                    "disable_tools": None,
                    "execution_timeout": None,
                },
            ],
        ),
        encoding="utf-8",
    )
    custom_src = tmp_path / "custom-shared-skill"
    custom_src.mkdir()
    (custom_src / "SKILL.md").write_text(
        "---\n"
        "name: shared-skill\n"
        "description: Shared agent skill.\n"
        "---\n"
        "# Shared skill\n",
        encoding="utf-8",
    )
    store = SkillStoreRepo(first.skills)
    store.ensure_ready()
    store.import_skill_dir(custom_src)

    workspaces = [
        _local_workspace(paths) for paths in (first, second, other_agent, other_user)
    ]
    try:
        for workspace in workspaces:
            await workspace.initialize()

        assert first.workspace == second.workspace
        assert first.artifact_dir != second.artifact_dir
        assert first.workspace != other_agent.workspace
        assert first.workspace != other_user.workspace
        assert [mcp.name for mcp in await workspaces[0].list_mcps()] == ["shared-mcp"]
        assert [mcp.name for mcp in await workspaces[1].list_mcps()] == ["shared-mcp"]
        assert await workspaces[2].list_mcps() == []
        assert await workspaces[3].list_mcps() == []

        first_skills = {
            skill.name for skill in await workspaces[0].list_skills(agent_id="agent-a")
        }
        second_skills = {
            skill.name for skill in await workspaces[1].list_skills(agent_id="agent-a")
        }
        other_agent_skills = {
            skill.name for skill in await workspaces[2].list_skills(agent_id="agent-b")
        }
        other_user_skills = {
            skill.name for skill in await workspaces[3].list_skills(agent_id="agent-a")
        }
        assert "shared-skill" in first_skills
        assert first_skills == second_skills
        assert "shared-skill" not in other_agent_skills
        assert "shared-skill" not in other_user_skills
        assert "fetch-data" in other_agent_skills
        assert "fetch-data" in other_user_skills
    finally:
        for workspace in workspaces:
            await workspace.close()

from __future__ import annotations

import json
import inspect
import os
from pathlib import Path
import shutil
import uuid

import pytest


@pytest.fixture()
def workspace_modules() -> object:
    from qwenpaw_data.host.core.paths import Paths
    from qwenpaw_data.host.core.utils import workspace as workspace_module

    yield Paths, workspace_module


def test_default_skill_paths_discovers_hierarchical_skills(
    workspace_modules: object,
) -> None:
    _, workspace_module = workspace_modules

    skill_paths = [Path(path) for path in workspace_module._default_skill_paths()]
    skill_root = Path(__file__).resolve().parents[2] / "qwenpaw-data-skills" / "skills"
    relative_paths = {path.relative_to(skill_root).as_posix() for path in skill_paths}

    assert len(skill_paths) == 31
    assert relative_paths >= {
        "meta/skill-hub-guide",
        "workflows/fetch-data",
        "atomic/bi-report-generation",
        "runtime/interaction-strategy",
        "domains/query-odps",
    }
    assert all((path / "SKILL.md").is_file() for path in skill_paths)


def test_default_skill_paths_falls_back_to_installed_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace_modules: object,
) -> None:
    _, workspace_module = workspace_modules
    flat_skill = tmp_path / "qwenpaw_data_skills" / "skills" / "flat-skill"
    installed_skill = (
        tmp_path / "qwenpaw_data_skills" / "skills" / "workflows" / "installed-skill"
    )
    flat_skill.mkdir(parents=True)
    installed_skill.mkdir(parents=True)
    (flat_skill / "SKILL.md").write_text("# Flat Skill\n", encoding="utf-8")
    (installed_skill / "SKILL.md").write_text(
        "# Installed Skill\n",
        encoding="utf-8",
    )

    class DistributionStub:
        def locate_file(self, path: str) -> Path:
            return tmp_path / path

    monkeypatch.setattr(workspace_module, "_source_skill_paths", lambda: [])
    monkeypatch.setattr(
        workspace_module, "distribution", lambda name: DistributionStub()
    )

    assert workspace_module._default_skill_paths() == [
        str(flat_skill),
        str(installed_skill),
    ]


def test_paths_use_host_workspace_and_scoped_state(
    tmp_path: Path,
    workspace_modules: object,
) -> None:
    Paths, _ = workspace_modules
    paths = Paths(tmp_path, session_id="session-a")

    assert paths.host_root == tmp_path / "host"
    assert paths.secrets_root == tmp_path / "host" / ".secrets"
    assert paths.workspace == tmp_path / "host" / "workspace"
    assert paths.mcp_config_file == paths.workspace / ".mcp"
    assert paths.session_root == paths.workspace / "sessions" / "session-a"
    assert paths.console_root == paths.workspace / "sessions" / "console"
    assert paths.dag_root == paths.workspace / "sessions" / "dag"
    assert paths.data_root == paths.workspace / "data"
    assert paths.artifacts_root == paths.workspace / "artifacts"
    assert paths.artifact_dir == paths.artifacts_root / "session-a"
    assert paths.node_artifact_dir("graph-a", "node-a") == (
        paths.artifact_dir / "graph-a" / "node-a"
    )


@pytest.mark.asyncio
async def test_local_workspace_seeds_qwenpaw_data_skills(
    tmp_path: Path,
    workspace_modules: object,
) -> None:
    Paths, workspace_module = workspace_modules
    workspace = workspace_module.create_local_workspace(Paths(tmp_path))

    try:
        await workspace.initialize()
        skills = await workspace.list_skills()
    finally:
        close = getattr(workspace, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    skill_names = {skill.name for skill in skills}
    assert {"skill-hub-guide", "fetch-data"} <= skill_names


@pytest.mark.asyncio
async def test_local_workspace_persists_native_state_under_host_workspace(
    tmp_path: Path,
    workspace_modules: object,
) -> None:
    from qwenpaw_data.host.core.utils.msg import user_msg

    Paths, workspace_module = workspace_modules
    paths = Paths(tmp_path, session_id="session-a")
    workspace = workspace_module.create_local_workspace(paths)

    try:
        await workspace.initialize()
        context_path = await workspace.offload_context(
            paths.session_id,
            [user_msg("hello")],
        )
    finally:
        close = getattr(workspace, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    assert paths.mcp_config_file.is_file()
    assert paths.skills.is_dir()
    assert Path(context_path) == paths.session_root / "context.jsonl"
    assert set(path.name for path in tmp_path.iterdir()) == {"host"}


@pytest.mark.asyncio
async def test_local_workspace_bash_uses_workspace_cwd(
    tmp_path: Path,
    workspace_modules: object,
) -> None:
    Paths, workspace_module = workspace_modules
    paths = Paths(tmp_path)
    workspace = workspace_module.create_local_workspace(paths)
    marker = f"artifacts/cwd-check-{uuid.uuid4().hex}.txt"
    workspace_marker = paths.workspace / marker
    process_cwd_marker = Path.cwd() / marker

    try:
        tools = await workspace.list_tools()
        shell_name = "PowerShell" if os.name == "nt" else "Bash"
        shell = next(tool for tool in tools if tool.name == shell_name)
        if os.name == "nt":
            command = (
                "New-Item -ItemType Directory -Force artifacts | Out-Null; "
                f"Set-Content -NoNewline -Path '{marker}' -Value workspace"
            )
        else:
            command = f"mkdir -p artifacts && printf workspace > {marker}"

        stream = shell(command=command)
        if inspect.iscoroutine(stream):
            stream = await stream
        chunks = [chunk async for chunk in stream]

        assert chunks[-1].state == "running"
        assert workspace_marker.read_text(encoding="utf-8") == "workspace"
        assert not process_cwd_marker.exists()
    finally:
        if process_cwd_marker.exists():
            process_cwd_marker.unlink()


@pytest.mark.asyncio
async def test_local_workspace_search_tools_use_workspace_default_path(
    tmp_path: Path,
    workspace_modules: object,
) -> None:
    Paths, workspace_module = workspace_modules
    paths = Paths(tmp_path)
    workspace = workspace_module.create_local_workspace(paths)
    token = f"workspace-search-{uuid.uuid4().hex}"
    artifact = paths.workspace / "artifacts" / token / "result.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(f"{token}\n", encoding="utf-8")

    tools = await workspace.list_tools()
    glob = next(tool for tool in tools if tool.name == "Glob")
    grep = next(tool for tool in tools if tool.name == "Grep")

    glob_chunk = await glob(pattern="artifacts/**/*.txt")
    glob_text = "\n".join(block.text for block in glob_chunk.content)
    assert str(artifact) in glob_text

    if shutil.which("rg") is None:
        pytest.skip("ripgrep is required for Grep")
    grep_chunk = await grep(pattern=token)
    grep_text = "\n".join(block.text for block in grep_chunk.content)
    assert str(artifact) in grep_text


@pytest.mark.asyncio
async def test_local_workspace_loads_existing_mcp(
    tmp_path: Path,
    workspace_modules: object,
) -> None:
    Paths, workspace_module = workspace_modules
    paths = Paths(tmp_path)
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
    workspace = workspace_module.create_local_workspace(paths)

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

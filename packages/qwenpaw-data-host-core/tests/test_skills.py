# -*- coding: utf-8 -*-
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.skill import (
    BuiltinSkill,
    SkillConflictError,
    SkillNotFoundError,
)
from qwenpaw_data.host.core.domain.skill_manager import SkillManager
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import SkillStateRepository, SkillStoreRepo
from qwenpaw_data.host.core.repositories import skill_store as skill_store_module
from qwenpaw_data.host.core.utils.skill import (
    discover_builtin_skills,
    read_skill_frontmatter,
)
from qwenpaw_data.host.core.workspace import prepare_workspace_dirs
from qwenpaw_data.host.core.workspace.workspace_manager import stable_workspace_id
from agentscope.workspace import LocalWorkspace

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


def _paths(tmp_path: Path, *, agent_id: str = "agent-a") -> UserAgentPaths:
    return UserAgentPaths(
        home=tmp_path,
        tenant_id="tenant-a",
        workspace_id="ws-a",
        user_id="user-a",
        agent_id=agent_id,
        session_id="session-a",
    )


def _write_skill(dir_path: Path, name: str, description: str = "demo") -> None:
    dir_path.mkdir(parents=True)
    (dir_path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
    (dir_path / "scripts").mkdir()
    (dir_path / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")


def test_discover_builtin_skills_are_flat_by_frontmatter_name() -> None:
    skills = discover_builtin_skills()
    names = {item.name for item in skills}
    assert "skill-hub-guide" in names
    assert "fetch-data" in names
    assert "bi-clustering" in names
    assert all(item.src_dir.name for item in skills)
    by_name = {item.name: item for item in skills}
    assert by_name["bi-clustering"].group == "atomic"
    assert by_name["fetch-data"].group == "workflows"


def test_new_agent_copies_only_builtins_not_custom(tmp_path: Path) -> None:
    paths_a = _paths(tmp_path, agent_id="agent-a")
    store_a = SkillStoreRepo(paths_a.skills)
    store_a.ensure_ready()
    custom_src = tmp_path / "custom-src"
    _write_skill(custom_src, "my-custom")
    store_a.import_skill_dir(custom_src)

    paths_b = _paths(tmp_path, agent_id="agent-b")
    store_b = SkillStoreRepo(paths_b.skills)
    added = store_b.ensure_ready()
    assert "my-custom" not in added
    agent_names = {item.name for item in store_b.list_skills()}
    assert "fetch-data" in agent_names
    assert "my-custom" not in agent_names
    assert (paths_b.skills / "fetch-data" / "SKILL.md").is_file()
    assert not (paths_b.skills / "my-custom").exists()
    # The custom skill stays only in the agent that installed it.
    assert (paths_a.skills / "my-custom" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_manual_install_custom_and_disable(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        paths = _paths(tmp_path)
        store = SkillStoreRepo(paths.skills)
        store.ensure_ready()
        custom_src = tmp_path / "custom-src"
        _write_skill(custom_src, "my-custom")
        store.import_skill_dir(custom_src)

        names = {item.name for item in store.list_skills()}
        assert "my-custom" in names

        async with get_session_factory()() as db:
            states = SkillStateRepository(db, TEST_IDENTITY, agent_id=paths.agent_id)
            await states.set_disabled("my-custom", True)
            disabled = await states.disabled_names()
            await db.commit()
            assert disabled == frozenset({"my-custom"})
            # Disabling keeps the files; it only drops the skill from the
            # agent's effective set.
            manager = SkillManager(db, identity=TEST_IDENTITY, paths=paths)
            by_name = {item.name: item for item in await manager.list_skills()}
            assert by_name["my-custom"].enabled is False
            assert by_name["fetch-data"].enabled is True
        assert (paths.skills / "my-custom" / "SKILL.md").is_file()
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_manager_rejects_builtin_write(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        paths = _paths(tmp_path)
        async with get_session_factory()() as db:
            manager = SkillManager(db, identity=TEST_IDENTITY, paths=paths)
            manager.ensure_ready()
            with pytest.raises(PermissionError, match="cannot modify builtin"):
                await manager.save(
                    "fetch-data",
                    "---\nname: fetch-data\ndescription: x\n---\n# x\n",
                )
            with pytest.raises(PermissionError, match="cannot uninstall builtin"):
                await manager.uninstall("fetch-data")
    finally:
        await reset_engine()


def test_ensure_ready_overwrites_stale_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    store = SkillStoreRepo(paths.skills)
    store.ensure_ready()
    target = paths.skills / "fetch-data" / "SKILL.md"
    packaged_text = target.read_text(encoding="utf-8")
    target.write_text(
        "---\nname: fetch-data\ndescription: locally edited\n---\n# edited\n",
        encoding="utf-8",
    )
    ghost = tmp_path / "ghost-skill"
    _write_skill(ghost, "ghost-builtin")
    packaged = discover_builtin_skills()
    monkeypatch.setattr(
        skill_store_module,
        "discover_builtin_skills",
        lambda: (
            *packaged,
            BuiltinSkill(
                name="ghost-builtin",
                src_dir=ghost,
                group="atomic",
                description="ghost",
            ),
        ),
    )
    synced = store.ensure_ready()
    assert "ghost-builtin" in synced
    assert "fetch-data" in synced
    assert target.read_text(encoding="utf-8") == packaged_text


def test_ensure_ready_keeps_custom_and_drops_marked_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    store = SkillStoreRepo(paths.skills)
    store.ensure_ready()
    custom_src = tmp_path / "custom-src"
    _write_skill(custom_src, "my-custom", description="user copy")
    store.import_skill_dir(custom_src)

    retired = paths.skills / "old-packaged"
    _write_skill(retired, "old-packaged")
    (retired / ".qwenpaw-data-builtin").write_text("", encoding="utf-8")
    unmarked = paths.skills / "user-upload"
    _write_skill(unmarked, "user-upload")

    packaged = discover_builtin_skills()
    pack_src = tmp_path / "packaged-my-custom"
    _write_skill(pack_src, "my-custom", description="from package")
    monkeypatch.setattr(
        skill_store_module,
        "discover_builtin_skills",
        lambda: (
            *packaged,
            BuiltinSkill(
                name="my-custom",
                src_dir=pack_src,
                group="atomic",
                description="from package",
            ),
        ),
    )
    synced = store.ensure_ready()
    assert "my-custom" not in synced
    dest = paths.skills / "my-custom"
    assert "user copy" in (dest / "SKILL.md").read_text(encoding="utf-8")
    assert not (dest / ".qwenpaw-data-builtin").is_file()
    assert store.info("my-custom").source == "custom"
    assert not retired.exists()
    assert (unmarked / "SKILL.md").is_file()


def test_reserved_builtin_name_cannot_be_installed(tmp_path: Path) -> None:
    store = SkillStoreRepo(_paths(tmp_path).skills)
    with pytest.raises(SkillConflictError, match="reserved by builtin"):
        store.create_skill(
            "fetch-data",
            "---\nname: fetch-data\ndescription: d\n---\n# fetch-data\n",
        )
    src = tmp_path / "fetch-data-src"
    _write_skill(src, "fetch-data")
    with pytest.raises(SkillConflictError, match="reserved by builtin"):
        store.import_skill_dir(src)


@pytest.mark.asyncio
async def test_zip_import(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        paths = _paths(tmp_path)
        skill_root = tmp_path / "zip-root" / "zip-skill"
        _write_skill(skill_root, "zip-skill")
        zip_path = tmp_path / "skills.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for path in skill_root.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(skill_root.parent).as_posix())
        async with get_session_factory()() as db:
            imported = await SkillManager(
                db, identity=TEST_IDENTITY, paths=paths
            ).import_zip(zip_path)
            await db.commit()
        assert imported == ["zip-skill"]
        assert (paths.skills / "zip-skill" / "scripts" / "run.py").is_file()
    finally:
        await reset_engine()


def test_read_skill_document_returns_description_and_body(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    skill_dir = paths.skills / "doc-skill"
    _write_skill(skill_dir, "doc-skill", description="one-line")
    store = SkillStoreRepo(paths.skills)
    info = store.info("doc-skill")
    files = {item.relative_path: item.content for item in store.read_files("doc-skill")}
    assert info.description == "one-line"
    assert "---\nname: doc-skill\n" in files["SKILL.md"]
    assert "# doc-skill\n" in files["SKILL.md"]


def test_shipped_builtin_skills_are_readable(tmp_path: Path) -> None:
    """Every packaged skill must fit the read cap, bundled assets included.

    bi-report-generation ships resources/echarts.min.js (~1 MB); a cap below
    its total aborted skill evolution for every chat.
    """
    store = SkillStoreRepo(tmp_path / "skills")
    store.ensure_ready()
    for info in store.list_skills():
        assert store.read_files(info.name)


def test_read_skill_document_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SKILL.md is required"):
        read_skill_frontmatter(tmp_path / "missing" / "SKILL.md")


def test_require_and_document(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SkillStoreRepo(paths.skills)
    store.ensure_ready()
    custom_src = tmp_path / "custom-src"
    _write_skill(custom_src, "my-custom", description="from-agent")
    store.import_skill_dir(custom_src)

    skill_dir = store.require("my-custom")
    info = store.info("my-custom")
    files = {item.relative_path: item.content for item in store.read_files("my-custom")}
    assert info.source == "custom"
    assert info.description == "from-agent"
    assert "# my-custom\n" in files["SKILL.md"]

    with pytest.raises(SkillNotFoundError):
        store.require("no-such-skill")

    (skill_dir / "SKILL.md").unlink()
    with pytest.raises(ValueError, match="SKILL.md is required"):
        store.require("my-custom")


def test_install_conflict(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = SkillStoreRepo(paths.skills)
    store.ensure_ready()
    first = tmp_path / "custom-a"
    _write_skill(first, "my-custom")
    store.import_skill_dir(first)
    again = tmp_path / "custom-b"
    _write_skill(again, "my-custom")
    with pytest.raises(SkillConflictError, match="already exists"):
        store.import_skill_dir(again)


@pytest.mark.asyncio
async def test_workspace_loads_effective_agent_skills(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    prepare_workspace_dirs(paths)
    workspace = LocalWorkspace(
        workdir=str(paths.workspace),
        workspace_id=stable_workspace_id(paths),
    )
    try:
        await workspace.initialize()
        names = {
            skill.name for skill in await workspace.list_skills(agent_id=paths.agent_id)
        }
    finally:
        await workspace.close()
    assert "skill-hub-guide" in names
    assert "fetch-data" in names
    assert (paths.skills / "fetch-data" / "SKILL.md").is_file()

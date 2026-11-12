# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw_data.host.core.agent.middleware import prompt as prompt_module
from qwenpaw_data.host.core.agent.middleware.prompt import (
    QwenPawDataPromptMiddleware,
    DatasourcePromptMiddleware,
    GlobalMemoryPromptMiddleware,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.settings import clear_settings


def _run_context(
    tmp_path: Path,
    *,
    session_id: str = "session-a",
    workdir: str | None = None,
) -> RunContext:
    paths = UserAgentPaths(
        home=tmp_path,
        tenant_id="tenant-a",
        workspace_id="ws-a",
        user_id="user-a",
        agent_id="agent-a",
        session_id=session_id,
    )
    return RunContext(
        session_id=session_id,
        chat_id="chat-a",
        workspace=SimpleNamespace(
            workdir=str(paths.workspace) if workdir is None else workdir
        ),
        paths=paths,
        user_runtime_config=SimpleNamespace(),  # type: ignore[arg-type]
        identity=Identity(user_id="user-a", tenant_id="tenant-a", workspace_id="ws-a"),
        session_factory=SimpleNamespace(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_qwenpaw_data_prompt_middleware_preserves_agent_skills(
    tmp_path: Path,
) -> None:
    middleware = QwenPawDataPromptMiddleware(run_context=_run_context(tmp_path))
    current = (
        "<agent-skills>\n"
        "<skill><name>data-intent-router</name></skill>\n"
        "</agent-skills>"
    )

    result = await middleware.on_system_prompt(
        agent=SimpleNamespace(), current_prompt=current
    )

    assert "QwenPawData" in result
    assert "data-intent-router" in result
    assert "<agent-skills>" in result
    assert "<qwenpaw-data-analysis-environment>" in result
    assert "未获用户明确指示，禁止读取或改写其他 Session 的文件" in result
    assert "local workspace" in result
    assert "file_path" in result
    assert "download_url" not in result
    assert "curl" not in result.lower()
    assert "<skill-dir>/scripts/" in result
    assert "plan.yaml" in result
    assert "data/raw/" in result
    assert "result.yaml" in result
    assert "<交付物名>_v1.<ext>" in result
    assert "人机交互" in result
    assert "执行期规范" not in result
    assert "2f 报告复现" in result
    assert "report-reproduction" in result
    assert "runtime-guide" not in result
    assert "interaction-strategy" not in result
    assert "bi-semantic-layer-guide" not in result


@pytest.mark.asyncio
async def test_datasource_prompt_injects_type(tmp_path: Path) -> None:
    context = _run_context(tmp_path)
    context.request_context["datasource_type"] = "postgresql"
    result = await DatasourcePromptMiddleware(run_context=context).on_system_prompt(
        agent=SimpleNamespace(),
        current_prompt="host",
    )
    assert result == "host\n\n当前数据源类型：`postgresql`。"


@pytest.mark.asyncio
async def test_datasource_prompt_injects_id_and_type(tmp_path: Path) -> None:
    context = _run_context(tmp_path)
    context.request_context["datasource_id"] = " ds-pg-1 "
    context.request_context["datasource_type"] = "postgresql"
    result = await DatasourcePromptMiddleware(run_context=context).on_system_prompt(
        agent=SimpleNamespace(),
        current_prompt="host",
    )
    assert result.startswith("host\n\n## 当前数据源\n")
    assert "- datasource_id：`ds-pg-1`" in result
    assert "- 数据源类型：`postgresql`" in result
    assert "`execute_read`" in result


@pytest.mark.asyncio
async def test_datasource_prompt_injects_id_without_type(tmp_path: Path) -> None:
    context = _run_context(tmp_path)
    context.request_context["datasource_id"] = "ds-pg-1"
    context.request_context["datasource_type"] = None
    result = await DatasourcePromptMiddleware(run_context=context).on_system_prompt(
        agent=SimpleNamespace(),
        current_prompt="",
    )
    assert result.startswith("## 当前数据源\n- datasource_id：`ds-pg-1`\n")
    assert "数据源类型" not in result
    assert "`execute_read`" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("datasource_type", [None, "", "  "])
async def test_datasource_prompt_skips_missing_type(
    tmp_path: Path,
    datasource_type: str | None,
) -> None:
    context = _run_context(tmp_path)
    if datasource_type is not None:
        context.request_context["datasource_type"] = datasource_type
    result = await DatasourcePromptMiddleware(run_context=context).on_system_prompt(
        agent=SimpleNamespace(),
        current_prompt="host",
    )
    assert result == "host"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,workdir",
    [
        ("docker", "/workspace"),
        ("fc-sandbox", "/custom-fc/workspace"),
        ("acs-sandbox", "/custom-acs/workspace"),
    ],
)
async def test_qwenpaw_data_prompt_uses_execution_workspace_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    workdir: str,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", backend)
    monkeypatch.setenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", "https://gateway.example.test")
    clear_settings()
    try:
        middleware = QwenPawDataPromptMiddleware(
            run_context=_run_context(tmp_path, workdir=workdir)
        )
        result = await middleware.on_system_prompt(
            agent=SimpleNamespace(),
            current_prompt="",
        )
        assert f"{workdir}/artifacts/session-a" in result
        assert str(tmp_path) not in result
    finally:
        monkeypatch.delenv("QWENPAW_DATA_WORKSPACE_BACKEND", raising=False)
        clear_settings()


def _global_memories() -> dict:
    return {
        "profile": {"display_name": "张三", "timezone": "Asia/Shanghai"},
        "type_descriptions": {"principle": "长期硬性规则"},
        "principles": [
            {"id": "umem:1", "type": "principle", "description": "说明数据来源。"},
        ],
        "preferences": [
            {
                "id": "umem:2",
                "type": "preference",
                "description": "优先使用中文。\n结果保留两位小数。",
            },
            {"id": "umem:3", "type": "preference", "description": "先给结论。"},
        ],
    }


@pytest.mark.asyncio
async def test_global_memory_prompt_and_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _run_context(tmp_path)
    context.request_context["access_token"] = "first-token"
    first = _global_memories()
    second = {**_global_memories(), "profile": {"display_name": "李四"}}
    second.update(principles=[], preferences=[])
    fetch = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(prompt_module, "fetch_global_memories", fetch)
    middleware = GlobalMemoryPromptMiddleware(run_context=context)
    header = "## Global Memory\n\n以下是当前用户在当前 workspace 下的长期上下文，遵循 Host 基础规则。\n\n"
    expected = (
        header + "### 用户资料\n\n- 姓名：张三\n- 时区：Asia/Shanghai\n\n"
        "### 长期原则（必须遵守）\n\n- 说明数据来源。\n\n"
        "### 长期偏好（默认遵循，可被当前明确指令覆盖）\n\n"
        "- 优先使用中文。\n结果保留两位小数。\n- 先给结论。"
    )
    assert await middleware.on_system_prompt(None, "skills") == expected + "\n\nskills"
    assert await middleware.on_system_prompt(None, "skills") == expected + "\n\nskills"
    fetch.assert_awaited_once_with(
        "first-token",
        chat_id=context.chat_id,
    )

    context.request_context["access_token"] = "second-token"
    refreshed = GlobalMemoryPromptMiddleware(run_context=context)
    assert (
        await refreshed.on_system_prompt(None, "")
        == header + "### 用户资料\n\n- 姓名：李四"
    )
    assert await middleware.on_system_prompt(None, "") == expected
    assert fetch.await_count == 2
    fetch.assert_awaited_with(
        "second-token",
        chat_id=context.chat_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        None,
        {"profile": None, "principles": [], "preferences": []},
    ],
)
async def test_unavailable_or_empty_global_memory_is_not_injected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: dict | None,
) -> None:
    fetch = AsyncMock(return_value=content)
    monkeypatch.setattr(prompt_module, "fetch_global_memories", fetch)
    middleware = GlobalMemoryPromptMiddleware(run_context=_run_context(tmp_path))
    assert (
        await middleware.on_system_prompt(None, "original prompt") == "original prompt"
    )
    assert (
        await middleware.on_system_prompt(None, "original prompt") == "original prompt"
    )
    fetch.assert_awaited_once()

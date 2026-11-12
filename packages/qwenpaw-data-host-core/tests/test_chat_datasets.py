from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage

from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.runtime.builder import data_agent as builder_module
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


class FakeTextModel(ChatModelBase):
    def __init__(self) -> None:
        self.model = "fake-dataset-model"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.formatter = DashScopeChatFormatter()

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        _ = (model_name, messages, tools, tool_choice, kwargs)
        return ChatResponse(
            content=[TextBlock(text="ok")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


def test_open_chat_dataset_names_are_per_chat() -> None:
    session = Session.create(identity=TEST_IDENTITY, title="datasets")
    first = session.open_chat(
        text="全部",
        datasource_id="postgresql-a",
        dataset_names=[],
        has_active_chat=False,
    )
    first.mark_status("completed")
    second = session.open_chat(
        text="两张表",
        datasource_id="postgresql-a",
        dataset_names=["orders", "users"],
        has_active_chat=False,
    )

    assert first.dataset_names is None
    assert second.dataset_names == ["orders", "users"]
    assert session.datasource_id == "postgresql-a"
    assert "dataset_names" not in session.__dataclass_fields__


@pytest.mark.asyncio
async def test_chat_row_persists_dataset_names() -> None:
    await reset_engine()
    await init_db()
    try:
        async with get_session_factory()() as db:
            session = Session.create(identity=TEST_IDENTITY, title="persist")
            chat = session.open_chat(
                text="两张表",
                datasource_id="postgresql-a",
                dataset_names=["orders", "users"],
                has_active_chat=False,
            )
            await SessionRepository(db, TEST_IDENTITY).add(session)
            await ChatRepository(db, TEST_IDENTITY).add(chat)
            await db.commit()
            loaded = await ChatRepository(db, TEST_IDENTITY).get(chat.id)
            assert loaded.dataset_names == ["orders", "users"]
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_chat_runtime_stamps_cm_headers_from_chat_dataset_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
) -> None:
    await reset_engine()
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    await init_db()
    session_factory = get_session_factory()

    async with session_factory() as db:
        session = Session.create(
            identity=TEST_IDENTITY,
            agent_id="agent-a",
            title="datasets",
        )
        session_repo = SessionRepository(db, TEST_IDENTITY)
        chat_repo = ChatRepository(db, TEST_IDENTITY)
        await session_repo.add(session)
        all_datasets = session.open_chat(
            text="全部",
            datasource_id="postgresql-a",
            has_active_chat=False,
        )
        await session_repo.save(session)
        await chat_repo.add(all_datasets)
        await db.commit()

    await seed_test_preferences(session_factory)
    monkeypatch.setattr(
        builder_module, "build_model", lambda *_a, **_k: FakeTextModel()
    )
    # The builtin context-manager MCP is an external service; this test only
    # cares about which headers the Host stamps onto it.
    stamped: list[dict[str, Any]] = []

    async def _capture_sync(_self: Any, _workspace: Any, **kwargs: Any) -> None:
        stamped.append(kwargs)

    monkeypatch.setattr(McpRepository, "sync", _capture_sync)
    from qwenpaw_data.host.core.workspace import (
        get_workspace_manager,
        stop_workspace_manager,
    )

    await get_workspace_manager().start()
    try:
        await ChatRuntime(session_factory, all_datasets.id).run(identity=TEST_IDENTITY)
        async with session_factory() as db:
            session = await SessionRepository(db, TEST_IDENTITY).get(
                all_datasets.session_id
            )
            subset = session.open_chat(
                text="两张表",
                datasource_id="postgresql-a",
                dataset_names=["orders", "users"],
                has_active_chat=False,
            )
            await SessionRepository(db, TEST_IDENTITY).save(session)
            await ChatRepository(db, TEST_IDENTITY).add(subset)
            await db.commit()
        await ChatRuntime(session_factory, subset.id).run(identity=TEST_IDENTITY)
    finally:
        await stop_workspace_manager()
        reset_hub()
        await reset_engine()

    assert [item.get("dataset_names") for item in stamped] == [
        None,
        ["orders", "users"],
    ]
    assert [item.get("datasource_id") for item in stamped] == [
        "postgresql-a",
        "postgresql-a",
    ]
    assert [item.get("session_id") for item in stamped] == [
        all_datasets.session_id,
        subset.session_id,
    ]
    assert [item.get("request_id") for item in stamped] == [all_datasets.id, subset.id]

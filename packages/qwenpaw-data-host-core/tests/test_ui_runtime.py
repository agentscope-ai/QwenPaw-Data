from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from qwenpaw_data.host.core.agent.middleware.ui import UiMiddleware
from qwenpaw_data.host.core.api.models.ui import (
    ANSWER_ADAPTER,
    ARGUMENT_TYPES,
)
from qwenpaw_data.host.core.db.engine import get_session_factory, init_db, reset_engine
from qwenpaw_data.host.core.db.tables import ChatRow, SessionRow
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.ui import (
    UI_TOOL_NAMES,
    UiExecution,
    require_ui_tools,
    ui_tools_enabled,
)
from qwenpaw_data.host.core.repositories.chat_command import ChatCommandRepository
from qwenpaw_data.host.core.settings import clear_settings
from pydantic import ValidationError

IDENTITY = Identity("ui-user", "ui-tenant", "ui-workspace")


@pytest.fixture
async def environment():
    await reset_engine()
    await init_db()
    factory = get_session_factory()
    async with factory() as db:
        db.add(SessionRow(id="s", title="test", **asdict(IDENTITY)))
        db.add(
            ChatRow(
                id="c", session_id="s", sequence=1, status="running", **asdict(IDENTITY)
            )
        )
        await db.commit()
    yield factory
    await reset_engine()


def _chat() -> Chat:
    chat = Chat.start(
        session_id="s",
        identity=IDENTITY,
        sequence=1,
        text="hello",
        datasource_id=None,
    )
    chat.id = "c"
    return chat


def _answer(call_id="call"):
    return ANSWER_ADAPTER.validate_python(
        {
            "call_id": call_id,
            "status": "executed",
            "result": {
                "view_revision": "1",
                "completed_steps": ["inspected"],
                "saved": False,
                "elements": [],
            },
        }
    )


async def test_wait_reads_persistent_answer(environment):
    factory = environment
    task = asyncio.create_task(
        UiExecution(factory, "c").wait("call"),
    )
    for _ in range(20):
        await asyncio.sleep(0.05)
        async with factory() as db:
            await ChatCommandRepository(db).add(
                ChatCommand.ui(
                    _chat(),
                    call_id="call",
                    outcome=_answer().model_dump(mode="json", exclude={"call_id"}),
                )
            )
            await db.commit()
        if task.done():
            break
    outcome = await asyncio.wait_for(task, 2)
    assert outcome["status"] == "executed"
    assert outcome["result"]["saved"] is False


async def test_middleware_does_not_execute_on_host(environment):
    factory = environment
    middleware = UiMiddleware(
        run_context=SimpleNamespace(session_factory=factory, chat_id="c")
    )

    async def next_handler(**kwargs):
        raise AssertionError("Host must not execute UI tools")
        yield

    async def collect():
        items = []
        async for item in middleware.on_acting(
            None,
            {"tool_call": SimpleNamespace(name="ui_inspect", id="call")},
            next_handler,
        ):
            items.append(item)
        return items

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.05)
    async with factory() as db:
        await ChatCommandRepository(db).add(
            ChatCommand.ui(
                _chat(),
                call_id="call",
                outcome=_answer().model_dump(mode="json", exclude={"call_id"}),
            )
        )
        await db.commit()
    items = await asyncio.wait_for(task, 2)
    assert json.loads(items[0].content[0].text)["status"] == "executed"


async def test_http_answer_and_identity_isolation(environment):
    import httpx
    from qwenpaw_data.host.core.api.app import create_app
    from qwenpaw_data.host.core.api.deps import get_identity

    _ = environment
    app = create_app()
    app.dependency_overrides[get_identity] = lambda: IDENTITY
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        body = _answer().model_dump(mode="json")
        received = await client.post("/api/sessions/s/chats/c/ui/answer", json=body)
        assert received.status_code == 200 and received.json()["chat"]["id"] == "c"
        mixed = await client.post(
            "/api/sessions/s/chats/c/ui/answer",
            json={**body, "error": {"code": "mixed", "message": ""}},
        )
        assert mixed.status_code == 422
        app.dependency_overrides[get_identity] = lambda: Identity(
            "other", "ui-tenant", "ui-workspace"
        )
        assert (
            await client.post("/api/sessions/s/chats/c/ui/answer", json=body)
        ).status_code == 404


async def test_stopped_chat_rejects_answer(environment):
    import httpx
    from qwenpaw_data.host.core.api.app import create_app
    from qwenpaw_data.host.core.api.deps import get_identity

    factory = environment
    app = create_app()
    app.dependency_overrides[get_identity] = lambda: IDENTITY
    async with factory() as db:
        (await db.get(ChatRow, "c")).status = "canceled"
        await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/sessions/s/chats/c/ui/answer",
            json=_answer().model_dump(mode="json"),
        )
        assert response.status_code == 409


async def test_http_answer_rejects_when_disabled(environment, monkeypatch):
    import httpx
    from qwenpaw_data.host.core.api.app import create_app
    from qwenpaw_data.host.core.api.deps import get_identity

    _ = environment
    monkeypatch.setenv("QWENPAW_DATA_TOOL_UI_ENABLED", "false")
    clear_settings()
    app = create_app()
    app.dependency_overrides[get_identity] = lambda: IDENTITY
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/sessions/s/chats/c/ui/answer",
            json=_answer().model_dump(mode="json"),
        )
        assert response.status_code == 409
        assert response.json()["code"] == "CONFLICT"


def test_ui_tools_enabled_defaults_true():
    assert ui_tools_enabled() is True


def test_require_ui_tools_fails_when_disabled(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_TOOL_UI_ENABLED", "false")
    clear_settings()
    with pytest.raises(RuntimeError, match="UI tools are disabled"):
        require_ui_tools()


def test_add_metadata_skips_when_disabled(environment, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_TOOL_UI_ENABLED", "false")
    clear_settings()
    metadata: dict = {}
    UiExecution(environment, "c").add_metadata(
        tool_name="ui_inspect", metadata=metadata
    )
    assert metadata == {}


@pytest.mark.parametrize(
    "name,args",
    [
        ("ui_inspect", {"scope": "current", "code": "bad"}),
        ("ui_open_page", {"page_id": "datasets", "params": {"url": "bad"}}),
        (
            "ui_open_object",
            {
                "object_ref": {
                    "kind": "cm.dataset",
                    "dataset_id": True,
                    "domain_id": 2,
                    "datasource_id": "x",
                }
            },
        ),
        (
            "ui_act",
            {
                "element_id": "save",
                "expected_view_revision": "1",
                "action": {"type": "click", "value": "x"},
            },
        ),
    ],
)
def test_contract_rejects_extra_or_invalid_fields(name, args):
    with pytest.raises(ValidationError):
        ARGUMENT_TYPES[name].model_validate(args)


def test_ui_tool_names_match_argument_types():
    assert set(ARGUMENT_TYPES) == UI_TOOL_NAMES

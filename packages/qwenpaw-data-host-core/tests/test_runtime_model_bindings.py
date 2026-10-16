"""Run admission can change a model without changing conversation memory."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.preference import UserRuntimeConfig
from qwenpaw_data.host.core.providers.registry import ActiveModel
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime import chat_runtime


async def test_later_turn_uses_new_snapshot_and_keeps_session_agent(monkeypatch):
    monkeypatch.setattr(chat_runtime, "_followup_enabled", lambda: False)
    monkeypatch.setattr(chat_runtime, "_biztrace_enabled", lambda: False)
    monkeypatch.setattr(chat_runtime, "OutputStream", Mock())
    monkeypatch.setattr(
        chat_runtime, "Envelope", lambda _: SimpleNamespace(begin=AsyncMock())
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.providers.factory.build_model", lambda model: model
    )
    identity = Identity.anonymous()
    chat = SimpleNamespace(
        id="run",
        session_id="session",
        status="running",
        datasource_id="ds",
        identity=identity,
        user_input="test",
        artifact_comments=[],
        attachments=[],
        dataset_names=[],
    )
    client = SimpleNamespace(set_runtime_headers=AsyncMock())
    memory = object()
    agent = SimpleNamespace(
        model=None,
        memory=memory,
        toolkit=SimpleNamespace(_qwenpaw_context_clients=[client]),
    )
    host = SimpleNamespace(
        model=None,
        _agent=agent,
        get_agent=AsyncMock(return_value=agent),
        workspace=None,
        paths=None,
    )
    contexts = []
    for name in ("first", "second"):
        model = ActiveModel(
            provider_id="openai",
            model_id=name,
            api_key="private",
            base_url="https://example.invalid/v1",
            chat_model="OpenAIChatModel",
            name=name,
        )
        config = UserRuntimeConfig(default=model, light=model)
        runtime = ChatRuntime(
            chats=SimpleNamespace(get=AsyncMock(return_value=chat)),
            events=None,
            hosts=SimpleNamespace(get=lambda **_: host),
        )
        runtime._executor = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        runtime._artifact_snapshot = lambda: {}
        runtime.report_analysis_progress = AsyncMock()
        runtime._deliver_biztrace = AsyncMock()
        runtime._deliver_followup = AsyncMock()
        runtime._finish = AsyncMock()
        runtime._schedule_settlement = Mock()
        await runtime._run(
            "run", identity=identity, runtime_config=config, context_model_header=name
        )
        assert runtime._finish.call_args.args[2] == "completed"
        assert agent.model.model_id == name
        assert runtime._executor.start.call_args.args[0] is agent
        assert agent.memory is memory
        assert runtime._run_context.user_runtime_config is config
        contexts.append(runtime._run_context)
    assert contexts[0].user_runtime_config.default.model_id == "first"
    assert contexts[1].user_runtime_config.default.model_id == "second"
    assert [call.args[0] for call in client.set_runtime_headers.call_args_list] == [
        {"x-qwenpaw-model-binding": "first"},
        {"x-qwenpaw-model-binding": "second"},
    ]

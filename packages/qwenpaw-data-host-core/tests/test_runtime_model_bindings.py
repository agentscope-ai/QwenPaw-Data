"""Turn-local model settings preserve persisted conversation state, not secrets."""

from types import SimpleNamespace

from agentscope.message import UserMsg, TextBlock

from qwenpaw_data.host.core.api.models.runtime_models import RuntimeModels
from qwenpaw_data.host.core.domain.preference import UserPreferences
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime

from . import test_chat_runtime_finish

workspace_chat = test_chat_runtime_finish.workspace_chat


async def test_later_turn_uses_new_snapshot_and_keeps_session_state(
    workspace_chat, monkeypatch,
):
    case = workspace_chat
    contexts = []

    async def build(*, run_context, envelope, state):
        contexts.append(run_context)
        if len(contexts) == 1:
            state.context = [UserMsg(name="user", content=[TextBlock(text="remember this")])]
        else:
            assert state.context[0].content[0].text == "remember this"
        return SimpleNamespace(name="test", state=state)

    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.get_agent_builder",
        lambda _: SimpleNamespace(build=build),
    )
    headers = []

    async def sync(self, workspace, **kwargs):
        headers.append(kwargs["context_model_header"])

    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.McpRepository.sync", sync
    )
    prefs = UserPreferences(
        user_id=case.identity.user_id, react_max_iters=17,
        tool_confirmation_level="high", tool_timeout_seconds=23,
    )
    for index, name in enumerate(("first", "second")):
        binding = RuntimeModels.model_validate({
            "analysis": {
                "provider_id": "openai", "model_id": name,
                "api_key": "turn-private-key", "base_url": "https://example.invalid/v1",
            },
            "inherit_analysis": ["lightweight", "databridge"],
        })
        config = binding.runtime_config(prefs)
        assert config.react_max_iters == 17
        assert config.tool_confirmation_level == "high"
        assert config.tool_timeout_seconds == 23
        assert prefs.default_model_id is None
        assert prefs.providers == {}
        if index:
            async with case.factory() as db:
                session = await SessionRepository(db, case.identity).get(case.session.id)
                chat = session.open_chat(text="next", datasource_id=None, has_active_chat=False)
                await SessionRepository(db, case.identity).save(session)
                await ChatRepository(db, case.identity).add(chat)
                await db.commit()
            runtime = ChatRuntime(case.factory, chat.id)
            monkeypatch.setattr(runtime, "_log_chat_start", lambda *args: None)
        else:
            runtime, chat = case.runtime, case.chat
        await runtime.run(
            identity=case.identity, runtime_config=config,
            context_model_header=binding.resolved_context_header(config),
        )
        async with case.factory() as db:
            saved = await ChatRepository(db, case.identity).get(chat.id)
            assert saved.status == "completed", saved.error
            assert "turn-private-key" not in str(saved.agent_state)
        assert contexts[-1].user_runtime_config is config
        assert contexts[-1].user_runtime_config.default.model_id == name
    assert contexts[0].user_runtime_config.default.model_id == "first"
    assert headers[0] != headers[1]
    case.executor.start.assert_awaited()

"""Offline regressions for the pinned async model clients and usage boundary."""

import asyncio
import traceback
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from context_manager.model import ChatMessage, LlmError, OpenAIEmbedCaller, OpenAILlmCaller
from context_manager.model import embedding, embedding_cache, llm
from context_manager.model.embedding_cache import EmbeddingCache
from context_manager.model.usage import ModelUsageTracker, bind_usage_context, current_usage_context


@pytest.fixture(autouse=True)
def client_settings(monkeypatch):
    settings = SimpleNamespace(
        model=SimpleNamespace(ignore_proxy=False, timeout=30, sdk_max_retries=2),
        embedding=SimpleNamespace(
            sdk_max_retries=1, batch_size=10, max_concurrency=2,
            max_input_chars=100, cache_ttl_seconds=600,
            cache_max_entries=64, rate_limit_retries=2,
        ),
    )
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    monkeypatch.setattr(embedding, "get_settings", lambda: settings)
    monkeypatch.setattr(embedding, "embedding_cache", EmbeddingCache())
    return settings


def sdk_client(vector_value=None):
    async def respond(**options):
        return SimpleNamespace(
            model=options["model"], usage=SimpleNamespace(total_tokens=4),
            data=[
                SimpleNamespace(
                    index=i,
                    embedding=(float(len(text)) if vector_value is None else vector_value,)
                    * options.get("dimensions", 2),
                )
                for i, text in reversed(tuple(enumerate(options["input"])))
            ],
        )

    request = AsyncMock(side_effect=respond)

    async def raw(**options):
        response = await request(**options)
        return SimpleNamespace(parse=lambda: response)

    completion = AsyncMock(return_value=SimpleNamespace(
        model="served-chat", usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
    ))
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=completion)),
        embeddings=SimpleNamespace(with_raw_response=SimpleNamespace(create=raw)),
        close=AsyncMock(), base_url="https://models.example/v1",
    ), completion, request


async def test_completion_options_and_borrowed_cleanup():
    sdk, completion, _ = sdk_client()
    caller = OpenAILlmCaller(client=sdk, model="chat-model")
    async with caller:
        assert await caller.complete(
            (ChatMessage("system", "instructions"), ChatMessage("user", "question")),
            model="override", temperature=0, max_tokens=9, json_mode=True,
            enable_reasoning=False, timeout_seconds=3,
        ) == "answer"
    completion.assert_awaited_once_with(
        model="override", messages=[
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "question"},
        ], temperature=0, max_tokens=9, response_format={"type": "json_object"},
        extra_body={"enable_thinking": False}, timeout=3,
    )
    await caller.aclose()
    sdk.close.assert_not_awaited()
    with pytest.raises(LlmError, match="closed"):
        await caller.complete((ChatMessage("user", "question"),))


@pytest.mark.parametrize("options", [
    {"temperature": -1}, {"temperature": 3}, {"max_tokens": 0},
    {"json_mode": 1}, {"enable_reasoning": "yes"}, {"timeout_seconds": 0},
    {"model": " padded"},
])
async def test_completion_validation_precedes_transport(options):
    sdk, request, _ = sdk_client()
    async with OpenAILlmCaller(client=sdk, model="chat") as caller:
        with pytest.raises((TypeError, ValueError)):
            await caller.complete((ChatMessage("user", "question"),), **options)
    request.assert_not_awaited()


@pytest.mark.parametrize("messages", [[], "text", [{"role": "user", "content": "text"}]])
async def test_completion_rejects_non_messages(messages):
    sdk, request, _ = sdk_client()
    async with OpenAILlmCaller(client=sdk, model="chat") as caller:
        with pytest.raises((TypeError, ValueError)):
            await caller.complete(messages)
    request.assert_not_awaited()


@pytest.mark.parametrize("kind", ["llm", "embedding"])
async def test_owned_clients_honor_ignore_proxy_and_close_once(kind, client_settings, monkeypatch):
    module = llm if kind == "llm" else embedding
    cls = OpenAILlmCaller if kind == "llm" else OpenAIEmbedCaller
    client_settings.model.ignore_proxy = True
    transport = SimpleNamespace(aclose=AsyncMock())
    transports, captured = [], []

    def http_client(**options):
        transports.append(options)
        return transport

    sdk, _, _ = sdk_client()

    async def close():
        await transport.aclose()

    sdk.close.side_effect = close

    def factory(**options):
        captured.append(options)
        return sdk

    monkeypatch.setattr(module.httpx, "AsyncClient", http_client)
    monkeypatch.setattr(module, "AsyncOpenAI", factory)
    caller = cls(api_key="synthetic-secret", model="public-model", base_url="https://models.example/v1")
    async with caller:
        pass
    await caller.aclose()
    assert transports == [{"trust_env": False}]
    assert captured[0]["http_client"] is transport
    assert captured[0]["max_retries"] == (2 if kind == "llm" else 1)
    sdk.close.assert_awaited_once()
    transport.aclose.assert_awaited_once()


async def test_embedding_borrowed_client_and_snapshot_are_safe():
    sdk, _, request = sdk_client()
    caller = OpenAIEmbedCaller(client=sdk, model="embed", dimensions=2)
    async with caller:
        snapshot = caller.config_snapshot
        snapshot["model"] = "modified"
        assert caller.config_snapshot["model"] == "embed"
        assert await caller.embed(("text",)) == ((4.0, 4.0),)
    await caller.aclose()
    sdk.close.assert_not_awaited()
    with pytest.raises(LlmError, match="closed"):
        await caller.embed(("text",))
    assert request.await_count == 1


async def test_embedding_batches_bound_concurrency_and_restore_order():
    sdk, _, request = sdk_client()
    first_release = asyncio.Event()
    active = peak = 0
    completed = []

    async def respond(**options):
        nonlocal active, peak
        start = int(options["input"][0])
        active += 1
        peak = max(peak, active)
        try:
            if start == 0:
                await first_release.wait()
            else:
                await asyncio.sleep(0)
            completed.append(start)
            if start == 2:
                first_release.set()
            return SimpleNamespace(data=[
                SimpleNamespace(index=i, embedding=(float(text), 1.0))
                for i, text in reversed(tuple(enumerate(options["input"])))
            ])
        finally:
            active -= 1

    request.side_effect = respond
    async with OpenAIEmbedCaller(client=sdk, model="embed", batch_size=2, max_concurrency=2) as caller:
        result = await asyncio.wait_for(caller.embed(tuple(str(i) for i in range(13)), dimensions=2), 2)
    assert result == tuple((float(i), 1.0) for i in range(13))
    assert peak == 2 and active == 0 and completed[0] == 2
    assert [len(call.kwargs["input"]) for call in request.await_args_list] == [2] * 6 + [1]


@pytest.mark.parametrize("cancel", [False, True])
async def test_batch_failure_or_cancellation_drains_workers(cancel):
    sdk, _, request = sdk_client()
    both_started = asyncio.Event()
    active = calls = 0

    async def respond(**options):
        nonlocal active, calls
        calls += 1
        active += 1
        if calls == 2:
            both_started.set()
        try:
            await both_started.wait()
            if options["input"] == ["0"] and not cancel:
                raise RuntimeError("synthetic-secret provider failure")
            await asyncio.Event().wait()
        finally:
            active -= 1

    request.side_effect = respond
    async with OpenAIEmbedCaller(client=sdk, model="embed", batch_size=1, max_concurrency=2) as caller:
        task = asyncio.create_task(caller.embed(tuple(str(i) for i in range(5))))
        await asyncio.wait_for(both_started.wait(), 2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(LlmError, match="embedding request failed"):
                await asyncio.wait_for(task, 2)
    assert active == 0 and calls == 2


async def test_cancelled_decoding_drains_thread_and_preserves_usage_context():
    sdk, _, _ = sdk_client()
    entered, release, finished = Event(), Event(), Event()
    seen = []

    def parse():
        entered.set()
        try:
            assert release.wait(2)
            seen.append(current_usage_context().session_id)
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=(1, 2))])
        finally:
            finished.set()

    sdk.embeddings.with_raw_response.create = AsyncMock(return_value=SimpleNamespace(parse=parse))
    caller = OpenAIEmbedCaller(client=sdk, model="embed")
    with bind_usage_context(session_id="request-session"):
        task = asyncio.create_task(caller.embed(("text",)))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    await caller.aclose()
    assert finished.is_set() and seen == ["request-session"]


async def test_cache_deduplicates_clipped_misses_without_publishing_failures():
    sdk, _, request = sdk_client()
    async with OpenAIEmbedCaller(client=sdk, model="embed", dimensions=2) as caller:
        prefix = "x" * 100
        assert await caller.embed((prefix + "one", "ab", prefix + "two")) == (
            (100.0, 100.0), (2.0, 2.0), (100.0, 100.0),
        )
        request.assert_awaited_once_with(model="embed", input=[prefix, "ab"], dimensions=2)
        assert await caller.embed(("ab", prefix + "three")) == ((2.0, 2.0), (100.0, 100.0))
        assert request.await_count == 1

    sdk, _, request = sdk_client()
    original = request.side_effect

    async def fail_second(**options):
        if options["input"] == ["b"]:
            raise RuntimeError("provider unavailable")
        return await original(**options)

    request.side_effect = fail_second
    async with OpenAIEmbedCaller(client=sdk, model="embed", batch_size=1, max_concurrency=1) as caller:
        with pytest.raises(LlmError):
            await caller.embed(("a", "b"))
        await caller.embed(("a",))
    assert [call.kwargs["input"] for call in request.await_args_list] == [["a"], ["b"], ["a"]]


async def test_borrowed_callers_never_accidentally_share_cache():
    sdk, _, request = sdk_client()
    for _ in range(2):
        async with OpenAIEmbedCaller(client=sdk, model="embed", cache_namespace=("t", "w", "u")) as caller:
            await caller.embed(("same-text",))
            await caller.embed(("same-text",))
    assert request.await_count == 2


@pytest.mark.parametrize("changed", [
    {}, {"cache_namespace": ("other-tenant", "w", "u")},
    {"cache_namespace": ("t", "other-workspace", "u")},
    {"cache_namespace": ("t", "w", "other-user")},
    {"api_key": "rotated-secret"}, {"model": "other-model"},
    {"base_url": "https://other.example/v1"}, {"dimensions": 3},
])
async def test_owned_cache_is_scoped_by_identity_credential_and_configuration(changed, monkeypatch):
    requests, clients = [], []

    def factory(**options):
        sdk, _, request = sdk_client(vector_value=float(len(requests) + 1))
        clients.append(sdk)
        requests.append(request)
        return sdk

    monkeypatch.setattr(embedding, "AsyncOpenAI", factory)
    options = dict(api_key="original-secret", model="embed", dimensions=2,
                   base_url="https://models.example/v1", cache_namespace=("t", "w", "u"))
    async with OpenAIEmbedCaller(**options) as caller:
        first = await caller.embed(("same-text",))
    async with OpenAIEmbedCaller(**(options | changed)) as caller:
        second = await caller.embed(("same-text",))
    assert requests[0].await_count == 1
    assert requests[1].await_count == bool(changed)
    assert (first == second) is (not changed)
    assert "original-secret" not in repr(embedding.embedding_cache._entries)
    assert "rotated-secret" not in repr(embedding.embedding_cache._entries)
    for sdk in clients:
        sdk.close.assert_awaited_once()


@pytest.mark.parametrize("texts,options", [
    ("text", {}), ((), {}), (("",), {}), ((1,), {}),
    (("text",), {"dimensions": True}), (("text",), {"dimensions": 0}),
    (("text",), {"model": " padded"}),
])
async def test_embedding_input_validation_precedes_transport(texts, options):
    sdk, _, request = sdk_client()
    async with OpenAIEmbedCaller(client=sdk, model="embed") as caller:
        with pytest.raises((TypeError, ValueError)):
            await caller.embed(texts, **options)
    request.assert_not_awaited()


@pytest.mark.parametrize("data", [
    [], [SimpleNamespace(index=1, embedding=(1, 2))],
    [SimpleNamespace(index=True, embedding=(1, 2))],
    [SimpleNamespace(index="secret-index", embedding=(1, 2))],
    [SimpleNamespace(index=0, embedding=())],
    [SimpleNamespace(index=0, embedding=(True, 2))],
    [SimpleNamespace(index=0, embedding=(float("nan"), 2))],
    [SimpleNamespace(index=0, embedding=(float("inf"), 2))],
    [SimpleNamespace(index=0, embedding=(1, 2, 3))],
])
async def test_embedding_response_validation(data):
    sdk, _, request = sdk_client()
    request.side_effect = None
    request.return_value = SimpleNamespace(data=data)
    async with OpenAIEmbedCaller(client=sdk, model="embed", dimensions=2) as caller:
        with pytest.raises(LlmError):
            await caller.embed(("text",))
    assert not embedding.embedding_cache._entries


@pytest.mark.parametrize("message", ["HTTP 429", "RateLimitError", "limit_burst_rate"])
async def test_rate_limit_retries_are_bounded_and_never_log_keys(message, monkeypatch, caplog):
    sdk, _, request = sdk_client()
    response = await request.side_effect(model="embed", input=["a"], dimensions=2)
    request.side_effect = [RuntimeError(message + " synthetic-secret"), RuntimeError(message), response]
    sleep = AsyncMock()
    monkeypatch.setattr(embedding.asyncio, "sleep", sleep)
    monkeypatch.setattr(embedding.random, "uniform", lambda *_: 0.1)
    async with OpenAIEmbedCaller(client=sdk, model="embed", dimensions=2) as caller:
        assert await caller.embed(("a",)) == ((1.0, 1.0),)
    assert request.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.6, 1.1]
    assert "synthetic-secret" not in caplog.text


@pytest.mark.parametrize("kind", ["llm", "embedding"])
async def test_provider_failure_tracebacks_are_redacted(kind, caplog, monkeypatch):
    sdk, completion, request = sdk_client()
    failure = RuntimeError("provider returned synthetic-secret")
    completion.side_effect = failure
    request.side_effect = failure
    cls = OpenAILlmCaller if kind == "llm" else OpenAIEmbedCaller
    async with cls(client=sdk, model="model") as caller:
        with pytest.raises(LlmError) as error:
            if kind == "llm":
                await caller.complete((ChatMessage("user", "question"),))
            else:
                await caller.embed(("text",))
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))
    assert "synthetic-secret" not in caplog.text


async def test_metering_records_attribution_once_and_excludes_probes():
    events = []
    tracker = ModelUsageTracker(lambda event: events.append(event) or True,
        tenant_id="tenant", workspace_id="workspace", user_id="user", provider_id="provider")
    sdk, _, _ = sdk_client()
    with bind_usage_context(session_id="session", chat_id="chat", source_step="step"):
        async with OpenAIEmbedCaller(client=sdk, model="embed", usage_tracker=tracker) as caller:
            await caller.embed(("cached",))
            await caller.embed(("cached",))
        with bind_usage_context(probe=True):
            async with OpenAILlmCaller(client=sdk, model="chat", usage_tracker=tracker) as caller:
                await caller.complete((ChatMessage("user", "question"),))
        async with OpenAILlmCaller(client=sdk, model="chat", usage_tracker=tracker) as caller:
            await caller.complete((ChatMessage("user", "question"),))
    assert len(events) == 2
    assert [(e.model_type, e.input_tokens, e.output_tokens) for e in events] == [
        ("embedding", 4, 0), ("chat", 7, 3),
    ]
    assert all((e.tenant_id, e.workspace_id, e.user_id, e.provider_id) ==
               ("tenant", "workspace", "user", "provider") for e in events)
    assert all((e.session_id, e.chat_id, e.source, e.source_step) ==
               ("session", "chat", "cm", "step") for e in events)
    assert current_usage_context().session_id == ""


def test_cache_ttl_lru_and_capacity(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(embedding_cache, "monotonic", lambda: now[0])
    cache = EmbeddingCache()
    keys = [("identity", "endpoint", "model", 2, str(i)) for i in range(3)]
    cache.put(keys[0], (1.0, 2.0), max_entries=2)
    cache.put(keys[1], (3.0, 4.0), max_entries=2)
    assert cache.get(keys[0], ttl_seconds=1) == (1.0, 2.0)
    cache.put(keys[2], (5.0, 6.0), max_entries=2)
    assert cache.get(keys[1], ttl_seconds=1) is None
    now[0] = 2.0
    assert cache.get(keys[0], ttl_seconds=1) is None
    assert cache.get(keys[2], ttl_seconds=1) is None
    assert not cache._entries

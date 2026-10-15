import asyncio
import base64
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from context_manager.model_config_store import ModelConfigStore
from context_manager.runtime_model_binding import (
    RuntimeModelBindingMiddleware,
    llm_binding,
)
from context_manager.openai_client import get_openai_client


def header(model, key="private-key"):
    return base64.urlsafe_b64encode(
        json.dumps(
            {"model": model, "api_key": key, "base_url": "https://models.example/v1"}
        ).encode()
    ).decode()


@pytest.mark.asyncio
async def test_binding_is_authenticated_request_local_and_not_persisted(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "service-key")
    store = ModelConfigStore(tmp_path / "model.json")
    store.update_llm(
        {
            "model": "global",
            "api_key": "global-key",
            "base_url": "https://global.example/v1",
        }
    )
    app = FastAPI()
    app.add_middleware(RuntimeModelBindingMiddleware)

    @app.get("/model")
    async def current():
        before = store.llm_model
        await asyncio.sleep(0.01)
        return {
            "before": before,
            "after": store.llm_model,
            "embedding": store.embed_model,
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def call(model):
            return await client.get(
                "/model",
                headers={
                    "Authorization": "Bearer service-key",
                    "x-qwenpaw-model-binding": header(model),
                },
            )

        one, two = await asyncio.gather(call("one"), call("two"))
        assert one.json()["before"] == one.json()["after"] == "one"
        assert two.json()["before"] == two.json()["after"] == "two"
        denied = await client.get(
            "/model", headers={"x-qwenpaw-model-binding": header("attacker")}
        )
        assert denied.status_code == 403
        assert (await client.get("/model")).json()["after"] == "global"
    assert llm_binding.get() is None
    assert "private-key" not in (tmp_path / "model.json").read_text()


def test_client_cache_is_keyed_by_runtime_connection(tmp_path):
    store = ModelConfigStore(tmp_path / "model.json")
    get_openai_client.cache_clear()
    with patch(
        "context_manager.model_config_store.get_model_config_store", return_value=store
    ):
        tokens = []
        try:
            tokens.append(
                llm_binding.set(
                    {
                        "model": "a",
                        "api_key": "key-a",
                        "base_url": "https://a.example/v1",
                    }
                )
            )
            a = get_openai_client()
            tokens.append(
                llm_binding.set(
                    {
                        "model": "b",
                        "api_key": "key-b",
                        "base_url": "https://b.example/v1",
                    }
                )
            )
            b = get_openai_client()
            assert a is not b
            assert a.api_key == "key-a" and b.api_key == "key-b"
        finally:
            for token in reversed(tokens):
                llm_binding.reset(token)
            get_openai_client.cache_clear()


@pytest.mark.asyncio
async def test_mcp_forwards_each_calls_binding_to_context_rest(tmp_path, monkeypatch):
    from context_manager.mcp import cm_server
    from context_manager.mcp.http_common import create_streamable_mcp, mount_streamable_http

    monkeypatch.setenv('QWENPAW_DATA_API_TOKEN', 'service-key')
    monkeypatch.setenv('QWENPAW_DATA_CLIENT_API_TOKEN', 'service-key')
    server = create_streamable_mcp('model-binding-test')
    monkeypatch.setattr(cm_server, 'mcp', server)
    store = ModelConfigStore(tmp_path / 'model.json')
    app = FastAPI()
    app.add_middleware(RuntimeModelBindingMiddleware)
    monkeypatch.setattr(server, '_cm_app', app, raising=False)

    @app.get('/api/v1/cm/domains')
    async def domains():
        await asyncio.sleep(.01)
        return {'model': store.llm_model}

    @server.tool()
    async def inspect_binding() -> str:
        return json.dumps(await cm_server._get('domains'))

    manager = mount_streamable_http(app, server, path='/mcp/v1/cm')
    async with manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            async def call(model):
                response = await client.post('/mcp/v1/cm', headers={
                    'Authorization': 'Bearer service-key',
                    'x-qwenpaw-model-binding': header(model),
                    'Accept': 'application/json, text/event-stream',
                }, json={'jsonrpc':'2.0','id':model,'method':'tools/call','params':{'name':'inspect_binding','arguments':{}}})
                assert response.status_code == 200, response.text
                return json.loads(response.json()['result']['content'][0]['text'])
            one, two = await asyncio.gather(call('workspace-one'), call('workspace-two'))
    assert one == {'model':'workspace-one'}
    assert two == {'model':'workspace-two'}

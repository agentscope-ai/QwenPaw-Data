import asyncio
import base64
import json

import httpx
import pytest
from fastapi import FastAPI

from context_manager.runtime_model_binding import (
    HEADER,
    RuntimeModelBindingMiddleware,
    llm_binding,
)


def encoded(binding):
    return base64.urlsafe_b64encode(json.dumps(binding).encode()).decode()


def header(model, key="private-key"):
    return encoded(
        {"model": model, "api_key": key, "base_url": "https://models.example/v1"}
    )


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "service-key")
    application = FastAPI()
    application.add_middleware(RuntimeModelBindingMiddleware)

    @application.get("/model")
    async def current():
        before = llm_binding.get()
        await asyncio.sleep(0)
        after = await asyncio.to_thread(llm_binding.get)
        return {
            "before": before["model"] if before else None,
            "after": after["model"] if after else None,
        }

    @application.get("/failure")
    async def failure():
        raise RuntimeError("test operation failed")

    return application


@pytest.mark.asyncio
async def test_binding_is_authenticated_request_local_and_propagates_to_workers(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        async def call(model):
            return await client.get(
                "/model",
                headers={"Authorization": "Bearer service-key", HEADER: header(model)},
            )

        one, two = await asyncio.gather(call("one"), call("two"))
        assert one.json() == {"before": "one", "after": "one"}
        assert two.json() == {"before": "two", "after": "two"}
        assert (await client.get("/model")).json() == {"before": None, "after": None}
    assert llm_binding.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", [None, "Bearer wrong-key", "Bearer read-key"])
async def test_untrusted_credentials_cannot_supply_model_bindings(app, credential):
    headers = {HEADER: header("attacker")}
    if credential is not None:
        headers["Authorization"] = credential
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/model", headers=headers)
    assert response.status_code == 403
    assert "private-key" not in response.text
    assert llm_binding.get() is None


@pytest.mark.asyncio
async def test_local_mode_cannot_enable_binding_without_a_machine_token(app, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/model", headers={"Authorization": "Bearer service-key", HEADER: header("one")}
        )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding",
    [
        {},
        {"api_key": "test-secret", "model": "one"},
        {"api_key": "test-secret", "model": "one", "base_url": "file:///tmp/model"},
        {"api_key": "test-secret", "model": "one", "base_url": "https://test-secret@models.example"},
        {"api_key": "test-secret", "model": "one", "base_url": "https://models.example:99999"},
        {"api_key": "test-secret", "model": "one", "base_url": "https://[invalid"},
        {"api_key": "test-secret", "model": "one", "base_url": "https://models.example/#fragment"},
        {"api_key": "test-secret", "model": " one", "base_url": "https://models.example"},
        {"api_key": "test-secret\n", "model": "one", "base_url": "https://models.example"},
        {"api_key": "test-secret", "model": 1, "base_url": "https://models.example"},
    ],
)
async def test_invalid_binding_errors_do_not_expose_credentials(app, binding):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/model",
            headers={"Authorization": "Bearer service-key", HEADER: encoded(binding)},
        )
    assert response.status_code == 422
    assert "test-secret" not in response.text
    assert llm_binding.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "!not-base64!", "A" * 16385])
async def test_malformed_binding_is_rejected(app, raw):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/model", headers={"Authorization": "Bearer service-key", HEADER: raw}
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_authorization_headers_do_not_grant_binding_authority(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/model",
            headers=[
                ("Authorization", "Bearer service-key"),
                ("Authorization", "Bearer read-key"),
                (HEADER, header("one")),
            ],
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_non_ascii_credentials_are_rejected_without_a_server_error(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/model",
            headers=[
                (b"Authorization", b"Bearer \xff"),
                (HEADER.encode(), header("one").encode()),
            ],
        )
    assert response.status_code == 403
    assert llm_binding.get() is None


@pytest.mark.asyncio
async def test_duplicate_model_binding_headers_are_rejected(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/model",
            headers=[
                ("Authorization", "Bearer service-key"),
                (HEADER, header("one")),
                (HEADER, header("two")),
            ],
        )
    assert response.status_code == 422
    assert llm_binding.get() is None


@pytest.mark.asyncio
async def test_failed_operations_reset_the_binding(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(RuntimeError, match="test operation failed"):
            await client.get(
                "/failure",
                headers={"Authorization": "Bearer service-key", HEADER: header("one")},
            )
    assert llm_binding.get() is None

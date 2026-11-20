"""Exercise the frontend UG contract through authenticated HTTP and real business plans."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.auth import WriteOwner
from context_manager.schema import (
    COLUMN,
    DIMENSION,
    ENTITY,
    EVENT,
    GRAPH_SCHEMA,
    HAS_MEMORY,
    METRIC,
    USER,
    USER_MEMORY,
)
from context_manager.sql import RdbConnector
from context_manager.store import StoreError
from context_manager.store.neo4j import GraphTarget
from qwenpaw_data_auth.config import AuthConfig
from fastapi.testclient import TestClient

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.models.support import StaticModelService
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.support import MemoryGraphStore
from cm.user_memory.support import FakeLlmCaller


class UserGraphStore(MemoryGraphStore):
    """Add visible reference traversal and failed-write injection to the transaction fake."""

    def __init__(self):
        super().__init__()
        self.reject_patch = False
        self.before_atomic = None

    def patch_nodes(self, spec, patches, *, scope):
        if self.reject_patch:
            return (False,) * len(patches)
        return super().patch_nodes(spec, patches, scope=scope)

    def create_edges(self, spec, edges, *, fields=None, scope):
        if self.fail_edge_write:
            raise StoreError("backend write failed", operation="create_edges")
        return super().create_edges(spec, edges, fields=fields, scope=scope)

    def atomic(self, work):
        if self.before_atomic is not None:
            callback, self.before_atomic = self.before_atomic, None
            callback()
        return super().atomic(work)


@pytest.fixture
def api():
    store = UserGraphStore()
    caller = FakeLlmCaller(embeddings=((1.0, 0.0),))
    manager = StaticModelService(caller)
    runtime = bound_context_runtime(
        store,
        RdbConnector(),
        make_glossary(GRAPH_SCHEMA),
        models=manager,
    )
    app = create_api_app()
    bind_context_runtime(app, runtime)
    app.state.test_context = authorization()
    install_context_authentication(
        app,
        AuthConfig(secret_keys=["test-secret"], control_plane_dsn="sqlite:///:memory:"),
        GraphTarget("tenant-1", "workspace-1"),
        authorizer=lambda request: app.state.test_context,
    )
    with TestClient(app) as client:
        yield client, store, manager


def create(client, store, **overrides):
    seed_user(store, user_id=client.app.state.test_context.user_id)
    response = client.post(
        "/api/v1/ug/me/memories",
        json={
            "type": "preference",
            "description": "请先给我结论，再解释具体分析过程。",
            **overrides,
        },
    )
    assert response.status_code in {200, 201}, response.text
    return response


def seed_user(store, *, user_id="user-1", workspace_id="workspace-1"):
    scope = store.access.issue_write_scope(
        authorization(user_id=user_id, workspace_id=workspace_id),
        owner=WriteOwner.USER,
        zone="user",
    )
    users = store.list_nodes(
        USER, filters={"user_id": user_id, "owner_id": user_id}, scope=scope
    )
    return (
        users[0]
        if users
        else store.get_nodes(
            USER,
            store.create_nodes(
                USER,
                ({"user_id": user_id, "profile": "{}", "status": "active"},),
                scope=scope,
            ),
            scope=scope,
        )[0]
    )


def seed_memory(
    store,
    *,
    user_id="user-1",
    workspace_id="workspace-1",
    memory_id="seed-memory",
    memory_type="preference",
    status="active",
    description="请先给我结论，再解释具体分析过程。",
):
    scope = store.access.issue_write_scope(
        authorization(user_id=user_id, workspace_id=workspace_id),
        owner=WriteOwner.USER,
        zone="user",
    )
    user = seed_user(store, user_id=user_id, workspace_id=workspace_id)
    memory = store.get_nodes(
        USER_MEMORY,
        store.create_nodes(
            USER_MEMORY,
            (
                {
                    "id": memory_id,
                    "type": memory_type,
                    "description": description,
                    "triggers": (),
                    "source_type": "user_implicit",
                    "source_refs": ("chat:first", "chat:second"),
                    "status": status,
                },
            ),
            scope=scope,
        ),
        scope=scope,
    )[0]
    store.create_edges(HAS_MEMORY, ((user.key, memory.key, {}),), scope=scope)[0]
    return memory


def seed_target(store, spec=ENTITY, *, workspace_id="workspace-1", user_id="user-1"):
    values = {
        ENTITY: {"name": "付费用户", "type": "concept", "identity_hash": "paying-user"},
        EVENT: {
            "name": "客户续约",
            "type": "business_event",
            "identity_hash": "renewal",
        },
        METRIC: {"name": "续费率", "domain": "customers"},
        DIMENSION: {"name": "渠道", "domain": "customers"},
        COLUMN: {"name": "region", "table": "customers", "data_type": "text"},
    }[spec]
    scope = store.access.issue_write_scope(
        authorization(user_id=user_id, workspace_id=workspace_id),
        owner=WriteOwner.USER,
        datasource_id="ds-1" if spec is COLUMN else None,
    )
    node = store.get_nodes(
        spec, store.create_nodes(spec, (values,), scope=scope), scope=scope
    )[0]
    return {"graph": spec.zone.value, "label": spec.name, "key": node.key}


@pytest.mark.asyncio
async def test_host_consumes_global_memories_through_http_and_refreshes_each_run(
    api, monkeypatch
):
    prompt = pytest.importorskip("qwenpaw_data.host.core.agent.middleware.prompt")
    cm_client = pytest.importorskip("qwenpaw_data.host.core.integrations.cm.client")
    client, store, _manager = api
    seed_memory(
        store,
        memory_id="principle",
        memory_type="principle",
        description="修改数据前必须取得我的确认。",
    )
    seed_memory(
        store, memory_id="preference", description="请先给结论，再说明分析过程。"
    )
    seed_memory(
        store,
        memory_id="revoked",
        status="revoked",
        description="这条撤销记忆不能被注入。",
    )
    assert (
        client.patch("/api/v1/ug/me", json={"display_name": "小周"}).status_code == 200
    )
    original_client = httpx.AsyncClient
    requests = []

    async def record_request(request):
        requests.append(request)

    def transport_client(**kwargs):
        return original_client(
            transport=httpx.ASGITransport(client.app),
            event_hooks={"request": [record_request]},
            **kwargs,
        )

    monkeypatch.setattr(cm_client, "resolve_cm_base_url", lambda: "http://testserver")
    monkeypatch.setattr(cm_client.httpx, "AsyncClient", transport_client)
    context = SimpleNamespace(
        request_context={"access_token": "user-token"}, chat_id="chat-1"
    )
    middleware = prompt.GlobalMemoryPromptMiddleware(run_context=context)
    result = await middleware.on_system_prompt(None, "base prompt")
    assert "姓名：小周" in result
    assert "修改数据前必须取得我的确认。" in result
    assert "请先给结论，再说明分析过程。" in result
    assert "这条撤销记忆不能被注入。" not in result
    assert result.endswith("base prompt")
    assert await middleware.on_system_prompt(None, "base prompt") == result
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/ug/me/global-memories"
    assert requests[0].headers["Authorization"] == "Bearer user-token"
    assert client.delete("/api/v1/ug/me/memories/preference").status_code == 200
    refreshed = prompt.GlobalMemoryPromptMiddleware(run_context=context)
    assert "请先给结论，再说明分析过程。" not in await refreshed.on_system_prompt(
        None, "base prompt"
    )
    client.app.state.test_context = authorization(user_id="empty-user")
    empty = prompt.GlobalMemoryPromptMiddleware(run_context=context)
    assert await empty.on_system_prompt(None, "base prompt") == "base prompt"
    assert len(requests) == 3


def test_manual_memory_create_merge_edit_references_and_revoke(api):
    client, store, _ = api
    first, second = seed_target(store), seed_target(store, EVENT)
    payload = {"type": "trigger", "triggers": ["准备写 SQL"], "references": [first]}
    memory = create(client, store, **payload).json()["data"]
    path = f"/api/v1/ug/me/memories/{memory['id']}"
    merged = create(client, store, **payload).json()["data"]
    assert merged["id"] == memory["id"]
    assert merged["created_at"] == memory["created_at"]
    assert set(memory["source_refs"]) <= set(merged["source_refs"])
    description = "准备写 SQL 时请先确认是否需要排除测试用户。"
    response = client.patch(path, json={"description": description})
    assert response.status_code == 200, response.text
    edited = client.get(path).json()["data"]
    assert edited["id"] == memory["id"] and edited["description"] == description
    assert [ref["key"] for ref in edited["references"]] == [first["key"]]
    response = client.patch(path, json={"references": [second]})
    assert response.status_code == 200, response.text
    assert [ref["key"] for ref in client.get(path).json()["data"]["references"]] == [
        second["key"]
    ]
    assert not any(target == first["key"] for _, _, target in store.edges)
    response = client.patch(path, json={"references": []})
    assert response.status_code == 200, response.text
    assert client.get(path).json()["data"]["references"] == []
    response = client.delete(path)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "revoked"
    assert (
        client.get("/api/v1/ug/me/memories", params={"status": "active"}).json()[
            "meta"
        ]["total"]
        == 0
    )
    revoked = client.get("/api/v1/ug/me/memories", params={"status": "revoked"}).json()[
        "data"
    ]
    assert [item["id"] for item in revoked] == [memory["id"]]


@pytest.mark.parametrize("role", ["ws_admin", "tenant_admin"])
def test_admin_review_is_workspace_scoped_and_write_routes_are_self_only(api, role):
    client, store, _ = api
    seed_memory(store, user_id="other")
    seed_memory(
        store, user_id="outside", workspace_id="workspace-2", memory_id="outside-memory"
    )
    client.app.state.test_context = authorization(role=role)
    assert client.get("/api/v1/ug/users").json()["meta"]["total"] == 1
    assert client.get("/api/v1/ug/users/other").json()["data"]["user_id"] == "other"
    assert client.get("/api/v1/ug/users/other/memories").json()["meta"]["total"] == 1
    assert client.get("/api/v1/ug/users/other/memories/seed-memory").status_code == 200
    assert client.get("/api/v1/ug/users/outside").json()["data"] == []
    assert (
        client.get("/api/v1/ug/users/outside/memories/outside-memory").status_code
        == 404
    )
    for method, path, body in [
        ("PATCH", "/users/other", {"display_name": "changed"}),
        (
            "POST",
            "/users/other/memories",
            {"type": "preference", "description": "不能替别人修改记忆。"},
        ),
        (
            "PATCH",
            "/users/other/memories/seed-memory",
            {"description": "不能替别人修改记忆。"},
        ),
        ("DELETE", "/users/other/memories/seed-memory", None),
    ]:
        response = client.request(method, f"/api/v1/ug{path}", json=body)
        assert response.status_code == 405
        assert response.json() == {"detail": "Method Not Allowed"}
    for method in ("GET", "PATCH", "DELETE"):
        response = client.request(
            method,
            "/api/v1/ug/me/memories/seed-memory",
            json={"description": "不能替别人修改记忆。"} if method == "PATCH" else None,
        )
        assert response.status_code == 404


def test_stale_memory_edit_returns_conflict_without_overwriting_newer_facts(api):
    client, store, _ = api
    memory = create(client, store).json()["data"]
    record_key = next(key for key in store.nodes if key[0] is USER_MEMORY)

    def change():
        store.nodes[record_key]["description"] = "已经在另一个窗口保存了新内容。"

    store.before_atomic = change
    response = client.patch(
        f"/api/v1/ug/me/memories/{memory['id']}",
        json={"description": "这个修改使用了过时的内容。"},
    )
    assert response.status_code == 409, response.text
    assert store.nodes[record_key]["description"] == "已经在另一个窗口保存了新内容。"


def test_profile_clear_preserves_existing_clocks_and_clockless_profiles(api):
    client, store, _ = api
    user = seed_user(store)
    assert (
        client.patch("/api/v1/ug/me", json={"display_name": "Alice"}).status_code == 200
    )
    assert json.loads(store.nodes[(USER, user.key)]["profile"]) == {
        "display_name": "Alice"
    }
    assert (
        client.patch("/api/v1/ug/me", json={"display_name": None}).json()["data"][
            "profile"
        ]
        is None
    )
    assert not store.nodes[(USER, user.key)].get("profile")
    old = "2026-01-01T00:00:00.000000+00:00"
    store.nodes[(USER, user.key)]["profile"] = json.dumps(
        {
            "display_name": "Alice",
            "_evidence_at": {"display_name": old},
        }
    )
    assert client.patch("/api/v1/ug/me", json={"display_name": None}).status_code == 200
    profile = json.loads(store.nodes[(USER, user.key)]["profile"])
    assert (
        "display_name" not in profile and profile["_evidence_at"]["display_name"] > old
    )
    for body in ({}, {"display_name": ""}, {"display_name": "   "}):
        assert client.patch("/api/v1/ug/me", json=body).status_code == 422


def test_patch_type_clears_inapplicable_fields_and_rejects_duplicate_meaning(api):
    client, store, _ = api
    reference = seed_target(store)
    memory = create(
        client, store, type="trigger", triggers=["SQL"], references=[reference]
    ).json()["data"]
    path = f"/api/v1/ug/me/memories/{memory['id']}"
    response = client.patch(path, json={"type": "principle"})
    assert response.status_code == 200, response.text
    assert (
        response.json()["data"]["triggers"]
        == response.json()["data"]["references"]
        == []
    )
    other = create(
        client,
        store,
        type="principle",
        description="Always confirm before changing data.",
    ).json()["data"]
    response = client.patch(path, json={"description": other["description"].upper()})
    assert response.status_code == 409
    assert client.get(path).json()["data"]["description"] == memory["description"]

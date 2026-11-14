"""ASGI path dispatch used by the single uvicorn process."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwenpaw_data_auth.asgi_dispatch import DualASGI, is_auth_path


def test_auth_prefixes_do_not_swallow_nearby_context_paths():
    assert is_auth_path("/api/auth")
    assert is_auth_path("/api/auth/status")
    assert is_auth_path("/api/platform/tenants")
    assert is_auth_path("/api/users")
    assert is_auth_path("/api/users/x")
    assert is_auth_path("/api/access-requests")
    assert is_auth_path("/api/access-requests/x/approve")
    assert not is_auth_path("/api/authorship")
    assert not is_auth_path("/api/userspace")
    assert not is_auth_path("/api/health")
    assert not is_auth_path("/onboarding")
    assert not is_auth_path("/api/kg_admin/x")


def test_every_mounted_auth_router_prefix_is_dispatched_to_the_auth_app():
    """A router mounted on the Auth app but absent here reaches Context instead.

    Context answers every unknown ``/api/*`` path from its auth middleware, so
    the symptom is a plain 401/404 rather than a routing error — which is how
    ``/api/access-requests`` stayed broken in the single-process build
    while local nginx and the vite dev proxy forwarded it correctly.
    """
    from qwenpaw_data_auth import (
        access_request_api,
        auth_api,
        platform_api,
        role_api,
        tenant_api,
        user_api,
        workspace_api,
    )

    modules = (
        access_request_api,
        auth_api,
        platform_api,
        role_api,
        tenant_api,
        user_api,
        workspace_api,
    )
    for module in modules:
        prefix = module.router.prefix
        assert prefix, f"{module.__name__} router lost its prefix"
        assert is_auth_path(prefix), f"{prefix} is not dispatched to the Auth app"


def test_dual_asgi_sends_auth_status_to_the_auth_app_not_the_context_stub():
    auth = FastAPI()
    context = FastAPI()

    @auth.get("/api/auth/status")
    def auth_status():
        return {"enabled": True, "mode": "multi-user"}

    @context.get("/api/auth/status")
    def context_stub():
        return {"enabled": True, "has_users": False}

    @context.get("/api/health")
    def health():
        return {"ok": True}

    combined = DualASGI(auth, context)
    with TestClient(combined) as client:
        status = client.get("/api/auth/status").json()
        assert status["mode"] == "multi-user"
        assert client.get("/api/health").json() == {"ok": True}

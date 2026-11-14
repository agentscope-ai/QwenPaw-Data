"""SOP generation validates the public structure without starting an agent."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.api.deps import get_identity
from qwenpaw_data.host.core.api.routers import sop as router
from qwenpaw_data.host.core.db.engine import get_session_factory, reset_engine
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.sop import SOP
from qwenpaw_data.host.core.workspace.workspace_manager import (
    QwenPawDataWorkspaceManager,
)


def plan():
    return {
        "name": "Sales",
        "description": "Inspect and summarize",
        "expected_outcome": "Summary",
        "nodes": [
            {
                "node_id": "inspect",
                "name": "Inspect",
                "description": "Discover schema",
                "expected_outcome": "Verified schema",
                "deps": [],
            },
            {
                "node_id": "summarize",
                "name": "Summarize",
                "description": "Aggregate sales",
                "expected_outcome": "Summary",
                "deps": ["inspect"],
            },
        ],
    }


def test_sop_yaml_round_trip_and_stable_default_ids():
    value = plan()
    assert SOP.from_yaml(SOP.from_dict(value).to_yaml()).to_dict() == value
    value["nodes"] = [{k: v for k, v in value["nodes"][0].items() if k != "node_id"}]
    assert SOP.from_dict(value).nodes[0].node_id == "node_000"


@pytest.mark.parametrize(
    "corruption", ["cycle", "unknown", "duplicate", "runtime", "extra"]
)
def test_sop_rejects_invalid_dependencies_and_runtime_payloads(corruption):
    value = plan()
    if corruption == "cycle":
        value["nodes"][0]["deps"] = ["summarize"]
    elif corruption == "unknown":
        value["nodes"][0]["deps"] = ["missing"]
    elif corruption == "duplicate":
        value["nodes"][1]["node_id"] = "inspect"
    elif corruption == "runtime":
        value["nodes"][0]["state"] = "done"
    else:
        value["hidden_command"] = "anything"
    with pytest.raises(ValueError):
        SOP.from_dict(value)


def test_planning_uses_authorized_preferences_but_no_agent_or_workspace(
    monkeypatch, seed_test_preferences
):
    asyncio.run(reset_engine())
    model = SimpleNamespace(
        stream=True,
        generate_structured_output=AsyncMock(
            return_value=SimpleNamespace(content=plan())
        ),
    )
    build = Mock(return_value=model)
    monkeypatch.setattr(router, "build_model", build)
    lease = Mock(
        side_effect=AssertionError("Planning must not allocate an execution workspace")
    )
    monkeypatch.setattr(QwenPawDataWorkspaceManager, "lease", lease)
    app = create_app()
    with TestClient(app) as http:
        asyncio.run(seed_test_preferences(get_session_factory()))
        sid = http.post("/api/sessions", json={"datasource_id": "sales-db"}).json()[
            "session"
        ]["id"]
        response = http.post(
            f"/api/sessions/{sid}/sop/plan", json={"text": "Summarize sales"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["plan"] == plan()
        assert http.get(f"/api/sessions/{sid}/snapshot").json()["chats"] == []
        assert model.stream is False
        assert build.call_args.kwargs["usage"].session_id == sid
        model.generate_structured_output.assert_awaited_once()
        lease.assert_not_called()
        app.dependency_overrides[get_identity] = lambda: Identity(
            user_id="other", tenant_id="other", workspace_id="other"
        )
        forbidden = http.post(
            f"/api/sessions/{sid}/sop/plan", json={"text": "anything"}
        )
        assert forbidden.status_code == 404
        assert build.call_count == 1
    asyncio.run(reset_engine())

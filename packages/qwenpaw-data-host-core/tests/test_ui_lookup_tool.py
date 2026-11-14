"""Exercise the actual AgentScope invocation/response contract, not a stub chunk."""

import json

import httpx
import pytest
from agentscope.message import ToolResultState
from agentscope.tool import ToolChunk
from qwenpaw_data.host.core.agent.tools.ui import DatasetLookupTool
from qwenpaw_data.host.core.integrations.cm.client import ContextManagerClient


@pytest.mark.parametrize("exact", [True, False])
async def test_lookup_returns_sdk_content(monkeypatch, exact):
    calls = []

    def lookup(self, **params):
        calls.append(params)
        record = {
            "dataset_id": 7,
            "domain_id": 3,
            "datasource_id": "source-1",
            "dataset_name": "sales",
            "description": "original",
        }
        return record if exact else {"records": [record], "total": 1}

    monkeypatch.setattr(ContextManagerClient, "lookup_datasets", lookup)
    result = await DatasetLookupTool("test-token")(
        **({"dataset_id": 7} if exact else {})
    )
    assert isinstance(result, ToolChunk)
    assert result.state == ToolResultState.SUCCESS
    assert result.is_last
    payload = json.loads(result.content[0].text)
    assert payload == {
        "items": [
            {
                "object_ref": {
                    "kind": "cm.dataset",
                    "dataset_id": 7,
                    "domain_id": 3,
                    "datasource_id": "source-1",
                },
                "name": "sales",
                "description": "original",
            }
        ],
        "page": 1,
        "total": 1,
    }
    assert calls[0]["dataset_id"] == (7 if exact else None)


@pytest.mark.parametrize("failure", ["identity", "arguments", "http", "wrong-object"])
async def test_lookup_errors_are_valid_sanitized_sdk_chunks(monkeypatch, failure):
    def lookup(self, **params):
        if failure == "http":
            raise httpx.ConnectError("secret-token at private-host")
        if failure == "wrong-object":
            return {"dataset_id": 99, "domain_id": 3, "datasource_id": "source-1"}
        pytest.fail("Invalid input/identity must not call CM")

    monkeypatch.setattr(ContextManagerClient, "lookup_datasets", lookup)
    result = await DatasetLookupTool(None if failure == "identity" else "test-token")(
        dataset_id=-1 if failure == "arguments" else 7,
    )
    assert isinstance(result, ToolChunk)
    assert result.state == ToolResultState.ERROR
    assert json.loads(result.content[0].text) == {
        "error": "Dataset lookup failed or access denied; do not guess a target."
    }

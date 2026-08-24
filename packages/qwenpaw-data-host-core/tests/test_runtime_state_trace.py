from __future__ import annotations

import pytest

from qwenpaw_data.host.core.orchestration import RuntimeStateManager


class FakeEvent:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def to_dict(self) -> dict:
        return dict(self.payload)


@pytest.mark.asyncio
async def test_state_dict_persists_node_traces() -> None:
    rs = RuntimeStateManager()
    await rs.create_plan(
        name="Trace plan",
        description="Collect node trace",
        expected_outcome="Trace is persisted",
        nodes=[
            {
                "node_id": "fetch_data",
                "name": "Fetch data",
                "description": "Fetch data",
                "expected_outcome": "Dataset",
            },
        ],
    )
    await rs.update_subtask("fetch_data", "in_progress")

    rs.append_to_trace(FakeEvent({"type": "ToolCallStartEvent", "name": "Bash"}))

    snapshot = rs.state_dict()
    assert snapshot["traces"] == {
        "fetch_data": [{"type": "ToolCallStartEvent", "name": "Bash"}],
    }

    restored = RuntimeStateManager()
    restored.load_state_dict(snapshot)

    assert restored.state_dict()["traces"] == snapshot["traces"]


@pytest.mark.asyncio
async def test_append_to_trace_requires_active_node() -> None:
    rs = RuntimeStateManager()
    await rs.create_plan(
        name="Trace plan",
        description="Collect node trace",
        expected_outcome="Trace is persisted",
        nodes=[
            {
                "node_id": "fetch_data",
                "name": "Fetch data",
                "description": "Fetch data",
                "expected_outcome": "Dataset",
            },
        ],
    )

    rs.append_to_trace(FakeEvent({"type": "ToolCallStartEvent", "name": "Bash"}))

    assert rs.state_dict()["traces"] == {}

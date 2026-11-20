"""Verify whole-batch semantic grouping and its lossless fallback."""

import json

import pytest
from context_manager.process import Deduplicator
from context_manager.schema import GRAPH_SCHEMA, USER_MEMORY
from context_manager.store import NodeDraft
from context_manager.user import UserMemoryDeduplicator

from cm.user_memory.support import FakeLlmCaller


def memory(index, *, kind="preference", source="user_explicit", status="active"):
    return NodeDraft.build(
        GRAPH_SCHEMA,
        USER_MEMORY,
        f"memory-{index}",
        {
            "type": kind,
            "description": f"Reports should start with conclusion {index}.",
            "source_type": source,
            "source_ref": f"m{index}",
            "source_refs": (f"m{index}",),
            "status": status,
            "triggers": (f"phrase {index}",) if kind == "trigger" else (),
        },
    )


@pytest.mark.asyncio
async def test_grouping_keeps_selected_canonical_and_merges_evidence():
    caller = FakeLlmCaller(
        (json.dumps({"groups": [{"canonical_index": 1, "duplicate_indexes": [0]}]}),)
    )
    nodes = (memory(0), memory(1, source="user_implicit", status="needs_review"))
    deduplicator = UserMemoryDeduplicator(caller)
    result = await deduplicator.deduplicate(nodes)
    assert isinstance(deduplicator, Deduplicator)
    assert len(result) == 1 and result[0].ref == nodes[1].ref
    assert result[0].properties["description"] == nodes[1].properties["description"]
    assert result[0].properties["source_refs"] == ("m1", "m0")
    assert result[0].properties["source_ref"] == "m0"
    assert result[0].properties["source_type"] == "user_explicit"
    assert result[0].properties["status"] == "active"
    assert caller.embed_calls == []

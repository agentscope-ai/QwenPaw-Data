"""Verify User message extraction crosses directly into canonical Drafts."""

from __future__ import annotations

import json

import pytest
from context_manager.process import TextChunk
from context_manager.schema import USER_MEMORY
from context_manager.user import (
    UserMemoryChunkExtractor,
)
from context_manager.user.ingest import _message_chunks

from cm.user_memory.support import FakeLlmCaller


def _chunk(*messages: tuple[str, str]) -> TextChunk:
    """Encode one complete structured user-message chunk."""
    text = json.dumps(
        {"messages": [{"id": item[0], "text": item[1]} for item in messages]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return TextChunk(0, text, 0, len(text))


def _valid_memory(**changes: object) -> dict[str, object]:
    """Return one valid explicit preference for public extraction tests."""
    memory: dict[str, object] = {
        "type": "preference",
        "description": "The user prefers conclusions first.",
        "source_type": "user_explicit",
        "status": "active",
        "source_ref": "msg-1",
        "evidence_refs": ["msg-1"],
    }
    memory.update(changes)
    return memory


@pytest.mark.asyncio
async def test_target_extraction_keeps_corroboration():
    """Earlier evidence supports the target without being extracted on its own."""
    caller = FakeLlmCaller(
        (
            json.dumps(
                {
                    "profile": None,
                    "memories": [
                        _valid_memory(
                            source_type="user_implicit",
                            status="needs_review",
                            source_ref="msg-2",
                            evidence_refs=["msg-1", "msg-2"],
                        ),
                    ],
                }
            ),
        )
    )
    chunk = _message_chunks(
        (
            {
                "id": "msg-1",
                "text": "Please start with the conclusion.",
                "timestamp": "2026-01-01T00:00:00Z",
            },
            {
                "id": "msg-2",
                "text": "Again, give the conclusion first.",
                "timestamp": "2026-01-02T00:00:00Z",
            },
        ),
        target_message_id="msg-2",
    )[0]
    drafts = await UserMemoryChunkExtractor(caller, user_id="user-1").extract(chunk)
    assert len(drafts) == 1
    assert drafts[0].spec is USER_MEMORY
    assert drafts[0].properties["source_type"] == "user_implicit"
    assert drafts[0].properties["status"] == "needs_review"
    assert drafts[0].properties["source_refs"] == ("msg-1", "msg-2")
    request = json.loads(caller.complete_calls[0][0][-1].content)
    assert [item["id"] for item in request["target_messages"]] == ["msg-2"]
    assert [item["id"] for item in request["context_messages"]] == ["msg-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "继续执行剩余未完成的节点。",
        "请按当前已加载的执行计划(current_plan)逐步执行,完成每个节点后用 update_subtask 记录进度,直到所有节点完成。",
        "Keep the exact label ＧＭＶ.\nSELECT  'a  b' AS value;",
    ),
)
async def test_user_text_reaches_semantic_extraction_without_source_guessing(text):
    caller = FakeLlmCaller(('{"profile":null,"memories":[]}',))
    assert (
        await UserMemoryChunkExtractor(caller, user_id="user-1").extract(
            _chunk(("msg-1", text))
        )
        == ()
    )
    assert len(caller.complete_calls) == 1
    request = json.loads(caller.complete_calls[0][0][-1].content)
    assert request["target_messages"] == [{"id": "msg-1", "text": text}]

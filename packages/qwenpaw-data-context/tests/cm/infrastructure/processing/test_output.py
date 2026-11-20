"""Verify shared structured model output and retry boundaries."""

from unittest.mock import Mock

import pytest
from context_manager.model import ChatMessage, LlmCaller
from context_manager.process import complete_json


@pytest.mark.asyncio
async def test_malformed_output_stops_at_retry_budget():
    caller = Mock(spec=LlmCaller)
    caller.complete.side_effect = ("invalid", "invalid", '{"value":1}')
    with pytest.raises(ValueError, match="after 2 attempts"):
        await complete_json(
            caller,
            (ChatMessage("user", "Return JSON"),),
            schema={"type": "object"},
            attempts=2,
        )
    assert caller.complete.await_count == 2

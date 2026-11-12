# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from qwenpaw_data.tracing.runtime.host.metadata import trace_metadata


def test_trace_metadata_omits_chat_id() -> None:
    assert trace_metadata() is None
    payload = json.loads(
        trace_metadata(datasource_id="ds-1", parent_session_id="ses-parent") or ""
    )
    assert payload == {"datasource_id": "ds-1", "parent_session_id": "ses-parent"}


def test_trace_metadata_records_source_trace_id() -> None:
    payload = json.loads(trace_metadata(source_trace_id="ab" * 16) or "")
    assert payload == {"source_trace_id": "ab" * 16}

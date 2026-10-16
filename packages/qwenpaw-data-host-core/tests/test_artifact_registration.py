# -*- coding: utf-8 -*-
"""Artifact stream registration remains available without BizTrace."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
import pytest

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.context import RunContext


class _Stream:
    def __init__(self):
        self.items = []

    async def artifact_registered(self, **fields):
        self.items.append(fields)

    async def append(self, fields):
        self.items.append(fields)


async def test_registers_digest_metadata_when_biztrace_is_disabled(tmp_path):
    runtime = ChatRuntime(chats=object(), events=object(), hosts=object())
    stream = _Stream()
    runtime._envelope = SimpleNamespace(stream=stream)
    runtime._run_context = RunContext(
        session_id="session-1",
        chat_id="chat-1",
        workspace=tmp_path,
        paths=SimpleNamespace(artifact_dir=tmp_path),
        identity=Identity(user_id="alice"),
    )
    content = b"# report\n"
    (tmp_path / "report.md").write_bytes(content)

    await runtime._register_new_files()

    assert len(stream.items) == 1
    artifact = stream.items[0]
    assert artifact["path"] == "report.md"
    assert artifact["media_type"] == "text/markdown"
    assert artifact["size_bytes"] == len(content)
    assert artifact["digest"] == ("sha256:" + hashlib.sha256(content).hexdigest())
    assert artifact["presentation"]["visibility"] == "app_only"


async def test_explicit_deliverable_does_not_depend_on_filename(tmp_path):
    runtime = ChatRuntime(chats=object(), events=object(), hosts=object())
    stream = _Stream()
    runtime._envelope = SimpleNamespace(stream=stream)
    runtime._run_context = RunContext(
        session_id="s",
        chat_id="c",
        workspace=tmp_path,
        paths=SimpleNamespace(artifact_dir=tmp_path),
        identity=Identity(user_id="alice"),
    )
    (tmp_path / "march.html").write_text("<h1>Findings</h1>")
    await runtime.publish_analysis_artifact(
        "march.html", "primary", "data/report", "chat"
    )
    await runtime._register_new_files()
    assert len(stream.items) == 1
    assert stream.items[0]["presentation"]["role"] == "primary"
    assert stream.items[0]["presentation"]["visibility"] == "chat"
    await runtime.report_analysis_progress("publish_report")
    assert stream.items[-1] == {
        "object": "analysis.progress",
        "stage": "publish_report",
    }
    with pytest.raises(ValueError):
        await runtime.publish_analysis_artifact(
            "../outside.txt", "primary", "data/report", "chat"
        )
    (tmp_path / "escape").symlink_to(tmp_path.parent)
    with pytest.raises(ValueError):
        await runtime.publish_analysis_artifact(
            "escape/outside.txt", "primary", "data/report", "chat"
        )

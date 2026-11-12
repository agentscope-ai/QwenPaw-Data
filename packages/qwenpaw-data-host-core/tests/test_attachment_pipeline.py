# -*- coding: utf-8 -*-
"""Console attachment pipeline — the single upload channel for every file kind.

``POST /console/upload`` stores an attachment under ``paths.uploads_dir``, while
the prompt handed to the agent is rebuilt from ``uploads_relative_path``. The
report-reproduction skill reads its source report through that prompt path
(``rr parse --file uploads/<session>/<name>``), so the two spellings must stay
in lockstep — a divergence would surface as "file not found" inside the agent
long after the upload returned 200.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw_data.host.core.domain.attachment import (
    MAX_SIZE_BYTES,
    Attachment,
    uploads_relative_path,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.runtime.turn import compose_agent_input

REPORT_FILENAME = "月度经营分析报告.docx"


def _paths(tmp_path: Path) -> UserAgentPaths:
    return UserAgentPaths(
        home=tmp_path,
        tenant_id="tenant-a",
        workspace_id="ws-a",
        user_id="user-a",
        agent_id="default",
        session_id="session-a",
    )


def _receive(
    paths: UserAgentPaths,
    filename: str = REPORT_FILENAME,
    data: bytes = b"report-bytes",
) -> Attachment:
    return Attachment.receive(
        session_id=paths.session_id,
        identity=Identity(user_id="user-a", tenant_id="tenant-a", workspace_id="ws-a"),
        filename=filename,
        data=data,
        dest_dir=paths.uploads_dir,
    )


def test_upload_lands_exactly_where_the_agent_prompt_points(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    attachment = _receive(paths)

    assert attachment.storage_path == uploads_relative_path(
        paths.session_id, REPORT_FILENAME
    )
    # The agent runs with the workspace as its root, so the very string put into
    # the prompt has to resolve to the stored file.
    prompt = compose_agent_input(
        "复现这份报告",
        None,
        [attachment.to_ref()],
        session_id=paths.session_id,
    )
    assert f"`{attachment.storage_path}`" in prompt
    assert (paths.workspace / attachment.storage_path).read_bytes() == b"report-bytes"


def test_report_extension_needs_no_backend_gate(tmp_path: Path) -> None:
    """The format whitelist is a console concern; the backend gate is not one."""
    paths = _paths(tmp_path)
    for filename in (
        "report.docx",
        "report.htm",
        "report.html",
        "report.md",
        "report.txt",
    ):
        assert _receive(paths, filename=filename, data=b"x").filename == filename


def test_unsafe_and_duplicate_names_are_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _receive(paths)

    with pytest.raises(ValueError, match="duplicate filename"):
        _receive(paths)
    with pytest.raises(ValueError):
        _receive(paths, filename="nested/report.docx")
    with pytest.raises(ValueError, match="file is empty"):
        _receive(paths, filename="hidden.docx", data=b"")


def test_oversized_report_is_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with pytest.raises(ValueError, match="exceeds"):
        _receive(paths, data=b"x" * (MAX_SIZE_BYTES + 1))

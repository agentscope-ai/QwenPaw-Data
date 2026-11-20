# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from qwenpaw_data.host.core.query_log import Step, log_info, reset_logger
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.settings.log import LogSettings


@pytest.fixture(autouse=True)
def _isolate_query_log() -> None:
    reset_logger()
    yield
    reset_logger()


def test_rotates_when_file_exceeds_max_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_LOG_PATH", str(tmp_path / "query.log"))
    monkeypatch.setenv("QWENPAW_DATA_LOG_MAX_BYTES", "400")
    monkeypatch.setenv("QWENPAW_DATA_LOG_BACKUP_COUNT", "2")
    clear_settings()
    reset_logger()

    for i in range(20):
        log_info(
            request_id=f"chat_{i:02d}",
            step=Step.CHAT_END,
            message="Chat turn finished",
            context={"session_id": "ses_rotate", "n": i, "pad": "x" * 40},
        )
    reset_logger()

    current = tmp_path / "query.log"
    backup = tmp_path / "query.log.1"
    assert current.exists()
    assert backup.exists()
    assert current.stat().st_size <= 400
    assert (tmp_path / "query.log.3").exists() is False
    for path in (current, backup):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert record["step"] == "chat_end"


def test_log_settings_reject_non_positive_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_LOG_MAX_BYTES", "0")
    with pytest.raises(ValidationError):
        LogSettings()
    monkeypatch.setenv("QWENPAW_DATA_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("QWENPAW_DATA_LOG_BACKUP_COUNT", "0")
    with pytest.raises(ValidationError):
        LogSettings()

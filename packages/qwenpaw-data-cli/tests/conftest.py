from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_cli_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("QWENPAW_DATA_ENV_FILE", str(env_file))
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "qwenpaw-data-home"))


@pytest.fixture(autouse=True)
def restore_process_logging() -> Iterator[None]:
    root = logging.getLogger()
    root_level = root.level
    root_handlers = list(root.handlers)

    agentscope_logger = logging.getLogger("as")
    agentscope_level = agentscope_logger.level
    agentscope_propagate = agentscope_logger.propagate
    agentscope_handlers = list(agentscope_logger.handlers)

    yield

    for handler in list(root.handlers):
        if handler not in root_handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in root_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(root_level)

    for handler in list(agentscope_logger.handlers):
        if handler not in agentscope_handlers:
            agentscope_logger.removeHandler(handler)
            handler.close()
    for handler in agentscope_handlers:
        if handler not in agentscope_logger.handlers:
            agentscope_logger.addHandler(handler)
    agentscope_logger.setLevel(agentscope_level)
    agentscope_logger.propagate = agentscope_propagate

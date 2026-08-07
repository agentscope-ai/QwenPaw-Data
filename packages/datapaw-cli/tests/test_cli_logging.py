from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from datapaw.cli import logging_config
from datapaw.cli.logging_config import configure_cli_logging


def _owned_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging_config._DataPawCLIFileHandler)
    ]


def _flush_owned_handlers() -> None:
    for handler in _owned_handlers():
        handler.flush()


def test_configure_cli_logging_routes_root_and_agentscope_to_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("DATAPAW_HOME", str(home))
    root_non_terminal = logging.NullHandler()
    agentscope_non_terminal = logging.NullHandler()
    root_terminal = logging.StreamHandler(sys.stderr)
    agentscope_terminal = logging.StreamHandler(sys.stderr)
    # 其他测试（如 agentscope 导入）可能已向 "as" logger 挂载 handler，
    # 用 monkeypatch 替换列表以隔离并在结束后还原。
    monkeypatch.setattr(logging.getLogger("as"), "handlers", [])
    logging.getLogger().addHandler(root_non_terminal)
    logging.getLogger().addHandler(root_terminal)
    logging.getLogger("as").addHandler(agentscope_non_terminal)
    logging.getLogger("as").addHandler(agentscope_terminal)

    log_path = configure_cli_logging()
    logging.getLogger("httpx").info("root-info-marker")
    logging.getLogger("as").info("agentscope-info-marker")
    logging.getLogger(
        "datapaw.host.core.orchestration.middleware",
    ).info("datapaw-trace-marker")
    _flush_owned_handlers()

    captured = capsys.readouterr()
    content = log_path.read_text(encoding="utf-8")

    assert log_path == (home / "host" / "datapaw.log").resolve()
    assert captured.err == ""
    assert "root-info-marker" in content
    assert "agentscope-info-marker" in content
    assert "datapaw-trace-marker" in content
    assert "\x1b[" not in content
    assert root_non_terminal in logging.getLogger().handlers
    assert root_terminal not in logging.getLogger().handlers
    assert logging.getLogger("as").handlers == [agentscope_non_terminal]
    assert agentscope_terminal not in logging.getLogger("as").handlers
    assert logging.getLogger("as").propagate is True

    [handler] = _owned_handlers()
    assert handler.level == logging.INFO
    assert handler.maxBytes == 50 * 1024 * 1024
    assert handler.backupCount == 2


def test_configure_cli_logging_is_idempotent_and_tracks_home_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_home = tmp_path / "first"
    monkeypatch.setenv("DATAPAW_HOME", str(first_home))

    first_path = configure_cli_logging()
    [first_handler] = _owned_handlers()
    assert configure_cli_logging() == first_path
    assert _owned_handlers() == [first_handler]

    logging.getLogger("datapaw.test").info("written-once-marker")
    _flush_owned_handlers()
    assert (
        first_path.read_text(encoding="utf-8").count("written-once-marker")
        == 1
    )

    second_home = tmp_path / "second"
    monkeypatch.setenv("DATAPAW_HOME", str(second_home))
    second_path = configure_cli_logging()

    [second_handler] = _owned_handlers()
    assert second_path == (second_home / "host" / "datapaw.log").resolve()
    assert second_handler is not first_handler
    assert first_handler.stream is None


def test_cli_log_rotates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATAPAW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(logging_config, "_MAX_BYTES", 256)

    log_path = configure_cli_logging()
    for index in range(12):
        logging.getLogger("datapaw.rotation").info(
            "rotation-marker-%02d-%s",
            index,
            "x" * 80,
        )
    _flush_owned_handlers()

    assert log_path.is_file()
    assert log_path.with_name("datapaw.log.1").is_file()
    assert log_path.with_name("datapaw.log.2").is_file()


def test_runtime_import_does_not_install_terminal_handlers(
    tmp_path: Path,
) -> None:
    home = tmp_path / "subprocess-home"
    script = """
import json
import logging
from datapaw.cli.logging_config import configure_cli_logging

log_path = configure_cli_logging()
from datapaw.host.core import DataPawHost

logging.getLogger("httpx").info("runtime-root-marker")
logging.getLogger("as").info("runtime-agentscope-marker")
logging.getLogger(
    "datapaw.host.core.orchestration.middleware"
).info("runtime-datapaw-trace-marker")
for handler in logging.getLogger().handlers:
    handler.flush()

terminal_handlers = [
    type(handler).__name__
    for handler in logging.getLogger().handlers
    if isinstance(handler, logging.StreamHandler)
    and not isinstance(handler, logging.FileHandler)
]
print(json.dumps({
    "log_path": str(log_path),
    "terminal_handlers": terminal_handlers,
    "agentscope_handlers": len(logging.getLogger("as").handlers),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATAPAW_HOME": str(home),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result == {
        "log_path": str((home / "host" / "datapaw.log").resolve()),
        "terminal_handlers": [],
        "agentscope_handlers": 0,
    }
    content = (home / "host" / "datapaw.log").read_text(encoding="utf-8")
    assert "runtime-root-marker" in content
    assert "runtime-agentscope-marker" in content
    assert "runtime-datapaw-trace-marker" in content


def test_main_prints_wrapped_error_and_logs_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main_module = importlib.import_module("datapaw.cli.main")

    class FakeParser:
        def parse_args(self, arguments: list[str]) -> SimpleNamespace:
            assert arguments == ["run"]

            def fail(_: SimpleNamespace) -> int:
                raise RuntimeError("command exploded")

            return SimpleNamespace(command="run", handler=fail)

    monkeypatch.setattr(
        main_module,
        "build_parser",
        lambda **_: FakeParser(),
    )

    assert main_module.main(["run"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "datapaw: error: RuntimeError: command exploded\n"

    log_path = Path(os.environ["DATAPAW_HOME"]) / "host" / "datapaw.log"
    _flush_owned_handlers()
    content = log_path.read_text(encoding="utf-8")
    assert "DataPaw CLI command failed: command=run" in content
    assert "Traceback (most recent call last)" in content
    assert "RuntimeError: command exploded" in content


def test_main_reports_log_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main_module = importlib.import_module("datapaw.cli.main")

    def fail_logging() -> Path:
        raise OSError("permission denied")

    monkeypatch.setattr(
        main_module,
        "configure_cli_logging",
        fail_logging,
    )

    assert main_module.main(["run"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "datapaw: error: unable to initialize log file: permission denied\n"
    )


def test_stream_renderer_remains_explicit_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agentscope.event import ReplyEndEvent, TextBlockDeltaEvent

    from datapaw.cli.util import print_event_stream

    async def events():
        yield TextBlockDeltaEvent(
            reply_id="reply",
            block_id="block",
            delta="streamed answer",
        )
        yield ReplyEndEvent(session_id="session", reply_id="reply")

    asyncio.run(print_event_stream(events()))

    captured = capsys.readouterr()
    assert captured.out == "streamed answer\n"
    assert captured.err == ""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from datapaw.cli.env import datapaw_env_file, datapaw_repo_root, load_datapaw_env


def test_repo_root_is_discovered_from_cli_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    root = datapaw_repo_root()

    assert (root / "pyproject.toml").is_file()
    assert (root / "packages" / "datapaw-cli").is_dir()


def test_custom_env_file_is_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "custom.env"
    monkeypatch.setenv("DATAPAW_ENV_FILE", str(env_file))

    assert datapaw_env_file() == env_file.resolve()


def test_load_env_preserves_process_values_unless_override_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("DATAPAW_CLI_ENV_TEST=dotenv\n", encoding="utf-8")
    monkeypatch.setenv("DATAPAW_ENV_FILE", str(env_file))
    monkeypatch.setenv("DATAPAW_CLI_ENV_TEST", "process")

    assert load_datapaw_env(override=False) == env_file.resolve()
    assert os.environ["DATAPAW_CLI_ENV_TEST"] == "process"

    assert load_datapaw_env(override=True) == env_file.resolve()
    assert os.environ["DATAPAW_CLI_ENV_TEST"] == "dotenv"


def test_missing_env_file_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "missing.env"
    monkeypatch.setenv("DATAPAW_ENV_FILE", str(env_file))

    assert load_datapaw_env() == env_file.resolve()


def test_main_loads_env_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("datapaw.cli.main")
    calls: list[tuple[str, Any]] = []

    def fake_load(*, override: bool) -> Path:
        calls.append(("load", override))
        return Path("/unused/.env")

    def fake_configure_logging() -> Path:
        calls.append(("logging", None))
        return Path("/unused/host/datapaw.log")

    class FakeParser:
        def parse_args(self, arguments: list[str]) -> SimpleNamespace:
            calls.append(("parse", arguments))
            return SimpleNamespace(handler=lambda _args: 0)

    def fake_build_parser(*, include_internal: bool) -> FakeParser:
        calls.append(("build", include_internal))
        return FakeParser()

    monkeypatch.setattr(main_module, "load_datapaw_env", fake_load)
    monkeypatch.setattr(
        main_module,
        "configure_cli_logging",
        fake_configure_logging,
    )
    monkeypatch.setattr(main_module, "build_parser", fake_build_parser)

    assert main_module.main(["run"]) == 0
    assert calls == [
        ("load", False),
        ("logging", None),
        ("build", False),
        ("parse", ["run"]),
    ]

"""Source installation must include all packages omitted from the lock export."""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workspace_packages():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return {
        Path(member).name for member in config["tool"]["uv"]["workspace"]["members"]
    }


@pytest.mark.parametrize("skip_context", [False, True])
def test_portable_initializer_installs_complete_workspace(
    tmp_path, monkeypatch, skip_context
):
    common = _load(ROOT / "scripts/_local_common.py", "_local_common")
    monkeypatch.setitem(sys.modules, "_local_common", common)
    initializer = _load(ROOT / "scripts/init_local.py", "qpd_init_test")
    root = tmp_path / "checkout with spaces"
    commands = []
    monkeypatch.setattr(initializer, "repository_root", lambda: root)
    monkeypatch.setattr(initializer, "ensure_environment", lambda _: None)
    monkeypatch.setattr(initializer, "resolve_command", lambda command: command)

    def record(command, **kwargs):
        commands.append([str(part) for part in command])
        if command[1] == "venv":
            venv = Path(command[-1])
            for name in ["python", "qwenpaw-data"]:
                executable = common.venv_executable(venv, name)
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()

    monkeypatch.setattr(initializer, "run", record)
    args = [
        "--uv",
        "uv",
        "--skip-build",
        "--skip-npm-install",
        "--skip-cli-link",
        "--skip-mcp-config",
    ]
    if skip_context:
        args.append("--skip-context")
    assert initializer.main(args) == 0
    exported = next(
        command
        for command in commands
        if command[1] == "export" and "--all-packages" in command
    )
    assert "--all-packages" in exported and "--no-emit-workspace" in exported
    workspace_python = str(common.venv_executable(root / ".venv", "python"))
    installed = next(
        command
        for command in commands
        if command[1:3] == ["pip", "install"] and workspace_python in command
    )
    paths = [
        Path(installed[i + 1])
        for i, token in enumerate(installed)
        if token == "--editable"
    ]
    assert {path.name for path in paths} == _workspace_packages()
    assert all(path.parent == root / "packages" for path in paths)
    assert "--no-deps" in installed
    if not skip_context:
        context_python = str(
            common.venv_executable(
                root / "packages/qwenpaw-data-context/.venv", "python"
            )
        )
        context_export = next(command for command in commands if "--package" in command)
        assert "qwenpaw-data-context" in context_export and "--no-dev" in context_export
        context_install = next(
            command
            for command in commands
            if command[1:3] == ["pip", "install"]
            and context_python in command
            and "--editable" in command
        )
        for package in ["qwenpaw-data-auth", "qwenpaw-data-tracing"]:
            assert str(root / "packages" / package) in context_install
        locked = next(
            command
            for command in commands
            if command[1:3] == ["pip", "install"] and "-r" in command
        )
        assert "--require-hashes" in locked
        assert ["uv", "pip", "check", "--python", context_python] in commands
    assert ["uv", "pip", "check", "--python", workspace_python] in commands
    # The command paths remain single argv entries, including spaces.
    assert workspace_python in installed


def test_bash_initializer_installs_complete_workspace():
    script = (ROOT / "scripts/init_local.sh").read_text()
    installed = set(
        re.findall(r'--editable "\$\{repo_root\}/packages/([^\"]+)"', script)
    )
    assert installed == _workspace_packages()

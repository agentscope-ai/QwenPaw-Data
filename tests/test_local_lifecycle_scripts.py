"""Portable local lifecycle helper regression tests."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load_common() -> ModuleType:
    path = ROOT / "scripts" / "_local_common.py"
    spec = importlib.util.spec_from_file_location("qwenpaw_data_local_common_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_root_is_resolved() -> None:
    common = _load_common()
    assert common.repository_root() == ROOT
    assert common.repository_root().is_absolute()


def test_load_environment_preserves_explicit_values(tmp_path, monkeypatch) -> None:
    common = _load_common()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nDEMO_ONE=from-file\nDEMO_TWO='quoted value'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEMO_ONE", "from-process")
    monkeypatch.delenv("DEMO_TWO", raising=False)

    common.load_environment(env_file)

    assert os.environ["DEMO_ONE"] == "from-process"
    assert os.environ["DEMO_TWO"] == "quoted value"


def test_venv_executable_uses_platform_layout(tmp_path, monkeypatch) -> None:
    common = _load_common()
    monkeypatch.setattr(common.os, "name", "nt")
    assert (
        common.venv_executable(tmp_path, "python")
        == tmp_path / "Scripts" / "python.exe"
    )
    assert common.venv_executable(tmp_path, "npm") == tmp_path / "Scripts" / "npm.cmd"


def test_lifecycle_entrypoints_offer_help() -> None:
    for name in ("init_local.py", "start_local.py"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        compile(source, name, "exec")


def test_start_entrypoint_has_explicit_neo4j_escape_hatch() -> None:
    source = (ROOT / "scripts" / "start_local.py").read_text(encoding="utf-8")

    assert '"--skip-neo4j"' in source
    assert "if not args.skip_neo4j:" in source


def test_powershell_entrypoints_are_present() -> None:
    for name in ("init_local.ps1", "start_local.ps1"):
        path = ROOT / "scripts" / name
        assert path.is_file()
        assert "@args" in path.read_text(encoding="utf-8")

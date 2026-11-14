from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src"


def test_host_core_does_not_depend_on_qwenpaw_data_context() -> None:
    config = tomllib.loads(
        (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    dependencies = config["project"]["dependencies"]
    workspace_sources = config.get("tool", {}).get("uv", {}).get("sources", {})

    assert all(
        not dependency.startswith("qwenpaw-data-context") for dependency in dependencies
    )
    assert "qwenpaw-data-context" not in workspace_sources


def test_host_source_does_not_import_context_paths() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in SOURCE_ROOT.rglob("*.py")
    )

    assert "qwenpaw_data.context.paths" not in source


def test_host_paths_import_without_workspace_dependencies(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "from qwenpaw_data.host.core import Paths, resolve_qwenpaw_data_home; "
                f"paths = Paths({str(tmp_path)!r}, 'session-a'); "
                "assert paths.host_root.name == 'host'; "
                f"assert resolve_qwenpaw_data_home({str(tmp_path)!r}).is_absolute()"
            ),
        ],
        cwd=PACKAGE_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_host_api_starts_without_context_packages(tmp_path: Path) -> None:
    root = PACKAGE_ROOT.parents[1]
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("QWENPAW_DATA_", "TRACING_", "OTEL_"))
    }
    env.update(
        QWENPAW_DATA_HOME=str(tmp_path),
        QWENPAW_DATA_ENV_FILE=str(tmp_path / "absent.env"),
        QWENPAW_DATA_AUTH_MODE="local",
        QWENPAW_DATA_DB_URL=f"sqlite+aiosqlite:///{tmp_path / 'host.db'}",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join(str(root / "packages" / package / "src") for package in (
            "qwenpaw-data-skills", "qwenpaw-data-auth", "qwenpaw-data-tracing",
        )),
    )
    script = """
import importlib.abc, sys
class BlockContext(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('context_manager', 'semantic_config', 'profiling', 'qwenpaw_data.context')
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise ModuleNotFoundError('Context intentionally unavailable', name=fullname)
sys.meta_path.insert(0, BlockContext())
from fastapi.testclient import TestClient
from qwenpaw_data.host.core.api.app import app
with TestClient(app) as client:
    assert client.get('/api/auth/status').status_code == 200
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=tmp_path, env=env,
        text=True, capture_output=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

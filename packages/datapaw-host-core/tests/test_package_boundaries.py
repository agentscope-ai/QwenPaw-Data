from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src"


def test_host_core_does_not_depend_on_datapaw_context() -> None:
    config = tomllib.loads(
        (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    dependencies = config["project"]["dependencies"]
    workspace_sources = config.get("tool", {}).get("uv", {}).get("sources", {})

    assert all(
        not dependency.startswith("datapaw-context") for dependency in dependencies
    )
    assert "datapaw-context" not in workspace_sources


def test_host_source_does_not_import_context_paths() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in SOURCE_ROOT.rglob("*.py")
    )

    assert "datapaw.context.paths" not in source


def test_host_paths_import_without_workspace_dependencies(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "from datapaw.host.core import Paths, resolve_datapaw_home; "
                f"paths = Paths({str(tmp_path)!r}, 'session-a'); "
                "assert paths.host_root.name == 'host'; "
                f"assert resolve_datapaw_home({str(tmp_path)!r}).is_absolute()"
            ),
        ],
        cwd=PACKAGE_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr

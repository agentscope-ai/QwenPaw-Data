from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_SCRIPT = REPO_ROOT / "scripts" / "env.sh"
INIT_LOCAL_SCRIPT = REPO_ROOT / "scripts" / "init_local.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="legacy Bash helper tests; native Windows lifecycle has separate tests",
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("~", "{home}"),
        ("~/.local/bin", "{home}/.local/bin"),
        ("/opt/datapaw", "/opt/datapaw"),
        ("relative/path", "{cwd}/relative/path"),
    ],
)
def test_datapaw_abs_path(
    tmp_path: Path,
    value: str,
    expected: str,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(fake_home)

    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; datapaw_abs_path "$2"',
            "bash",
            str(ENV_SCRIPT),
            value,
        ],
        cwd=working_dir,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == expected.format(
        home=fake_home,
        cwd=working_dir,
    )


def test_init_local_uses_temporary_mcp_import_file() -> None:
    script = INIT_LOCAL_SCRIPT.read_text(encoding="utf-8")

    assert 'mcp_import_file="$(mktemp ' in script
    assert 'mcp_import_file="${datapaw_home}/databridge.mcp.json"' not in script

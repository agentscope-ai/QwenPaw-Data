"""Console-only publication must never include engine distributions."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_VERSION = tomllib.loads(
    (ROOT / "packages/qwenpaw-data-console/pyproject.toml").read_text()
)["project"]["version"]


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("console-v0.3.3", ["console"]),
        ("v0.3.4", ["console", "skills", "context", "host-core", "cli"]),
        ("", ["console", "skills", "context", "host-core", "cli"]),
    ],
)
def test_publication_scope(tag, expected, tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())
    step = next(s for s in workflow["jobs"]["build"]["steps"] if s.get("id") == "scope")
    script = step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    output = tmp_path / "output"
    subprocess.run(
        [sys.executable, "-"], input=script, text=True, check=True,
        env={**os.environ, "RELEASE_TAG": tag, "GITHUB_OUTPUT": str(output)},
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(values["publish_matrix"]) == [
        {"project": "qwenpaw-data-" + suffix, "environment": "pypi-" + suffix}
        for suffix in expected
    ]
    assert values["package"] == ("qwenpaw-data-console" if len(expected) == 1 else "")


@pytest.mark.parametrize("engine_artifact", [False, True])
def test_console_dist_check_rejects_engine_artifacts(tmp_path, engine_artifact):
    for name in (
        f"qwenpaw_data_console-{CONSOLE_VERSION}-py3-none-any.whl",
        f"qwenpaw_data_console-{CONSOLE_VERSION}.tar.gz",
    ):
        (tmp_path / name).touch()
    if engine_artifact:
        (tmp_path / f"qwenpaw_data_context-{CONSOLE_VERSION}-py3-none-any.whl").touch()
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", f"console-v{CONSOLE_VERSION}",
         "--dist-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == (1 if engine_artifact else 0), result.stderr
    if engine_artifact:
        assert "distribution set mismatch" in result.stderr


def test_console_release_rejects_wrong_version():
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "console-v9.9.9"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert "does not match" in result.stderr

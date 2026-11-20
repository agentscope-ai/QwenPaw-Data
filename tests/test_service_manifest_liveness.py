"""Service manifest checks must never signal the processes being inspected."""

import os
import subprocess
import sys
from pathlib import Path


def test_manifest_check_preserves_live_process_and_detects_exit(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/common/service_manifest.py"
    env = os.environ | {
        "QWENPAW_DATA_ENV_FILE": str(tmp_path / "isolated.env"),
        "QWENPAW_DATA_DB_URL": "sqlite:///isolated.db",
    }
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        subprocess.run(
            [sys.executable, str(script), "write", "--pid", str(child.pid)],
            env=env, check=True,
        )
        subprocess.run([sys.executable, str(script), "check"], env=env, check=True)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)
    result = subprocess.run(
        [sys.executable, str(script), "check"], env=env, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "Service manifest is stale" in result.stderr

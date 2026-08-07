from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load_init_demo() -> ModuleType:
    path = ROOT / "examples" / "init_demo.py"
    spec = importlib.util.spec_from_file_location("datapaw_example_init_demo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_demo_is_repeatable_and_has_expected_aggregates(tmp_path: Path) -> None:
    demo = _load_init_demo()
    target = tmp_path / "nested" / "demo.sqlite"

    demo.seed_sqlite(target)
    demo.seed_sqlite(target)

    assert demo.query_sqlite(target) == demo.EXPECTED_DAILY_AVERAGES
    with sqlite3.connect(target) as connection:
        count = connection.execute("SELECT COUNT(*) FROM dws_gaap_di").fetchone()
    assert count == (demo.EXPECTED_ROW_COUNT,)


def test_demo_datasource_example_matches_local_compose_contract() -> None:
    payload = json.loads(
        (ROOT / "examples/demo/postgres/datasource.example.json").read_text(),
    )
    assert payload == {
        "datasource_name": "Demo PG - GAAP use case",
        "datasource_type": "postgresql",
        "config": {
            "host": "127.0.0.1",
            "port": 55432,
            "dbname": "datapaw_demo",
            "user": "datapaw",
            "password": "datapaw-demo",
        },
    }


def test_demo_environment_loader_preserves_explicit_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    demo = _load_init_demo()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEMO_FROM_FILE=loaded\nDEMO_EXPLICIT=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEMO_FROM_FILE", raising=False)
    monkeypatch.setenv("DEMO_EXPLICIT", "from-process")

    demo.load_repo_environment(env_file)

    assert os.environ["DEMO_FROM_FILE"] == "loaded"
    assert os.environ["DEMO_EXPLICIT"] == "from-process"


def test_demo_entrypoints_are_executable() -> None:
    entrypoints = (
        "examples/init_demo.py",
        "examples/init_demo.sh",
        "examples/init_demo.ps1",
        "examples/smoke_test.py",
    )
    for relative in entrypoints:
        assert (ROOT / relative).is_file(), relative
    if os.name != "nt":
        for relative in entrypoints:
            if not relative.endswith(".ps1"):
                assert (ROOT / relative).stat().st_mode & 0o111, relative

    powershell = (ROOT / "examples/init_demo.ps1").read_text(encoding="utf-8")
    assert "init_demo.py" in powershell
    assert '"compose", "-f"' in powershell
    assert "--postgres-dsn" in powershell
    assert '"--register"' in powershell
